"""Email normalization helpers."""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._+-]*[a-z0-9])?@"
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
)


def normalize_email(value: str | None) -> str | None:
    """
    Normalize an email to lowercase trimmed form, or None if invalid.

    Examples:
        \"  Jane.Doe@Example.COM \" -> jane.doe@example.com
        \"not-an-email\" -> None
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or any(ch.isspace() for ch in text):
        return None
    if text.count("@") != 1:
        return None
    local, domain = text.split("@", 1)
    if not local or not domain:
        return None
    if not _EMAIL_RE.match(text):
        return None
    return text
