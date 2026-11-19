"""Email finders that answer during the run: Apollo and Hunter (ENRICHMENT_PROVIDER).

Both look a lead up by the domain of its own website (or storefront), so leads with only
YouTube, Bandcamp, social or similar links can't be looked up and are marked no_domain.
They find emails only, so a lead's score doesn't change. Each lookup can use a credit
on your plan.

- Apollo: People Enrichment (POST /api/v1/people/match) with the lead's name and domain,
  APOLLO_API_KEY. Personal emails aren't requested.
- Hunter: Domain Search (GET /v2/domain-search), HUNTER_API_KEY; the email Hunter is
  most confident about is used.
"""
import os
import time
from urllib.parse import urlparse

import requests

from src.settings import EMAIL_LOOKUPS_PER_SECOND

TIMEOUT = 20
APOLLO_URL = "https://api.apollo.io/api/v1/people/match"
HUNTER_URL = "https://api.hunter.io/v2/domain-search"
API_KEY_SETTINGS = {"apollo": "APOLLO_API_KEY", "hunter": "HUNTER_API_KEY"}
# Hosts that belong to a platform, not to the lead, so they say nothing about its email.
PLATFORM_DOMAINS = (
    "youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "fb.com",
    "x.com", "twitter.com", "threads.net", "snapchat.com", "linktr.ee", "bandcamp.com",
    "soundcloud.com", "spotify.com", "apple.com", "beatstars.com", "gumroad.com",
    "splice.com", "patreon.com", "payhip.com", "myshopify.com", "bit.ly", "discogs.com",
    "musicbrainz.org", "beatport.com", "traxsource.com", "loopmasters.com",
)


class EmailLookupError(Exception):
    """The lookup itself failed (network, bad key, out of credits); the lead can be tried again."""


def api_key(provider: str) -> str | None:
    return os.getenv(API_KEY_SETTINGS[provider]) or None


def lead_domain(row: dict[str, object]) -> str | None:
    """The domain of the lead's own website or storefront, or None if it only has platform links."""
    for column in ("website_url", "storefront_url"):
        url = str(row.get(column) or "").strip()
        if not url:
            continue
        host = (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
        host = host.removeprefix("www.")
        if host and not any(host == d or host.endswith(f".{d}") for d in PLATFORM_DOMAINS):
            return host
    return None


def _get_json(response: requests.Response, provider: str) -> dict:
    if response.status_code in (401, 403):
        raise EmailLookupError(f"{API_KEY_SETTINGS[provider]} was rejected ({response.status_code})")
    if response.status_code == 429:
        raise EmailLookupError(f"{provider} rate limit or credit limit reached")
    if response.status_code == 404:
        return {}
    try:
        response.raise_for_status()
        return response.json() or {}
    except (requests.RequestException, ValueError) as error:
        raise EmailLookupError(f"{provider} lookup failed: {error}") from error


def find_with_apollo(row: dict[str, object], key: str) -> str | None:
    domain = lead_domain(row)
    try:
        response = requests.post(
            APOLLO_URL,
            json={"name": str(row.get("name") or ""), "domain": domain, "reveal_personal_emails": False},
            headers={"x-api-key": key, "Content-Type": "application/json", "Cache-Control": "no-cache"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as error:
        raise EmailLookupError(f"apollo lookup failed: {error}") from error
    person = _get_json(response, "apollo").get("person") or {}
    email = str(person.get("email") or "").strip()
    # Apollo returns a placeholder when the email exists but isn't unlocked on the plan.
    return email if "@" in email and "not_unlocked" not in email else None


def find_with_hunter(row: dict[str, object], key: str) -> str | None:
    try:
        response = requests.get(
            HUNTER_URL, params={"domain": lead_domain(row), "api_key": key, "limit": 10}, timeout=TIMEOUT
        )
    except requests.RequestException as error:
        raise EmailLookupError(f"hunter lookup failed: {error}") from error
    emails = (_get_json(response, "hunter").get("data") or {}).get("emails") or []
    found = [e for e in emails if isinstance(e, dict) and "@" in str(e.get("value") or "")]
    if not found:
        return None
    return str(max(found, key=lambda e: e.get("confidence") or 0)["value"]).strip()


FINDERS = {"apollo": find_with_apollo, "hunter": find_with_hunter}


class EmailFinder:
    """Looks emails up one lead at a time, at most EMAIL_LOOKUPS_PER_SECOND."""

    def __init__(self, provider: str, key: str | None = None):
        self.provider = provider
        self.key = key or api_key(provider)
        if not self.key:
            raise RuntimeError(f"Set {API_KEY_SETTINGS[provider]} to find emails with {provider.title()}")
        self._last = 0.0

    def find(self, row: dict[str, object]) -> str | None:
        """The email, or None if the provider has none. Raises EmailLookupError if the lookup failed."""
        wait = 1 / EMAIL_LOOKUPS_PER_SECOND - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        return FINDERS[self.provider](row, self.key)
