"""Guess work-email candidates from a person's name and company domain.

Used by search-only runs (0 Apollo enrichment credits). Guesses are not verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.utils.email import normalize_email

_OBFUSCATED_RE = re.compile(r"[*•…]")
_NON_LOCAL_RE = re.compile(r"[^a-z0-9._]")


@dataclass(frozen=True)
class EmailGuessResult:
    """Primary guess plus alternate patterns for manual review."""

    primary: str | None
    candidates: list[str]
    reason: str | None = None


def _clean_name_part(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().lower()
    if not text or _OBFUSCATED_RE.search(text):
        return None
    # Drop punctuation / spaces from nicknames like "J.R."
    text = re.sub(r"[^a-z\-'\s]", "", text)
    text = text.replace("'", "").replace("-", "")
    text = re.sub(r"\s+", "", text)
    return text or None


def split_person_name(
    *,
    first_name: str | None,
    last_name: str | None,
    full_name: str | None,
) -> tuple[str | None, str | None]:
    """
    Resolve first/last for guessing.

    Ignores obfuscated Apollo last names (e.g. Lo***e). Falls back to parsing
    a clean full_name when last_name is missing.
    """
    first = _clean_name_part(first_name)
    last = _clean_name_part(last_name)

    if first and last:
        return first, last

    if full_name and not _OBFUSCATED_RE.search(full_name):
        parts = [p for p in re.split(r"\s+", full_name.strip()) if p]
        if len(parts) >= 2:
            parsed_first = _clean_name_part(parts[0])
            parsed_last = _clean_name_part(parts[-1])
            return first or parsed_first, last or parsed_last
        if len(parts) == 1:
            return first or _clean_name_part(parts[0]), last

    return first, last


def _local_part(value: str) -> str:
    return _NON_LOCAL_RE.sub("", value)


def guess_work_emails(
    *,
    domain: str,
    first_name: str | None = None,
    last_name: str | None = None,
    full_name: str | None = None,
) -> EmailGuessResult:
    """
    Build common corporate email patterns for a person at ``domain``.

    Patterns (when enough name parts exist):
    - first.last
    - first
    - flast
    - first_last
    - f.last
    - firstlast
    """
    domain_norm = (domain or "").strip().lower().lstrip("@")
    if domain_norm.startswith("www."):
        domain_norm = domain_norm[4:]
    if not domain_norm or "." not in domain_norm:
        return EmailGuessResult(primary=None, candidates=[], reason="invalid_domain")

    first, last = split_person_name(
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
    )
    if not first:
        return EmailGuessResult(
            primary=None,
            candidates=[],
            reason="insufficient_name_for_email_guess",
        )

    locals_: list[str] = []
    if last:
        locals_.extend(
            [
                f"{first}.{last}",
                first,
                f"{first[0]}{last}",
                f"{first}_{last}",
                f"{first[0]}.{last}",
                f"{first}{last}",
            ]
        )
    else:
        locals_.append(first)

    candidates: list[str] = []
    seen: set[str] = set()
    for local in locals_:
        cleaned = _local_part(local)
        if not cleaned:
            continue
        email = normalize_email(f"{cleaned}@{domain_norm}")
        if email and email not in seen:
            seen.add(email)
            candidates.append(email)

    if not candidates:
        return EmailGuessResult(
            primary=None,
            candidates=[],
            reason="insufficient_name_for_email_guess",
        )
    return EmailGuessResult(primary=candidates[0], candidates=candidates)
