"""Fill employee_name + email on accepted_contacts CSVs via Apollo enrichment (credits)."""

from __future__ import annotations

import base64
import io
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.models import Company, Person
from app.schemas.apollo import (
    ApolloBulkEnrichPersonDetail,
    ApolloEnrichedPerson,
    ApolloPeopleSearchRequest,
)
from app.services.acceptance import evaluate_apollo_email_status
from app.services.apollo import MAX_BULK_ENRICH_PEOPLE, ApolloClient
from app.services.enrich_run_store import EnrichRunStore
from app.services.job_title_profiles import resolve_job_titles
from app.utils.domain import normalize_domain
from app.utils.email import normalize_email
from app.utils.logging import get_logger

logger = get_logger(__name__)

REQUIRED_COLUMNS = {"company_domain", "employee_name", "email"}
_OBFUSCATED_RE = re.compile(r"[*•…]")

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class ContactEnrichBackfillResult:
    total_rows: int = 0
    enrich_targets: int = 0
    skipped_complete: int = 0
    skipped_no_email: int = 0
    skipped_after_stop: int = 0
    skipped_already_processed: int = 0
    skipped_no_apollo_email: int = 0
    filled_from_db: int = 0
    names_updated: int = 0
    emails_updated: int = 0
    valid_emails_found: int = 0
    chargeable_matches: int = 0
    unresolved: int = 0
    already_exact: int = 0
    enrich_calls: int = 0
    search_calls: int = 0
    credits_consumed: float = 0.0
    people_done: int = 0
    stop_after_valid_emails: int = 0
    stopped_early: bool = False
    run_id: str | None = None
    csv_bytes: bytes = b""
    notes: list[str] = field(default_factory=list)

    def to_progress(
        self,
        *,
        phase: str,
        current_label: str | None = None,
        percent: float | None = None,
    ) -> dict[str, Any]:
        goal = self.stop_after_valid_emails or self.enrich_targets or 1
        if percent is None:
            if self.stop_after_valid_emails > 0:
                percent = round(
                    min(self.valid_emails_found / max(goal, 1), 1.0) * 100.0, 1
                )
            else:
                percent = round(
                    (self.people_done / max(self.enrich_targets, 1)) * 100.0, 1
                )
        return {
            "phase": phase,
            "current_label": current_label,
            "run_id": self.run_id,
            "total_rows": self.total_rows,
            "enrich_targets": self.enrich_targets,
            "people_done": self.people_done,
            "skipped_complete": self.skipped_complete,
            "skipped_no_email": self.skipped_no_email,
            "skipped_after_stop": self.skipped_after_stop,
            "skipped_already_processed": self.skipped_already_processed,
            "skipped_no_apollo_email": self.skipped_no_apollo_email,
            "filled_from_db": self.filled_from_db,
            "names_updated": self.names_updated,
            "emails_updated": self.emails_updated,
            "valid_emails_found": self.valid_emails_found,
            "chargeable_matches": self.chargeable_matches,
            "unresolved": self.unresolved,
            "already_exact": self.already_exact,
            "enrich_calls": self.enrich_calls,
            "search_calls": self.search_calls,
            "credits_consumed": self.credits_consumed,
            "stop_after_valid_emails": self.stop_after_valid_emails,
            "stopped_early": self.stopped_early,
            "percent": percent,
        }


class ContactEnrichBackfillService:
    """
    Correct ``employee_name`` and ``email`` using Apollo People Enrichment.

    Prefer local ``apollo_person_id`` (from prior search runs). Otherwise match via
    free People Search, then enrich by id. Falls back to name+domain enrichment.
    """

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        apollo_client: ApolloClient | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._apollo = apollo_client

    async def backfill_csv(
        self,
        content: bytes | None = None,
        *,
        only_incomplete: bool = True,
        only_enrich_if_has_email: bool = True,
        max_people: int = 500,
        stop_after_valid_emails: int = 0,
        max_pages_per_domain: int = 5,
        per_page: int = 25,
        run_id: str | None = None,
        resume: bool = False,
        source_filename: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> ContactEnrichBackfillResult:
        store = (
            EnrichRunStore(run_id)
            if run_id
            else EnrichRunStore.create()
        )
        prior = store.load_state() if resume or run_id else {}
        processed_keys: set[str] = set(prior.get("processed_keys") or [])

        if resume:
            content = store.load_checkpoint_csv()
            stop_after_valid_emails = int(
                prior.get("stop_after_valid_emails") or stop_after_valid_emails
            )
            source_filename = prior.get("source_filename") or source_filename

        if not content or not content.strip():
            raise ValueError("Uploaded CSV is empty")

        try:
            frame = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Unable to parse CSV: {exc}") from exc

        if frame.empty:
            raise ValueError("Uploaded CSV has no rows")

        columns = {str(c).strip().lower(): c for c in frame.columns}
        missing = REQUIRED_COLUMNS - set(columns)
        if missing:
            raise ValueError(
                "CSV must include columns: "
                + ", ".join(sorted(REQUIRED_COLUMNS))
                + f" (missing: {', '.join(sorted(missing))})"
            )

        domain_col = columns["company_domain"]
        name_col = columns["employee_name"]
        email_col = columns["email"]
        company_name_col = columns.get("company_name")
        title_col = columns.get("job_title")
        status_col = columns.get("apollo_email_status")

        frame["_domain"] = frame[domain_col].map(self._safe_domain)
        frame["_email"] = frame[email_col].map(
            lambda v: normalize_email(str(v) or "") or ""
        )
        frame["_first"] = frame[name_col].map(self._first_name)
        frame["_title"] = (
            frame[title_col].map(lambda v: str(v or "").strip().lower())
            if title_col
            else ""
        )
        frame["_org"] = (
            frame[company_name_col].map(lambda v: str(v or "").strip())
            if company_name_col
            else ""
        )
        frame["_status"] = (
            frame[status_col].map(lambda v: str(v or "").strip().lower())
            if status_col
            else ""
        )
        frame["_row_key"] = [
            self._row_key(
                domain=str(frame.at[i, "_domain"] or ""),
                email=str(frame.at[i, "_email"] or ""),
                first=str(frame.at[i, "_first"] or ""),
                title=str(frame.at[i, "_title"] or ""),
            )
            for i in frame.index
        ]

        result = ContactEnrichBackfillResult(total_rows=len(frame), run_id=store.run_id)
        if resume:
            result.valid_emails_found = int(prior.get("valid_emails_found") or 0)
            result.credits_consumed = float(prior.get("credits_consumed") or 0)
            result.names_updated = int(prior.get("names_updated") or 0)
            result.emails_updated = int(prior.get("emails_updated") or 0)
            result.people_done = int(prior.get("people_done") or 0)
            result.notes.append(f"Resuming enrich run {store.run_id}.")

        # Fill from local DB first (0 credits) when we already enriched before.
        result.filled_from_db = self._hydrate_from_db(
            frame,
            name_col=name_col,
            email_col=email_col,
            status_col=status_col,
        )
        if result.filled_from_db:
            # Refresh helpers after DB hydration.
            frame["_email"] = frame[email_col].map(
                lambda v: normalize_email(str(v) or "") or ""
            )
            frame["_first"] = frame[name_col].map(self._first_name)
            if status_col:
                frame["_status"] = frame[status_col].map(
                    lambda v: str(v or "").strip().lower()
                )
            result.notes.append(
                f"Applied {result.filled_from_db} rows from local DB (0 credits)."
            )

        targets: list[int] = []
        for idx in frame.index:
            i = int(idx)
            email = str(frame.at[i, "_email"] or "").strip()
            row_key = str(frame.at[i, "_row_key"] or "")
            if not email:
                result.skipped_no_email += 1
                continue
            if row_key and row_key in processed_keys:
                result.skipped_already_processed += 1
                continue
            if only_incomplete and not self._row_incomplete(
                employee_name=str(frame.at[i, name_col] or ""),
                email=email,
                apollo_email_status=str(frame.at[i, "_status"] or ""),
            ):
                result.skipped_complete += 1
                if row_key:
                    processed_keys.add(row_key)
                continue
            # Don't spend credits again on people Apollo already marked unavailable.
            if str(frame.at[i, "_status"] or "") == "unavailable":
                result.skipped_no_apollo_email += 1
                if row_key:
                    processed_keys.add(row_key)
                continue
            if not frame.at[i, "_domain"] or not frame.at[i, "_first"]:
                result.unresolved += 1
                continue
            targets.append(i)

        if max_people < 1:
            raise ValueError("max_people must be at least 1")
        if stop_after_valid_emails < 0:
            raise ValueError("stop_after_valid_emails cannot be negative")
        if len(targets) > max_people:
            result.notes.append(
                f"Capped enrichment attempts at {max_people} of {len(targets)} eligible rows "
                f"(raise max people to attempt to process more)."
            )
            targets = targets[:max_people]

        result.enrich_targets = len(targets)
        result.stop_after_valid_emails = stop_after_valid_emails

        def _persist(status: str) -> None:
            out = frame.drop(
                columns=[c for c in frame.columns if str(c).startswith("_")]
            )
            buffer = io.BytesIO()
            out.to_csv(buffer, index=False)
            csv_bytes = buffer.getvalue()
            result.csv_bytes = csv_bytes
            store.save_checkpoint_csv(csv_bytes)
            store.save_state(
                {
                    "status": status,
                    "source_filename": source_filename,
                    "processed_keys": sorted(processed_keys),
                    "valid_emails_found": result.valid_emails_found,
                    "people_done": result.people_done,
                    "enrich_targets": result.enrich_targets,
                    "stop_after_valid_emails": stop_after_valid_emails,
                    "credits_consumed": result.credits_consumed,
                    "names_updated": result.names_updated,
                    "emails_updated": result.emails_updated,
                    "skipped_already_processed": result.skipped_already_processed,
                    "skipped_no_apollo_email": result.skipped_no_apollo_email,
                    "filled_from_db": result.filled_from_db,
                    "chargeable_matches": result.chargeable_matches,
                }
            )

        _persist("running")
        await self._emit(
            on_progress,
            result.to_progress(phase="starting", percent=0),
        )

        if not targets:
            result.notes.append("No rows needed enrichment.")
            _persist("completed")
            await self._emit(
                on_progress,
                {
                    **result.to_progress(phase="completed", percent=100),
                    "csv_base64": base64.b64encode(result.csv_bytes).decode("ascii"),
                    "notes": result.notes,
                },
            )
            return result

        apollo = self._apollo or ApolloClient(settings=self._settings)
        owns_client = self._apollo is None
        job_titles = resolve_job_titles("custom", settings=self._settings)

        try:
            await self._emit(
                on_progress,
                result.to_progress(phase="resolving_ids"),
            )
            id_by_row, has_email_by_id = await self._resolve_apollo_ids(
                apollo,
                frame,
                targets,
                job_titles=job_titles,
                max_pages_per_domain=max_pages_per_domain,
                per_page=per_page,
                result=result,
                on_progress=on_progress,
            )

            details_by_row: dict[int, ApolloBulkEnrichPersonDetail] = {}
            for i in targets:
                apollo_id = id_by_row.get(i)
                first = str(frame.at[i, "_first"] or "").strip()
                domain = str(frame.at[i, "_domain"] or "").strip()
                org = str(frame.at[i, "_org"] or "").strip() or None

                if apollo_id and only_enrich_if_has_email:
                    # Require an explicit True from Search. Unknown/False are skipped
                    # so we don't spend credits on people likely to come back unavailable.
                    if has_email_by_id.get(apollo_id) is not True:
                        result.skipped_no_apollo_email += 1
                        continue

                if apollo_id:
                    details_by_row[i] = ApolloBulkEnrichPersonDetail(id=apollo_id)
                elif not only_enrich_if_has_email and first and domain:
                    # Name+domain enrich is riskier for credits; only when opted in.
                    details_by_row[i] = ApolloBulkEnrichPersonDetail(
                        first_name=first.title(),
                        domain=domain,
                        organization_name=org,
                    )
                else:
                    if only_enrich_if_has_email and not apollo_id:
                        result.skipped_no_apollo_email += 1
                    else:
                        result.unresolved += 1
                        result.people_done += 1
                        key = str(frame.at[i, "_row_key"] or "")
                        if key:
                            processed_keys.add(key)

            enrich_rows = [i for i in targets if i in details_by_row]
            result.notes.append(
                "Apollo bills ~1 credit when demographics/email data is returned — "
                "not only when email_status is verified. "
                "has_email=true still allows unavailable after enrich. "
                "With the email filter on, we only enrich people Search lists with "
                "contact_email_status=verified (and has_email=true)."
            )
            await self._emit(
                on_progress,
                result.to_progress(
                    phase="enriching",
                    current_label=f"0/{len(enrich_rows)}",
                ),
            )

            batch_index = 0
            for start in range(0, len(enrich_rows), MAX_BULK_ENRICH_PEOPLE):
                if (
                    stop_after_valid_emails > 0
                    and result.valid_emails_found >= stop_after_valid_emails
                ):
                    remaining = enrich_rows[start:]
                    result.skipped_after_stop += len(remaining)
                    result.stopped_early = True
                    break

                chunk = enrich_rows[start : start + MAX_BULK_ENRICH_PEOPLE]
                details = [details_by_row[i] for i in chunk]
                response = await apollo.bulk_enrich_people(details)
                batch_index += 1
                # Persist raw Apollo payload immediately (before further processing).
                store.save_batch_response(
                    batch_index,
                    apollo.last_raw_response,
                    meta=apollo.last_response_meta,
                )
                result.enrich_calls += 1
                if response.credits_consumed:
                    result.credits_consumed += float(response.credits_consumed)

                matches = list(response.matches or [])
                by_id = {m.id: m for m in matches if m and m.id}
                for offset, row_idx in enumerate(chunk):
                    detail = details_by_row[row_idx]
                    match = by_id.get(detail.id) if detail.id else None
                    if match is None and offset < len(matches):
                        match = matches[offset]
                    if match is None or not self._match_usable(match):
                        result.unresolved += 1
                    else:
                        if match.email or self._display_name(match):
                            result.chargeable_matches += 1
                        self._apply_match(
                            frame,
                            row_idx,
                            match,
                            name_col=name_col,
                            email_col=email_col,
                            status_col=status_col,
                            result=result,
                        )
                        self._sync_person_to_db(
                            frame,
                            row_idx,
                            match,
                            apollo_id=detail.id,
                        )
                    key = str(frame.at[row_idx, "_row_key"] or "")
                    if key:
                        processed_keys.add(key)
                    result.people_done += 1
                    # Checkpoint after every person so a crash mid-batch keeps progress.
                    _persist("running")

                done = min(start + len(chunk), len(enrich_rows))
                stop_label = ""
                if stop_after_valid_emails > 0:
                    stop_label = (
                        f" · valid {result.valid_emails_found}"
                        f"/{stop_after_valid_emails}"
                    )
                await self._emit(
                    on_progress,
                    result.to_progress(
                        phase="enriching",
                        current_label=(
                            f"{done}/{len(enrich_rows)}{stop_label}"
                            f" · credits {result.credits_consumed}"
                        ),
                        percent=(
                            round(
                                min(
                                    result.valid_emails_found
                                    / max(stop_after_valid_emails, 1),
                                    1.0,
                                )
                                * 100.0,
                                1,
                            )
                            if stop_after_valid_emails > 0
                            else round(
                                35.0 + (done / max(len(enrich_rows), 1)) * 65.0, 1
                            )
                        ),
                    ),
                )

                if (
                    stop_after_valid_emails > 0
                    and result.valid_emails_found >= stop_after_valid_emails
                ):
                    remaining = enrich_rows[start + len(chunk) :]
                    result.skipped_after_stop += len(remaining)
                    result.stopped_early = True
                    break
        except Exception:
            _persist("interrupted")
            raise
        finally:
            if owns_client:
                await apollo.aclose()

        result.notes.append(
            "Filled employee_name + email via Apollo People Enrichment "
            "(phone reveal off). "
            "Rows with empty/missing CSV email were skipped. "
            f"Checkpoint run_id={store.run_id} under data/output/enrich_runs/."
        )
        if result.stopped_early:
            result.notes.append(
                f"Stopped early after {result.valid_emails_found} Apollo-valid emails "
                f"with full name (target {stop_after_valid_emails}). "
                f"{result.skipped_after_stop} remaining candidates were not enriched."
            )
            _persist("stopped_early")
        else:
            _persist("completed")

        await self._emit(
            on_progress,
            {
                **result.to_progress(phase="completed", percent=100),
                "csv_base64": base64.b64encode(result.csv_bytes).decode("ascii"),
                "notes": result.notes,
            },
        )
        return result

    def _hydrate_from_db(
        self,
        frame: pd.DataFrame,
        *,
        name_col: str,
        email_col: str,
        status_col: str | None,
    ) -> int:
        """Copy verified name/email from local DB into incomplete CSV rows (0 credits)."""
        required = (self._settings.apollo_required_email_status or "verified").lower()
        filled = 0
        by_domain: dict[str, list[int]] = defaultdict(list)
        for idx in frame.index:
            i = int(idx)
            domain = str(frame.at[i, "_domain"] or "")
            if domain:
                by_domain[domain].append(i)

        for domain, indices in by_domain.items():
            company = self._session.scalar(
                select(Company).where(Company.domain == domain)
            )
            if company is None:
                continue
            people = list(
                self._session.scalars(
                    select(Person).where(Person.company_id == company.id)
                ).all()
            )
            email_map: dict[str, Person] = {}
            first_title_map: dict[tuple[str, str], list[Person]] = defaultdict(list)
            for person in people:
                status = (person.apollo_email_status or "").strip().lower()
                if status != required or not person.email:
                    continue
                full = (person.full_name or "").strip()
                if self._name_incomplete(full):
                    continue
                email = normalize_email(person.email) or ""
                if email:
                    email_map[email] = person
                first = (person.first_name or "").strip().lower()
                title = (person.job_title or "").strip().lower()
                if first:
                    first_title_map[(first, title)].append(person)

            for i in indices:
                if not self._row_incomplete(
                    employee_name=str(frame.at[i, name_col] or ""),
                    email=str(frame.at[i, "_email"] or ""),
                    apollo_email_status=str(frame.at[i, "_status"] or ""),
                ):
                    continue
                person = None
                email = str(frame.at[i, "_email"] or "")
                if email and email in email_map:
                    person = email_map[email]
                if person is None:
                    first = str(frame.at[i, "_first"] or "")
                    title = str(frame.at[i, "_title"] or "")
                    cands = first_title_map.get((first, title)) or []
                    if len(cands) == 1:
                        person = cands[0]
                if person is None:
                    continue
                frame.at[i, name_col] = person.full_name or frame.at[i, name_col]
                frame.at[i, email_col] = person.email or frame.at[i, email_col]
                if status_col and person.apollo_email_status:
                    frame.at[i, status_col] = person.apollo_email_status
                filled += 1
        return filled

    def _sync_person_to_db(
        self,
        frame: pd.DataFrame,
        row_idx: int,
        match: ApolloEnrichedPerson,
        *,
        apollo_id: str | None,
    ) -> None:
        """Best-effort persist enrich result so future runs skip (0 credits)."""
        try:
            domain = str(frame.at[row_idx, "_domain"] or "")
            if not domain:
                return
            company = self._session.scalar(
                select(Company).where(Company.domain == domain)
            )
            if company is None:
                return
            person = None
            if apollo_id:
                person = self._session.scalar(
                    select(Person).where(Person.apollo_person_id == apollo_id)
                )
            if person is None and match.email:
                email = normalize_email(match.email) or ""
                if email:
                    person = self._session.scalar(
                        select(Person).where(
                            Person.company_id == company.id,
                            Person.email == match.email,
                        )
                    )
            if person is None:
                return
            if match.first_name:
                person.first_name = match.first_name
            if match.last_name:
                person.last_name = match.last_name
            name = self._display_name(match)
            if name:
                person.full_name = name
            if match.email:
                person.email = match.email
                person.normalized_email = normalize_email(match.email)
            if match.email_status:
                person.apollo_email_status = match.email_status
            if match.title:
                person.job_title = match.title
            self._session.commit()
        except Exception:  # noqa: BLE001
            self._session.rollback()
            logger.exception("enrich_db_sync_failed")

    @staticmethod
    def _row_key(*, domain: str, email: str, first: str, title: str) -> str:
        if domain and email:
            return f"{domain}|{email}"
        return f"{domain}|{first}|{title}"

    async def _resolve_apollo_ids(
        self,
        apollo: ApolloClient,
        frame: pd.DataFrame,
        targets: list[int],
        *,
        job_titles: list[str],
        max_pages_per_domain: int,
        per_page: int,
        result: ContactEnrichBackfillResult,
        on_progress: ProgressCallback | None,
    ) -> tuple[dict[int, str], dict[str, bool | None]]:
        id_by_row: dict[int, str] = {}
        has_email_by_id: dict[str, bool | None] = {}
        by_domain: dict[str, list[int]] = defaultdict(list)
        for i in targets:
            domain = str(frame.at[i, "_domain"] or "")
            if domain:
                by_domain[domain].append(i)

        domains_total = len(by_domain)
        domains_done = 0
        for domain, indices in by_domain.items():
            domains_done += 1
            await self._emit(
                on_progress,
                result.to_progress(
                    phase="resolving_ids",
                    current_label=f"{domain} ({domains_done}/{domains_total})",
                    percent=round(
                        (domains_done - 0.5) / max(domains_total, 1) * 35.0, 1
                    ),
                ),
            )

            company = self._session.scalar(
                select(Company).where(Company.domain == domain)
            )
            people: list[Person] = []
            if company is not None:
                people = list(
                    self._session.scalars(
                        select(Person).where(Person.company_id == company.id)
                    ).all()
                )

            email_to_id: dict[str, str] = {}
            first_title_to_ids: dict[tuple[str, str], list[str]] = defaultdict(list)
            first_to_ids: dict[str, list[str]] = defaultdict(list)
            for person in people:
                if not person.apollo_person_id:
                    continue
                # DB people: unknown has_email unless they already carry a real email.
                has_email_by_id.setdefault(person.apollo_person_id, None)
                status = (person.apollo_email_status or "").strip().lower()
                if person.email and status not in {"guessed", "unavailable", ""}:
                    has_email_by_id[person.apollo_person_id] = True
                email = normalize_email(person.email or "") or ""
                if email:
                    email_to_id[email] = person.apollo_person_id
                first = (person.first_name or "").strip().lower()
                title = (person.job_title or "").strip().lower()
                if first:
                    first_title_to_ids[(first, title)].append(person.apollo_person_id)
                    first_to_ids[first].append(person.apollo_person_id)

            still_need = []
            for i in indices:
                email = str(frame.at[i, "_email"] or "")
                first = str(frame.at[i, "_first"] or "")
                title = str(frame.at[i, "_title"] or "")
                apollo_id = email_to_id.get(email) if email else None
                if not apollo_id and first:
                    candidates = first_title_to_ids.get((first, title)) or []
                    if len(set(candidates)) == 1:
                        apollo_id = candidates[0]
                    elif not title:
                        uniq = list(dict.fromkeys(first_to_ids.get(first) or []))
                        if len(uniq) == 1:
                            apollo_id = uniq[0]
                if apollo_id:
                    id_by_row[i] = apollo_id
                else:
                    still_need.append(i)

            # Always run verified-email Search for this domain (0 credits).
            # DB apollo ids only count as enrichable when they appear here.
            search_first_title: dict[tuple[str, str], list[str]] = defaultdict(list)
            search_first: dict[str, list[str]] = defaultdict(list)
            verified_ids: set[str] = set()
            for page in range(1, max(1, max_pages_per_domain) + 1):
                response = await apollo.search_people(
                    ApolloPeopleSearchRequest(
                        q_organization_domains_list=[domain],
                        person_titles=job_titles or None,
                        include_similar_titles=False,
                        contact_email_status=["verified"],
                        page=page,
                        per_page=min(per_page, 100),
                    )
                )
                result.search_calls += 1
                if not response.people:
                    break
                for person in response.people:
                    if not person.id:
                        continue
                    verified_ids.add(person.id)
                    has_email_by_id[person.id] = (
                        True if person.has_email is not False else False
                    )
                    first = (person.first_name or "").strip().lower()
                    title = (person.title or "").strip().lower()
                    if first:
                        search_first_title[(first, title)].append(person.id)
                        search_first[first].append(person.id)

            # DB-matched ids: only keep as enrichable if Apollo verified Search sees them.
            for i in list(indices):
                apollo_id = id_by_row.get(i)
                if not apollo_id:
                    continue
                if apollo_id in verified_ids:
                    has_email_by_id[apollo_id] = True
                else:
                    # Leave as None/False so enrich step skips (saves credits).
                    has_email_by_id.setdefault(apollo_id, False)
                    if has_email_by_id.get(apollo_id) is not True:
                        has_email_by_id[apollo_id] = False

            for i in still_need:
                first = str(frame.at[i, "_first"] or "")
                title = str(frame.at[i, "_title"] or "")
                apollo_id = None
                if first:
                    candidates = search_first_title.get((first, title)) or []
                    uniq = list(dict.fromkeys(candidates))
                    if len(uniq) == 1:
                        apollo_id = uniq[0]
                    else:
                        uniq_first = list(dict.fromkeys(search_first.get(first) or []))
                        if len(uniq_first) == 1:
                            apollo_id = uniq_first[0]
                if apollo_id:
                    id_by_row[i] = apollo_id

        return id_by_row, has_email_by_id

    def _apply_match(
        self,
        frame: pd.DataFrame,
        row_idx: int,
        match: ApolloEnrichedPerson,
        *,
        name_col: str,
        email_col: str,
        status_col: str | None,
        result: ContactEnrichBackfillResult,
    ) -> None:
        new_name = self._display_name(match)
        new_email = (match.email or "").strip()
        old_name = str(frame.at[row_idx, name_col] or "").strip()
        old_email = str(frame.at[row_idx, email_col] or "").strip()

        name_changed = False
        email_changed = False
        if new_name and new_name.lower() != old_name.lower():
            frame.at[row_idx, name_col] = new_name
            result.names_updated += 1
            name_changed = True
        if new_email and new_email.lower() != old_email.lower():
            # Don't replace an existing address with nothing useful.
            frame.at[row_idx, email_col] = new_email
            result.emails_updated += 1
            email_changed = True
        if status_col and match.email_status:
            # Keep unavailable visible for honesty, but if enrich returned no email
            # and we already had a guessed one, still record the Apollo status.
            frame.at[row_idx, status_col] = match.email_status

        if new_email:
            decision = evaluate_apollo_email_status(
                match.email_status,
                settings=self._settings,
            )
            final_name = str(frame.at[row_idx, name_col] or "").strip()
            # Count toward the stop goal only when we have verified email + full name.
            if decision.accepted and not self._name_incomplete(final_name):
                result.valid_emails_found += 1

        if not name_changed and not email_changed and (new_name or new_email):
            result.already_exact += 1
        if not new_name and not new_email:
            result.unresolved += 1

    @staticmethod
    def _match_usable(match: ApolloEnrichedPerson) -> bool:
        name = ContactEnrichBackfillService._display_name(match)
        return bool(name or (match.email or "").strip())

    @staticmethod
    def _display_name(match: ApolloEnrichedPerson) -> str:
        if match.name and not _OBFUSCATED_RE.search(match.name):
            return match.name.strip()
        parts = [
            p.strip()
            for p in [match.first_name, match.last_name]
            if p and not _OBFUSCATED_RE.search(p)
        ]
        return " ".join(parts)

    @staticmethod
    def _row_incomplete(
        *,
        employee_name: str,
        email: str,
        apollo_email_status: str,
    ) -> bool:
        """True when name and/or email still need Apollo enrichment.

        Empty emails are handled separately (skipped entirely — no credits).
        """
        if ContactEnrichBackfillService._name_incomplete(employee_name):
            return True
        if apollo_email_status in {"guessed", "unavailable"}:
            return True
        # email is required to reach this check; treat blank as incomplete
        # only as a safety net (caller should already skip empty emails).
        if not email:
            return True
        return False

    @staticmethod
    def _name_incomplete(employee_name: str) -> bool:
        text = (employee_name or "").strip()
        if not text:
            return True
        if _OBFUSCATED_RE.search(text):
            return True
        parts = [p for p in re.split(r"\s+", text) if p]
        if len(parts) < 2:
            return True
        last = parts[-1]
        return bool(_OBFUSCATED_RE.search(last) or len(last) < 2)

    async def _emit(
        self,
        on_progress: ProgressCallback | None,
        payload: dict[str, Any],
    ) -> None:
        if on_progress is None:
            return
        maybe = on_progress(payload)
        if maybe is not None and hasattr(maybe, "__await__"):
            await maybe  # type: ignore[misc]

    @staticmethod
    def _safe_domain(value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            return normalize_domain(raw)
        except Exception:  # noqa: BLE001
            return raw.lower().removeprefix("www.")

    @staticmethod
    def _first_name(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        first = text.split()[0]
        if _OBFUSCATED_RE.search(first):
            return ""
        return first.lower()
