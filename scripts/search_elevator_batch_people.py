"""Search people for a small elevator company batch and export guessed emails."""

from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.schemas.apollo import ApolloPeopleSearchRequest
from app.services.apollo import ApolloClient, ApolloValidationError
from app.services.email_guess import guess_work_emails
from app.services.job_title_profiles import resolve_job_titles
from app.utils.domain import DomainNormalizationError, normalize_domain

MAX_PAGES = 20
PER_PAGE = 100
TITLE_CHUNK = 6
OUT = ROOT / "data/output/elevator_batch_people_batch6.csv"
OUT.parent.mkdir(parents=True, exist_ok=True)

COLUMNS = [
    "company_name",
    "company_domain",
    "employee_name",
    "job_title",
    "email",
    "email_guess_candidates",
    "apollo_email_status",
    "external_verification_status",
    "linkedin_url",
    "final_status",
    "rejection_reason",
]

COMPANIES = [
    {"lookup_name": "Midea Group", "domain": "midea.com", "aliases": ["Midea"]},
    {"lookup_name": "Sansei Technologies, Inc.", "domain": "sansei-technologies.com", "aliases": ["Sansei Technologies"]},
    {"lookup_name": "MEI Total Elevator Solutions", "domain": "meiusa.com", "aliases": ["MEI Elevator", "MEI Total Elevator"]},
    {"lookup_name": "Lodige Industries GmbH", "domain": "lodige.com", "aliases": ["Lodige Industries", "Lodige"]},
    {"lookup_name": "Shanghai FUJI Elevator Co., Ltd.", "domain": "shanghai-fuji.com", "aliases": ["Shanghai FUJI Elevator", "Shanghai Fuji Elevator"]},
    {"lookup_name": "Zhujiang Fuji Elevator (China) Co., Ltd.", "domain": "zhujiangfuji.com", "aliases": ["Zhujiang Fuji Elevator", "Zhujiang Fuji"]},
    {"lookup_name": "GMV Martini SpA", "domain": "gmv.it", "aliases": ["GMV Martini", "GMV"]},
    {"lookup_name": "Joylive Elevator Co., Ltd.", "domain": "joylive.com", "aliases": ["Joylive Elevator", "Joylive"]},
    {"lookup_name": "Ametal Lift Systems", "domain": "ametal.com", "aliases": ["Ametal", "Ametal Lift"]},
    {"lookup_name": "Hybon Elevators", "domain": "hybonelevators.com", "aliases": ["Hybon Elevators"]},
    {"lookup_name": "Mowrey Elevator Company Inc.", "domain": "mowreyelevator.com", "aliases": ["Mowrey Elevator"]},
    {"lookup_name": "Star Elevator Group", "domain": "starelevatorgroup.com", "aliases": ["Star Elevator"]},
    {"lookup_name": "Nocee Elevators", "domain": "noceeelevators.com", "aliases": ["Nocee Elevators"]},
    {"lookup_name": "Orbis Elevator Co. Ltd.", "domain": "orbiselevator.com", "aliases": ["Orbis Elevator"]},
    {"lookup_name": "ATTA Elevators", "domain": "attaelevators.com", "aliases": ["ATTA Elevators"]},
    {"lookup_name": "SODIMAS", "domain": "sodimas.com", "aliases": ["Sodimas"]},
]


def _domain(org) -> str:
    if org is None:
        return ""
    raw = (org.primary_domain or "").strip()
    if not raw and org.website_url:
        raw = org.website_url
    if not raw:
        return ""
    try:
        return normalize_domain(raw)
    except DomainNormalizationError:
        host = raw.lower().replace("https://", "").replace("http://", "").split("/")[0]
        if host.startswith("www."):
            host = host[4:]
        return host if "." in host else ""


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _title_matches(title: str, titles: list[str]) -> bool:
    text = (title or "").strip().lower()
    if not text:
        return False
    for wanted in titles:
        token = wanted.strip().lower()
        if token and token in text:
            return True
    return False


def _org_belongs(company: dict, org_name: str, org_domain: str) -> bool:
    target = company["domain"].lower().strip()
    found = (org_domain or "").lower().strip()
    if found and found == target:
        return True
    if found and found != target:
        return False
    name = (org_name or "").lower()
    needles = [company["lookup_name"], *company.get("aliases", [])]
    for raw in needles:
        needle = " ".join((raw or "").lower().split())
        if len(needle) < 12:
            continue
        if needle in name:
            return True
    return False


def _full_name(person) -> str:
    if person.name:
        return person.name.strip()
    parts = [p for p in [person.first_name, person.last_name] if p]
    if parts:
        return " ".join(parts)
    if person.first_name and person.last_name_obfuscated:
        return f"{person.first_name} {person.last_name_obfuscated}"
    return (person.first_name or "").strip()


async def _one_search(
    apollo: ApolloClient,
    request: ApolloPeopleSearchRequest,
) -> tuple[list, int | None]:
    response = await apollo.search_people(request)
    people = [p for p in (response.people or []) if p is not None]
    return people, response.total_entries


async def search_company(apollo: ApolloClient, company: dict, titles: list[str]) -> list[dict]:
    rows: list[dict] = []
    seen_ids: set[str] = set()
    domain = company["domain"]
    names = [company["lookup_name"], *company.get("aliases", [])]
    unique_names = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))

    searches: list[ApolloPeopleSearchRequest] = [
        ApolloPeopleSearchRequest(
            q_organization_domains_list=[domain],
            person_titles=titles,
            include_similar_titles=True,
            page=1,
            per_page=PER_PAGE,
        ),
        # Wider domain sweep, then keep people whose titles match TARGET_JOB_TITLES.
        ApolloPeopleSearchRequest(
            q_organization_domains_list=[domain],
            include_similar_titles=True,
            page=1,
            per_page=PER_PAGE,
        ),
    ]
    for chunk in _chunks(titles, TITLE_CHUNK):
        searches.append(
            ApolloPeopleSearchRequest(
                q_organization_domains_list=[domain],
                person_titles=chunk,
                include_similar_titles=True,
                page=1,
                per_page=PER_PAGE,
            )
        )
    for name in unique_names:
        searches.append(
            ApolloPeopleSearchRequest(
                q_organization_name=name,
                person_titles=titles,
                include_similar_titles=True,
                page=1,
                per_page=PER_PAGE,
            )
        )
        searches.append(
            ApolloPeopleSearchRequest(
                q_keywords=name,
                person_titles=titles,
                include_similar_titles=True,
                page=1,
                per_page=PER_PAGE,
            )
        )

    collected: list[tuple[object, bool, bool]] = []
    for base in searches:
        require_title = not bool(base.person_titles)
        from_domain = bool(base.q_organization_domains_list)
        try:
            for page in range(1, MAX_PAGES + 1):
                request = base.model_copy(update={"page": page})
                people, total = await _one_search(apollo, request)
                if not people:
                    break
                collected.extend((person, require_title, from_domain) for person in people)
                if len(people) < PER_PAGE:
                    break
                if total is not None and page * PER_PAGE >= total:
                    break
        except ApolloValidationError:
            continue

    for person, require_title, from_domain in collected:
        if not person.id or person.id in seen_ids:
            continue
        if require_title and not _title_matches(person.title or "", titles):
            continue
        org_name = ""
        org_domain = ""
        if person.organization:
            org_name = (person.organization.name or "").strip()
            org_domain = _domain(person.organization)
        if not from_domain and not _org_belongs(company, org_name, org_domain):
            continue
        if from_domain and org_domain and org_domain != domain:
            continue
        seen_ids.add(person.id)
        guess_domain = domain
        row_company = org_name or company["lookup_name"]

        full_name = _full_name(person)
        guess = guess_work_emails(
            domain=guess_domain,
            first_name=person.first_name,
            last_name=person.last_name,
            full_name=full_name,
        )
        if guess.primary:
            email = guess.primary
            candidates = "; ".join(c for c in guess.candidates if c != guess.primary)
            status = "guessed"
            final = "unverified"
            reason = ""
        else:
            email = ""
            candidates = ""
            status = ""
            final = "unverified"
            reason = guess.reason or "no_real_domain_for_email_guess"

        rows.append(
            {
                "company_name": row_company,
                "company_domain": guess_domain,
                "employee_name": full_name,
                "job_title": person.title or "",
                "email": email,
                "email_guess_candidates": candidates,
                "apollo_email_status": status,
                "external_verification_status": "",
                "linkedin_url": (person.linkedin_url or "").strip(),
                "final_status": final,
                "rejection_reason": reason,
            }
        )

    label = company["lookup_name"].encode("ascii", "replace").decode("ascii")
    print(f"{label}: people={len(rows)} domain={domain}", flush=True)
    return rows


async def main() -> None:
    settings = get_settings()
    titles = resolve_job_titles("custom", settings=settings)
    print(f"titles ({len(titles)}): {titles}", flush=True)
    apollo = ApolloClient(settings=settings)
    all_rows: list[dict] = []
    try:
        for company in COMPANIES:
            all_rows.extend(await search_company(apollo, company, titles))
    finally:
        await apollo.aclose()

    with OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(all_rows)

    guessed = sum(1 for r in all_rows if r["apollo_email_status"] == "guessed")
    print(f"WROTE {len(all_rows)} rows guessed={guessed} -> {OUT}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
