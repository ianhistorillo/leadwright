"""Company website / domain normalization helpers."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

_DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)

# Synthetic domains for CSV rows that only provide company_name (no website).
NAME_LOOKUP_DOMAIN_SUFFIX = "name-lookup.local"


class DomainNormalizationError(ValueError):
    """Raised when a website or domain value cannot be normalized."""


def normalize_domain(value: str | None) -> str:
    """
    Normalize a company website URL or bare domain to a canonical domain.

    Examples:
        https://company.com       -> company.com
        http://company.com/       -> company.com
        www.company.com           -> company.com
        company.com               -> company.com
        https://www.acme.io/path  -> acme.io
    """
    if value is None:
        raise DomainNormalizationError("Value is required")

    raw = str(value).strip()
    if not raw:
        raise DomainNormalizationError("Value is empty")

    # Reject spaces and obvious junk early.
    if any(ch.isspace() for ch in raw):
        raise DomainNormalizationError(f"Malformed value contains whitespace: {value!r}")

    candidate = raw
    if "://" not in candidate:
        # Bare domain / host — parse as a network location.
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise DomainNormalizationError(f"Could not extract host from: {value!r}")

    if host.startswith("www."):
        host = host[4:]

    if host.endswith("."):
        host = host.rstrip(".")

    if not host or "." not in host:
        raise DomainNormalizationError(f"Invalid domain: {value!r}")

    if not _DOMAIN_RE.match(host):
        raise DomainNormalizationError(f"Malformed domain: {value!r}")

    return host


def website_from_domain(domain: str) -> str:
    """Build a canonical https website URL from a normalized domain."""
    return f"https://{domain}"


def is_name_lookup_domain(domain: str | None) -> bool:
    """True when domain is a synthetic placeholder for name-only company rows."""
    host = (domain or "").strip().lower()
    return host.endswith(f".{NAME_LOOKUP_DOMAIN_SUFFIX}") or host == NAME_LOOKUP_DOMAIN_SUFFIX


def lookup_domain_for_name(name: str) -> str:
    """
    Stable synthetic domain for a CSV company_name used only as a lookup key.

    Not a real website — Apollo Search should use lookup_name / q_organization_name.
    """
    normalized = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not normalized:
        raise DomainNormalizationError("Company name is empty")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"lookup-{digest}.{NAME_LOOKUP_DOMAIN_SUFFIX}"


_MULTI_PART_TLDS = frozenset(
    {
        "co.uk",
        "com.au",
        "co.jp",
        "com.br",
        "co.in",
        "com.mx",
        "co.nz",
        "com.sg",
        "co.kr",
        "com.tw",
        "co.th",
        "com.hk",
        "com.cn",
        "co.il",
        "com.tr",
        "co.za",
    }
)


def company_name_from_domain(domain: str) -> str:
    """
    Derive a readable company label from a domain when CSV has no company_name.

    Examples:
        ge.com                 -> Ge
        sw.siemens.com         -> Siemens
        mitsubishielectric.co.jp -> Mitsubishielectric
        global.abb             -> Abb
    """
    host = (domain or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    host = host.strip(".")
    if not host:
        return ""

    parts = [p for p in host.split(".") if p]
    if not parts:
        return ""

    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_PART_TLDS:
        label = parts[-3]
    elif len(parts) >= 2:
        label = parts[-2]
    else:
        label = parts[0]

    return label.replace("-", " ").replace("_", " ").title()
