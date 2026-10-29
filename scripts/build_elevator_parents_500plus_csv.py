"""Build stricter elevator manufacturing parent CSV (50+ employees)."""

from __future__ import annotations

import csv
import re
import time
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.models import Company
from app.utils.domain import DomainNormalizationError, is_name_lookup_domain, normalize_domain

TARGET = 200
PER_PAGE = 100
OUT = Path("data/output/elevator_manufacturing_parents_50plus_new_only.csv")
OUT_UPLOAD = Path(
    "data/output/elevator_manufacturing_parents_50plus_new_only_for_upload.csv"
)
OUT.parent.mkdir(parents=True, exist_ok=True)

# Apollo employee range strings — exclude micro firms (<50)
EMPLOYEE_RANGES = [
    "51,200",
    "201,500",
    "501,1000",
    "1001,5000",
    "5001,10000",
    "10001,20000",
    "20001,50000",
    "50001,100000",
]

SUBSIDIARY_NAME_RE = re.compile(
    r"(?ix)\b("
    r"india|brasil|brazil|china|japan|korea|italia|italy|france|spain|"
    r"deutschland|germany|switzerland|usa|america|canada|australia|"
    r"mexico|chile|argentina|colombia|peru|egypt|uae|saudi|turkey|russia|"
    r"singapore|malaysia|indonesia|thailand|vietnam|philippines|taiwan|"
    r"hong\s*kong|south\s*africa|nigeria|kenya|poland|romania|"
    r"netherlands|belgium|austria|sweden|norway|denmark|finland|portugal|"
    r"middle\s*east|apac|emea|latam|asia\s*pacific|north\s*america|"
    r"south\s*america|of\s+america|pvt\.?\s*ltd|private\s+limited|ltda"
    r")\b|"
    r"\(\s*(india|brazil|china|usa|uk|italy|spain|france|germany|"
    r"switzerland|canada|australia|korea|japan)\s*\)"
)

KNOWN_BRAND_CC_RE = re.compile(
    r"^(otis|schindler|kone|fujitec|tkelevator|tk-elevator|hyundai.?elevator)\.[a-z.]{2,}$",
    re.I,
)
KNOWN_BRAND_PREFIXES = (
    "otis.",
    "schindler.",
    "tkelevator.",
    "tk-elevator.",
    "kone.",
    "fujitec.",
    "hyundaielevator.",
)


def domain_of(org: dict) -> str:
    domain = (org.get("primary_domain") or "").strip().lower()
    if not domain and org.get("website_url"):
        try:
            domain = normalize_domain(org["website_url"])
        except DomainNormalizationError:
            domain = ""
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def is_known_brand_regional(name: str, domain: str) -> bool:
    if domain and (
        any(domain.startswith(p) for p in KNOWN_BRAND_PREFIXES)
        or KNOWN_BRAND_CC_RE.match(domain)
    ):
        # Keep only the main corporate domains if they somehow appear.
        if domain in {
            "otis.com",
            "schindler.com",
            "tkelevator.com",
            "kone.com",
            "fujitec.com",
            "hyundaielevator.co.kr",
        }:
            return False
        return True
    if re.search(
        r"(?i)\b(schindler|otis|kone|fujitec|tk elevator|hyundai elevator)\b", name
    ) and re.search(r"(?i)\b(s\.?a\.?|gmbh|ag|ltd|llc|pvt)\b", name):
        if domain and domain not in {
            "otis.com",
            "schindler.com",
            "tkelevator.com",
            "kone.com",
            "fujitec.com",
            "hyundaielevator.co.kr",
        }:
            return True
    return False


def main() -> None:
    settings = get_settings()
    api_key = settings.apollo_api_key.strip()
    if not api_key:
        raise SystemExit("APOLLO_API_KEY is not set")

    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": api_key,
    }

    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as session:
        companies = list(session.scalars(select(Company)).all())

    existing_domains: set[str] = set()
    existing_names: set[str] = set()
    for c in companies:
        if c.domain and not is_name_lookup_domain(c.domain):
            existing_domains.add(c.domain.strip().lower())
        if c.website:
            try:
                existing_domains.add(normalize_domain(c.website))
            except DomainNormalizationError:
                pass
        if c.name:
            existing_names.add(c.name.strip().casefold())
        if c.lookup_name:
            existing_names.add(c.lookup_name.strip().casefold())

    def search_page(page: int) -> dict:
        payload = {
            "q_organization_keyword_tags": ["elevator manufacturing"],
            "organization_num_employees_ranges": EMPLOYEE_RANGES,
            "page": page,
            "per_page": PER_PAGE,
        }
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(
                "https://api.apollo.io/api/v1/mixed_companies/search",
                headers=headers,
                json=payload,
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Apollo HTTP {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    seen_domains: set[str] = set()
    seen_names: set[str] = set()
    kept: list[dict[str, str]] = []
    stats = {
        "scanned": 0,
        "has_parent": 0,
        "regional_name": 0,
        "brand_regional": 0,
        "in_db": 0,
        "name_is_domain": 0,
        "kept": 0,
    }

    page = 1
    max_pages = 15
    while len(kept) < TARGET and page <= max_pages:
        data = search_page(page)
        orgs = data.get("organizations") or data.get("accounts") or []
        total = int(
            data.get("pagination", {}).get("total_entries")
            or data.get("total_entries")
            or 0
        )
        print(f"page={page} got={len(orgs)} total={total} kept={len(kept)}")
        if page == 1:
            print(f"apollo_total_matching_employee_filter={total}")
        if not orgs:
            break

        for org in orgs:
            if not isinstance(org, dict):
                continue
            stats["scanned"] += 1
            name = (org.get("name") or "").strip()
            if not name:
                continue
            if org.get("owned_by_organization_id") or org.get("owned_by_organization"):
                stats["has_parent"] += 1
                continue
            if SUBSIDIARY_NAME_RE.search(name):
                stats["regional_name"] += 1
                continue
            if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", name.lower().replace(" ", "")):
                stats["name_is_domain"] += 1
                continue

            domain = domain_of(org)
            if is_known_brand_regional(name, domain):
                stats["brand_regional"] += 1
                continue

            name_key = name.casefold()
            if name_key in seen_names or (domain and domain in seen_domains):
                continue
            if name_key in existing_names or (domain and domain in existing_domains):
                stats["in_db"] += 1
                continue

            seen_names.add(name_key)
            if domain:
                seen_domains.add(domain)
            kept.append({"company_name": name, "company_domain": domain})
            stats["kept"] += 1
            if len(kept) >= TARGET:
                break

        if total and page * PER_PAGE >= total:
            break
        page += 1
        time.sleep(0.35)

    kept = kept[:TARGET]
    with OUT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["company_name", "company_domain"])
        writer.writeheader()
        writer.writerows(kept)

    with OUT_UPLOAD.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["company_name", "company_website"])
        writer.writeheader()
        for row in kept:
            website = f"https://{row['company_domain']}" if row["company_domain"] else ""
            writer.writerow(
                {"company_name": row["company_name"], "company_website": website}
            )

    print("STATS", stats)
    print(f"WROTE {len(kept)} -> {OUT.resolve()}")
    print(f"with_domain={sum(1 for r in kept if r['company_domain'])}")
    print("sample:")
    for row in kept[:15]:
        line = f" - {row['company_name']} | {row['company_domain']}"
        print(line.encode("ascii", "replace").decode("ascii"))


if __name__ == "__main__":
    main()
