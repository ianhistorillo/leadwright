"""Employee search and Apollo enrichment orchestration."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.enums import CompanyStatus, FinalStatus
from app.database.models import Company, Person
from app.repositories.company_repository import CompanyRepository
from app.repositories.person_repository import PersonRepository
from app.schemas.apollo import (
    ApolloBulkEnrichPersonDetail,
    ApolloEnrichedPerson,
    ApolloPeopleSearchRequest,
    ApolloPersonSearchResult,
)
from app.schemas.person import (
    PersonEnrichRequest,
    PersonEnrichResponse,
    PersonListResponse,
    PersonRead,
    PersonSearchRequest,
    PersonSearchResponse,
)
from app.services.acceptance import evaluate_apollo_email_status
from app.services.apollo import MAX_BULK_ENRICH_PEOPLE, ApolloClient, ApolloValidationError
from app.services.email_guess import guess_work_emails
from app.services.job_title_profiles import resolve_job_titles
from app.utils.domain import (
    DomainNormalizationError,
    is_name_lookup_domain,
    normalize_domain,
    website_from_domain,
)
from app.utils.email import normalize_email
from app.utils.logging import get_logger

logger = get_logger(__name__)


class PeopleService:
    """Search Apollo for employees, enrich contacts, and persist results."""

    def __init__(
        self,
        session: Session,
        *,
        apollo_client: ApolloClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._companies = CompanyRepository(session)
        self._people = PersonRepository(session)
        self._apollo = apollo_client

    def get_person(self, person_id: str) -> Person | None:
        return self._people.get_by_id(person_id)

    def list_people(
        self,
        *,
        company_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> PersonListResponse:
        if company_id:
            items = self._people.list_by_company(
                company_id, offset=offset, limit=limit
            )
            total = self._people.count_by_company(company_id)
        else:
            items = self._people.list_people(offset=offset, limit=limit)
            total = self._people.count()
        return PersonListResponse(
            total=total,
            items=[PersonRead.model_validate(item) for item in items],
        )

    async def search_company_people(
        self,
        company_id: str,
        request: PersonSearchRequest | None = None,
    ) -> PersonSearchResponse:
        """
        Run Apollo People API Search for one company domain and upsert people.

        Search does not return emails/phones (Apollo docs). Enrichment is Phase 6.
        """
        company = self._companies.get_by_id(company_id)
        if company is None:
            raise LookupError(f"Company not found: {company_id}")

        filters = request or PersonSearchRequest()
        job_titles = resolve_job_titles(
            filters.job_title_profile,
            titles=filters.job_titles,
            settings=self._settings,
        )

        company.status = CompanyStatus.PROCESSING.value
        self._session.flush()

        apollo = self._apollo or ApolloClient(settings=self._settings)
        owns_client = self._apollo is None

        created = 0
        updated = 0
        skipped = 0
        skipped_existing = 0
        discovered = 0
        pages_fetched = 0
        total_entries: int | None = None
        touched: list[Person] = []

        try:
            name_search_fallback = False
            for page in range(1, filters.max_pages + 1):
                search_request = self._build_people_search_request(
                    company,
                    filters,
                    job_titles,
                    page=page,
                    name_fallback=name_search_fallback,
                )
                logger.info(
                    "apollo_people_search",
                    extra={
                        "company_id": company.id,
                        "domain": company.domain,
                        "lookup_name": company.lookup_name,
                        "by_name": is_name_lookup_domain(company.domain),
                        "name_fallback": name_search_fallback,
                        "skip_existing_people": bool(filters.skip_existing_people),
                        "page": page,
                        "per_page": filters.per_page,
                        "title_count": len(job_titles),
                    },
                )
                try:
                    response = await apollo.search_people(search_request)
                except ApolloValidationError:
                    if (
                        page == 1
                        and is_name_lookup_domain(company.domain)
                        and not name_search_fallback
                    ):
                        # Official docs omit q_organization_name; fall back to keywords.
                        name_search_fallback = True
                        search_request = self._build_people_search_request(
                            company,
                            filters,
                            job_titles,
                            page=page,
                            name_fallback=True,
                        )
                        logger.warning(
                            "apollo_people_search_name_fallback",
                            extra={"company_id": company.id, "lookup_name": company.lookup_name},
                        )
                        response = await apollo.search_people(search_request)
                    else:
                        raise
                pages_fetched += 1
                if total_entries is None:
                    total_entries = response.total_entries

                if not response.people:
                    break

                for result in response.people:
                    if result is None or not result.id:
                        skipped += 1
                        continue
                    if not self._title_matches_filters(
                        result.title, job_titles, include_similar=filters.include_similar_titles
                    ):
                        skipped += 1
                        continue
                    person, action = self._upsert_search_result(
                        company,
                        result,
                        skip_existing=bool(filters.skip_existing_people),
                        source_job_id=filters.source_job_id,
                    )
                    if action == "skipped_existing":
                        skipped_existing += 1
                        continue
                    self._apply_apollo_company_from_search(company, result)
                    discovered += 1
                    if action == "created":
                        created += 1
                        touched.append(person)  # type: ignore[arg-type]
                    elif action == "updated":
                        updated += 1
                        touched.append(person)  # type: ignore[arg-type]
                    else:
                        skipped += 1

                # Commit after each page so a crash mid-company keeps progress
                # (Search is 0 credits; resume re-upserts safely).
                self._session.commit()

                page_people = [p for p in response.people if p is not None]
                if len(page_people) < filters.per_page:
                    break
                if total_entries is not None and page * filters.per_page >= total_entries:
                    break

            company.status = CompanyStatus.COMPLETED.value
            self._session.commit()
            for person in touched:
                try:
                    self._session.refresh(person)
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._session.refresh(company)
            except Exception:  # noqa: BLE001
                company = self._companies.get_by_id(company_id) or company
        except Exception:
            self._session.rollback()
            company = self._companies.get_by_id(company_id)
            if company is not None:
                # Keep people already committed on prior pages; only mark failed
                # if nothing useful was saved yet.
                company.status = CompanyStatus.FAILED.value
                self._session.commit()
            raise
        finally:
            if owns_client:
                await apollo.aclose()

        return PersonSearchResponse(
            company_id=company.id,
            company_domain=company.domain,
            job_titles_used=job_titles,
            apollo_total_entries=total_entries,
            pages_fetched=pages_fetched,
            discovered=discovered,
            created=created,
            updated=updated,
            skipped_unchanged=skipped,
            skipped_existing=skipped_existing,
            people=[PersonRead.model_validate(p) for p in touched],
        )

    async def enrich_people(
        self,
        *,
        company_id: str | None = None,
        request: PersonEnrichRequest | None = None,
    ) -> PersonEnrichResponse:
        """
        Enrich people via Apollo Bulk People Enrichment (max 10 per request).

        Applies APOLLO_REQUIRED_EMAIL_STATUS. Contacts that pass remain
        `unverified` until external verification (Phase 7). Failures are stored
        as `rejected` with a rejection reason.
        """
        options = request or PersonEnrichRequest()
        if company_id is not None and self._companies.get_by_id(company_id) is None:
            raise LookupError(f"Company not found: {company_id}")

        only_missing = options.only_missing_email and not options.force
        candidates = self._people.list_for_enrichment(
            company_id=company_id,
            person_ids=options.person_ids,
            only_missing_email=only_missing,
            source_job_id=options.source_job_id,
            limit=options.limit,
        )
        if options.person_ids and not candidates:
            raise LookupError("No enrichable people found for the provided person_ids")

        apollo = self._apollo or ApolloClient(settings=self._settings)
        owns_client = self._apollo is None

        enriched = 0
        apollo_accepted = 0
        apollo_rejected = 0
        missing_matches = 0
        batches = 0
        touched: list[Person] = []
        required = self._settings.apollo_required_email_status

        by_apollo_id = {
            person.apollo_person_id: person
            for person in candidates
            if person.apollo_person_id
        }

        try:
            apollo_ids = list(by_apollo_id.keys())
            for start in range(0, len(apollo_ids), MAX_BULK_ENRICH_PEOPLE):
                chunk_ids = apollo_ids[start : start + MAX_BULK_ENRICH_PEOPLE]
                details = [
                    ApolloBulkEnrichPersonDetail(id=apollo_id)
                    for apollo_id in chunk_ids
                ]
                logger.info(
                    "apollo_bulk_enrich",
                    extra={
                        "company_id": company_id,
                        "batch_size": len(details),
                        "batch_index": batches + 1,
                    },
                )
                response = await apollo.bulk_enrich_people(details)
                batches += 1

                matched_ids: set[str] = set()
                for match in response.matches:
                    if match is None or not match.id:
                        continue
                    person = by_apollo_id.get(match.id)
                    if person is None:
                        continue
                    matched_ids.add(match.id)
                    self._apply_enrichment_match(person, match)
                    enriched += 1
                    touched.append(person)
                    if person.final_status == FinalStatus.REJECTED.value:
                        apollo_rejected += 1
                    else:
                        apollo_accepted += 1

                for apollo_id in chunk_ids:
                    if apollo_id in matched_ids:
                        continue
                    person = by_apollo_id[apollo_id]
                    person.final_status = FinalStatus.REJECTED.value
                    person.rejection_reason = "apollo_enrichment_no_match"
                    missing_matches += 1
                    apollo_rejected += 1
                    touched.append(person)

            self._session.commit()
            for person in touched:
                self._session.refresh(person)
        except Exception:
            self._session.rollback()
            raise
        finally:
            if owns_client:
                await apollo.aclose()

        return PersonEnrichResponse(
            company_id=company_id,
            requested=len(candidates),
            enriched=enriched,
            apollo_accepted=apollo_accepted,
            apollo_rejected=apollo_rejected,
            missing_matches=missing_matches,
            batches=batches,
            required_apollo_email_status=required,
            people=[PersonRead.model_validate(p) for p in touched],
        )

    def _apply_enrichment_match(
        self,
        person: Person,
        match: ApolloEnrichedPerson,
    ) -> None:
        """Update a person from an Apollo enrichment match and apply Apollo gate."""
        if match.first_name:
            person.first_name = match.first_name
        if match.last_name:
            person.last_name = match.last_name
        if match.name:
            person.full_name = match.name
        elif match.first_name or match.last_name:
            person.full_name = " ".join(
                part for part in [match.first_name, match.last_name] if part
            )
        if match.title:
            person.job_title = match.title
        if match.linkedin_url:
            person.linkedin_url = match.linkedin_url

        person.apollo_email_status = match.email_status
        email = match.email
        normalized = normalize_email(email)

        if email and normalized:
            existing = self._people.get_by_normalized_email(normalized)
            if existing is not None and existing.id != person.id:
                person.email = email
                person.normalized_email = None
                person.final_status = FinalStatus.REJECTED.value
                person.rejection_reason = (
                    f"duplicate_normalized_email already assigned to person {existing.id}"
                )
                return
            person.email = email
            person.normalized_email = normalized
        else:
            person.email = email
            person.normalized_email = None

        decision = evaluate_apollo_email_status(
            person.apollo_email_status,
            settings=self._settings,
        )
        if not decision.accepted:
            person.final_status = FinalStatus.REJECTED.value
            person.rejection_reason = decision.reason
            return

        if not person.email:
            person.final_status = FinalStatus.REJECTED.value
            person.rejection_reason = "missing_email_after_apollo_enrichment"
            return

        # Apollo gate passed — awaiting external verification (Phase 7).
        person.final_status = FinalStatus.UNVERIFIED.value
        person.rejection_reason = None

    def apply_email_guesses_for_company(
        self,
        company_id: str,
        *,
        source_job_id: str | None = None,
    ) -> dict[str, int]:
        """
        Guess work emails from name + company domain (no Apollo enrichment).

        Sets apollo_email_status='guessed' when a primary candidate is stored.
        Alternate patterns are kept in rejection_reason for manual review/export.

        Guesses intentionally leave ``normalized_email`` NULL. That column is
        globally UNIQUE and firstname-only guesses collide constantly (Apollo
        often obfuscates last names). The display/export ``email`` field is
        enough for search-only runs.

        When ``source_job_id`` is set, only people created by that job are guessed
        (used by "new people only" batches).
        """
        company = self._companies.get_by_id(company_id)
        if company is None:
            raise LookupError(f"Company not found: {company_id}")

        guessed = 0
        skipped = 0
        people = self._people.list_by_company(company_id, offset=0, limit=5_000)
        if source_job_id:
            people = [p for p in people if p.source_job_id == source_job_id]

        if is_name_lookup_domain(company.domain):
            # No real domain yet — guessing @name-lookup.local is useless.
            for person in people:
                if person.email and (person.apollo_email_status or "").lower() != "guessed":
                    skipped += 1
                    continue
                person.email = None
                person.normalized_email = None
                person.apollo_email_status = None
                person.final_status = FinalStatus.UNVERIFIED.value
                person.rejection_reason = "no_real_domain_for_email_guess"
                skipped += 1
            self._session.commit()
            return {"guessed": guessed, "skipped": skipped}

        for person in people:
            if person.email and (person.apollo_email_status or "").lower() != "guessed":
                skipped += 1
                continue
            if (
                person.email
                and (person.apollo_email_status or "").lower() == "guessed"
            ):
                skipped += 1
                continue

            result = guess_work_emails(
                domain=company.domain,
                first_name=person.first_name,
                last_name=person.last_name,
                full_name=person.full_name,
            )
            if not result.primary:
                person.email = None
                person.normalized_email = None
                person.apollo_email_status = None
                person.final_status = FinalStatus.UNVERIFIED.value
                person.rejection_reason = (
                    result.reason or "insufficient_name_for_email_guess"
                )
                skipped += 1
                continue

            chosen = result.primary
            person.email = chosen
            # Critical: do not set normalized_email for guesses — UNIQUE crashes.
            person.normalized_email = None
            person.apollo_email_status = "guessed"
            person.final_status = FinalStatus.UNVERIFIED.value
            others = [c for c in result.candidates if c != chosen]
            person.rejection_reason = (
                f"guess_candidates:{'|'.join(others)}" if others else None
            )
            guessed += 1

        # Belt-and-suspenders: guessed rows must never claim the UNIQUE column.
        for person in people:
            if (person.apollo_email_status or "").lower() == "guessed":
                person.normalized_email = None

        self._session.commit()
        return {"guessed": guessed, "skipped": skipped}

    def accept_apollo_passed_without_external(
        self,
        company_id: str,
        *,
        source_job_id: str | None = None,
    ) -> int:
        """
        When ZeroBounce/external verify is skipped, accept Apollo-gated contacts.

        People with an email who already passed APOLLO_REQUIRED_EMAIL_STATUS
        (final_status still `unverified`) become `accepted`.
        """
        company = self._companies.get_by_id(company_id)
        if company is None:
            raise LookupError(f"Company not found: {company_id}")

        accepted = 0
        people = self._people.list_by_company(company_id, offset=0, limit=5_000)
        if source_job_id:
            people = [p for p in people if p.source_job_id == source_job_id]
        for person in people:
            if person.final_status != FinalStatus.UNVERIFIED.value:
                continue
            if not person.email:
                continue
            if (person.apollo_email_status or "").lower() == "guessed":
                continue
            decision = evaluate_apollo_email_status(
                person.apollo_email_status,
                settings=self._settings,
            )
            if not decision.accepted:
                continue
            person.final_status = FinalStatus.ACCEPTED.value
            person.rejection_reason = None
            accepted += 1

        self._session.commit()
        return accepted

    def _upsert_search_result(
        self,
        company: Company,
        result: ApolloPersonSearchResult,
        *,
        skip_existing: bool = False,
        source_job_id: str | None = None,
    ) -> tuple[Person | None, str]:
        """Insert or update a person from an Apollo search hit."""
        existing = self._people.get_by_apollo_person_id(result.id)
        full_name = self._build_full_name(result)
        last_name = result.last_name or None
        linkedin = (result.linkedin_url or "").strip() or None

        if existing is not None and skip_existing:
            # Already in DB (same Apollo person) — omit from this "new only" batch.
            return None, "skipped_existing"

        if existing is None:
            person = self._people.create(
                company_id=company.id,
                apollo_person_id=result.id,
                first_name=result.first_name,
                last_name=last_name,
                full_name=full_name,
                job_title=result.title,
                linkedin_url=linkedin,
                final_status=FinalStatus.UNVERIFIED.value,
                source_job_id=source_job_id,
            )
            return person, "created"

        changed = False
        if existing.company_id != company.id:
            existing.company_id = company.id
            changed = True
        if result.first_name and existing.first_name != result.first_name:
            existing.first_name = result.first_name
            changed = True
        if last_name and existing.last_name != last_name:
            existing.last_name = last_name
            changed = True
        if result.title and existing.job_title != result.title:
            existing.job_title = result.title
            changed = True
        if full_name and existing.full_name != full_name:
            existing.full_name = full_name
            changed = True
        if linkedin and existing.linkedin_url != linkedin:
            existing.linkedin_url = linkedin
            changed = True
        if source_job_id and existing.source_job_id != source_job_id:
            existing.source_job_id = source_job_id
            changed = True

        return existing, "updated" if changed else "skipped"

    @staticmethod
    def _title_matches_filters(
        title: str | None,
        job_titles: list[str],
        *,
        include_similar: bool,
    ) -> bool:
        """
        Local safety net: keep only titles that contain a configured filter token.

        Apollo can return loose matches even with include_similar_titles=false.
        When similar matching is enabled we still require a token hit so results
        stay aligned with TARGET_JOB_TITLES / the job snapshot.
        """
        if not job_titles:
            return True
        text = (title or "").strip().lower()
        if not text:
            return False
        for wanted in job_titles:
            token = (wanted or "").strip().lower()
            if token and token in text:
                return True
        # include_similar is Apollo-side only; we still enforce token overlap.
        _ = include_similar
        return False

    @staticmethod
    def _build_people_search_request(
        company: Company,
        filters: PersonSearchRequest,
        job_titles: list[str],
        *,
        page: int,
        name_fallback: bool = False,
    ) -> ApolloPeopleSearchRequest:
        """Domain search when website exists; organization-name search otherwise."""
        common = {
            "person_titles": job_titles,
            "include_similar_titles": filters.include_similar_titles,
            "person_seniorities": filters.person_seniorities,
            "person_locations": filters.person_locations,
            "organization_locations": filters.organization_locations,
            "contact_email_status": filters.contact_email_status,
            "page": page,
            "per_page": filters.per_page,
        }
        if is_name_lookup_domain(company.domain):
            org_name = (company.lookup_name or "").strip()
            if not org_name:
                raise ValueError(
                    f"Company {company.id} has no website/domain and no lookup_name "
                    "for Apollo organization-name search"
                )
            if name_fallback:
                return ApolloPeopleSearchRequest(q_keywords=org_name, **common)
            return ApolloPeopleSearchRequest(
                q_organization_name=org_name,
                **common,
            )
        return ApolloPeopleSearchRequest(
            q_organization_domains_list=[company.domain],
            **common,
        )

    def _apply_apollo_company_from_search(
        self,
        company: Company,
        result: ApolloPersonSearchResult,
    ) -> None:
        """
        Set company display fields from Apollo Search only (0 credits).

        CSV company_name is never kept as the export/display name — it is only
        stored on lookup_name for name-based search.
        """
        org = result.organization
        if org is None:
            return

        org_name = (org.name or "").strip()
        if org_name and company.name != org_name:
            company.name = org_name

        if org.id and not company.apollo_id:
            company.apollo_id = org.id

        # When the row was name-only, upgrade synthetic domain to Apollo's real one.
        if not is_name_lookup_domain(company.domain):
            return

        raw_domain = (org.primary_domain or "").strip()
        if not raw_domain and org.website_url:
            try:
                raw_domain = normalize_domain(org.website_url)
            except DomainNormalizationError:
                raw_domain = ""
        if not raw_domain:
            return
        try:
            real_domain = normalize_domain(raw_domain)
        except DomainNormalizationError:
            return
        if is_name_lookup_domain(real_domain):
            return

        existing = self._companies.get_by_domain(real_domain)
        if existing is not None and existing.id != company.id:
            # Another company already owns this domain — keep synthetic key,
            # but still expose website when Apollo provides one.
            if org.website_url and not company.website:
                company.website = org.website_url
            return

        company.domain = real_domain
        company.website = website_from_domain(real_domain)

    @staticmethod
    def _build_full_name(result: ApolloPersonSearchResult) -> str | None:
        if result.name:
            return result.name
        parts = [part for part in [result.first_name, result.last_name] if part]
        if parts:
            return " ".join(parts)
        if result.first_name and result.last_name_obfuscated:
            return f"{result.first_name} {result.last_name_obfuscated}"
        return result.first_name
