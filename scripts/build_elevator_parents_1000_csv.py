"""Build parent-leaning elevator manufacturing CSV (up to 1000, exclude DB)."""

from __future__ import annotations

import csv
import re
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.database.models import Company
from app.utils.domain import DomainNormalizationError, is_name_lookup_domain, normalize_domain

TARGET = 1000
PER_PAGE = 100
MAX_PAGES_PER_QUERY = 20
OUT = ROOT / "data/output/elevator_manufacturing_parents_1000_new_only.csv"
OUT_UPLOAD = ROOT / "data/output/elevator_manufacturing_parents_1000_new_only_for_upload.csv"
OUT.parent.mkdir(parents=True, exist_ok=True)

# Complementary Apollo keyword searches (1 credit per page each).
KEYWORD_QUERIES: list[list[str]] = [
    ["elevator manufacturing"],
    ["elevator manufacturer"],
    ["lift manufacturer"],
    ["escalator manufacturer"],
    ["elevator"],
    ["elevators"],
    ["lift manufacturing"],
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
KNOWN_PARENT_ROOTS = {
    "otis.com",
    "schindler.com",
    "tkelevator.com",
    "kone.com",
    "fujitec.com",
    "hyundaielevator.co.kr",
}


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
    if domain in KNOWN_PARENT_ROOTS:
        return False
    if domain and (
        any(domain.startswith(p) for p in KNOWN_BRAND_PREFIXES)
        or KNOWN_BRAND_CC_RE.match(domain)
    ):
        return True
    if re.search(
        r"(?i)\b(schindler|otis|kone|fujitec|tk elevator|hyundai elevator)\b", name
    ) and re.search(r"(?i)\b(s\.?a\.?|gmbh|ag|ltd|llc|pvt)\b", name):
        if domain and domain not in KNOWN_PARENT_ROOTS:
            return True
    return False


def ascii_line(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def main() -> None:
    settings = get_settings()
    api_key = (settings.apollo_api_key or "").strip()
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
    for company in companies:
        if company.domain and not is_name_lookup_domain(company.domain):
            existing_domains.add(company.domain.strip().lower())
        if company.website:
            try:
                existing_domains.add(normalize_domain(company.website))
            except DomainNormalizationError:
                pass
        if company.name:
            existing_names.add(company.name.strip().casefold())
        if company.lookup_name:
            existing_names.add(company.lookup_name.strip().casefold())

    def search_page(keywords: list[str], page: int) -> dict:
        payload = {
            "q_organization_keyword_tags": keywords,
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
        "pages": 0,
        "scanned": 0,
        "has_parent": 0,
        "regional_name": 0,
        "brand_regional": 0,
        "name_is_domain": 0,
        "in_db": 0,
        "dupes": 0,
        "kept": 0,
    }

    for keywords in KEYWORD_QUERIES:
        if len(kept) >= TARGET:
            break
        page = 1
        while len(kept) < TARGET and page <= MAX_PAGES_PER_QUERY:
            data = search_page(keywords, page)
            stats["pages"] += 1
            orgs = data.get("organizations") or data.get("accounts") or []
            total = int(
                data.get("pagination", {}).get("total_entries")
                or data.get("total_entries")
                or 0
            )
            print(
                ascii_line(
                    f"keywords={keywords!r} page={page} got={len(orgs)} "
                    f"total={total} kept={len(kept)}"
                )
            )
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
                    stats["dupes"] += 1
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
    with OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["company_name", "company_domain"])
        writer.writeheader()
        writer.writerows(kept)

    with OUT_UPLOAD.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["company_name", "company_website"])
        writer.writeheader()
        for row in kept:
            website = f"https://{row['company_domain']}" if row["company_domain"] else ""
            writer.writerow(
                {"company_name": row["company_name"], "company_website": website}
            )

    print("STATS", stats)
    print(f"WROTE {len(kept)} -> {OUT}")
    print(f"with_domain={sum(1 for row in kept if row['company_domain'])}")
    print("sample:")
    for row in kept[:12]:
        print(ascii_line(f" - {row['company_name']} | {row['company_domain']}"))


if __name__ == "__main__":
    main()
