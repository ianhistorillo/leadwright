"""
Configurable job-title profiles for employee search.

Business logic should resolve titles via these helpers / settings — never
hard-code title lists inside search orchestration.
"""

from __future__ import annotations

from app.config import Settings, get_settings

# Named profiles for future use. "custom" always defers to Settings.target_job_titles.
JOB_TITLE_PROFILES: dict[str, list[str]] = {
    "executive": [
        "CEO",
        "Founder",
        "Co-Founder",
        "President",
        "COO",
        "CFO",
        "CTO",
        "Chief Executive Officer",
        "Chief Operating Officer",
        "Chief Financial Officer",
        "Chief Technology Officer",
    ],
    "sales": [
        "VP Sales",
        "Head of Sales",
        "Chief Revenue Officer",
        "CRO",
        "Director of Sales",
        "Sales Director",
        "VP of Sales",
    ],
    "marketing": [
        "VP Marketing",
        "CMO",
        "Head of Marketing",
        "Chief Marketing Officer",
        "Director of Marketing",
        "Marketing Director",
    ],
    "technology": [
        "CTO",
        "VP Engineering",
        "Head of Engineering",
        "Chief Technology Officer",
        "VP of Engineering",
        "Director of Engineering",
    ],
    "finance": [
        "CFO",
        "VP Finance",
        "Head of Finance",
        "Chief Financial Officer",
        "Controller",
        "VP of Finance",
    ],
}


def list_job_title_profiles() -> list[str]:
    """Return available profile names including custom."""
    return sorted([*JOB_TITLE_PROFILES.keys(), "custom"])


def resolve_job_titles(
    profile: str | None = None,
    *,
    titles: list[str] | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """
    Resolve the job-title filter list.

    Precedence:
    1. Explicit `titles` override
    2. Named profile (except `custom`)
    3. Settings.target_job_titles (default / custom)
    """
    if titles:
        cleaned = [title.strip() for title in titles if title and title.strip()]
        if not cleaned:
            raise ValueError("titles override is empty")
        return cleaned

    cfg = settings or get_settings()
    selected = (profile or "custom").strip().lower()

    if selected == "custom":
        resolved = [title.strip() for title in cfg.target_job_titles if title.strip()]
    elif selected in JOB_TITLE_PROFILES:
        resolved = list(JOB_TITLE_PROFILES[selected])
    else:
        raise ValueError(
            f"Unknown job title profile {profile!r}. "
            f"Available: {', '.join(list_job_title_profiles())}"
        )

    if not resolved:
        raise ValueError("Resolved job title list is empty")
    return resolved
