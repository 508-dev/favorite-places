"""Safe correlation fields for scraper logs."""

from hashlib import sha256


def url_log_fields(
    source_url: str | None,
    resolved_url: str | None = None,
) -> dict[str, str | None]:
    """Correlate events without logging query strings, URL paths, or credentials."""
    return {
        "source_url_hash": sha256(source_url.encode()).hexdigest() if source_url else None,
        "resolved_url_hash": sha256(resolved_url.encode()).hexdigest() if resolved_url else None,
    }
