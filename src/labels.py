"""Label evidence from MusicBrainz (free, no key) and Discogs (free, needs DISCOGS_TOKEN).

Labels are matched by name (see src/matching.py): an exact match is used as is; a
variant (plural or filler-word difference) is used and flagged for review; anything
else is no match. Both services are rate limited, so calls are spaced out:
MusicBrainz allows about 1 request a second, Discogs 60 a minute.

Neither service returns a label's releases in date order, so every page is read (up to
MAX_RELEASE_PAGES pages of 100) and the releases are then sorted newest to oldest. A
label with more releases than that is marked as partially read.
"""
import logging
import os
import time
from datetime import date

import requests

from src.matching import best_match
from src.scraper import REQUEST_TIMEOUT
from src.settings import DISCOGS_DATED_RELEASES, MAX_RELEASE_PAGES

logger = logging.getLogger(__name__)

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2"
DISCOGS_URL = "https://api.discogs.com"
PAGE_SIZE = 100
MIN_SECONDS_BETWEEN_CALLS = 1.1


def user_agent() -> str:
    # MusicBrainz and Discogs both ask for a User-Agent that says how to reach you.
    contact = os.getenv("DATA_SOURCE_CONTACT", "").strip()
    return f"landr-gtm-engine/1.0 ( {contact} )" if contact else "landr-gtm-engine/1.0"


class Throttle:
    def __init__(self, interval: float = MIN_SECONDS_BETWEEN_CALLS):
        self.interval = interval
        self.last_call = 0.0

    def wait(self) -> None:
        delay = self.last_call + self.interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.last_call = time.monotonic()


musicbrainz_throttle = Throttle()
discogs_throttle = Throttle()


def _match_info(source: str, wanted: str, matched: str, kind: str, url: str) -> dict:
    return {
        "source": source,
        "searched_name": wanted,
        "matched_name": matched,
        "match_type": kind,
        "exact": kind == "exact",
        "matched_url": url,
    }


def _match_note(match: dict) -> str:
    if match["exact"]:
        return ""
    return f" (name variant of {match['searched_name']!r}; flagged for review)"


def _coverage_note(source: str, name: str, read: int, total: int) -> str:
    if total > read:
        logger.info("%s: read %d of %d %s releases (page limit)", name, read, total, source)
        return f"Releases listed: {total} (read {read}; latest dates may be incomplete)"
    return f"Releases listed: {total}"


def _musicbrainz_get(path: str, params: dict[str, object]) -> dict:
    musicbrainz_throttle.wait()
    response = requests.get(
        f"{MUSICBRAINZ_URL}/{path}",
        params={**params, "fmt": "json"},
        headers={"User-Agent": user_agent()},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def fetch_musicbrainz_label(name: str) -> dict[str, object] | None:
    """The label's links and release history, or None if no label name is close enough."""
    escaped = name.replace('"', '\\"')
    search = _musicbrainz_get("label", {"query": f'label:"{escaped}"', "limit": 10})
    found = best_match(name, [(label.get("name", ""), label) for label in search.get("labels", [])])
    if found is None:
        return None
    matched_name, candidate, kind = found
    label_id = candidate["id"]

    label = _musicbrainz_get(f"label/{label_id}", {"inc": "url-rels"})
    links = [
        relation["url"]["resource"]
        for relation in label.get("relations", [])
        if relation.get("url", {}).get("resource")
    ]

    release_list, total = [], 0
    for page in range(MAX_RELEASE_PAGES):
        data = _musicbrainz_get(
            "release", {"label": label_id, "limit": PAGE_SIZE, "offset": page * PAGE_SIZE}
        )
        batch = data.get("releases", [])
        total = data.get("release-count", total)
        release_list.extend(batch)
        if not batch or len(release_list) >= total:
            break
    # Newest to oldest. Partial dates ("2019", "2019-05") sort before full dates of the same year.
    release_list.sort(key=lambda r: r.get("date") or "", reverse=True)
    release_dates = [r["date"] for r in release_list if r.get("date")]

    match = _match_info(
        "musicbrainz", name, matched_name, kind, f"https://musicbrainz.org/label/{label_id}"
    )
    lines = [
        f"MusicBrainz label: {label.get('name', matched_name)}{_match_note(match)}",
        f"Type: {label.get('type') or 'unknown'}; country: {label.get('country') or 'unknown'}",
        f"Active since: {(label.get('life-span') or {}).get('begin') or 'unknown'}",
        _coverage_note("MusicBrainz", name, len(release_list), total),
    ]
    for release in release_list[:20]:
        lines.append(f"Release {release.get('date') or 'undated'}: {release.get('title', '')}")

    return {
        "source": "musicbrainz",
        "title": label.get("name", matched_name),
        "text": "\n".join(lines),
        "links": links,
        "audience_size": None,
        "release_count": total or len(release_list),
        "release_dates": release_dates,
        "match": match,
    }


def _discogs_get(path: str, params: dict[str, object], token: str) -> dict:
    discogs_throttle.wait()
    response = requests.get(
        f"{DISCOGS_URL}/{path}",
        params=params,
        headers={"User-Agent": user_agent(), "Authorization": f"Discogs token={token}"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _released(value: str) -> str:
    """Discogs dates look like 2025-03-14, 2025-03-00 or 2025; keep the known part."""
    parts = [part for part in (value or "").split("-") if part and part.strip("0")]
    return "-".join(parts[:3]) if len(parts) > 1 else ""


def _add_exact_dates(release_list: list[dict], token: str) -> None:
    """Look up the exact date of the newest releases from this year or last year."""
    oldest_useful = date.today().year - 1
    checked = 0
    for release in release_list:
        if checked >= DISCOGS_DATED_RELEASES or (release.get("year") or 0) < oldest_useful:
            break
        if release.get("type", "release") != "release" or not release.get("id"):
            continue
        checked += 1
        try:
            details = _discogs_get(f"releases/{release['id']}", {}, token)
        except requests.RequestException as error:
            logger.info("Couldn't read Discogs release %s: %s", release["id"], error)
            continue
        exact = _released(str(details.get("released") or ""))
        if exact:
            release["released"] = exact


def fetch_discogs_label(name: str, token: str) -> dict[str, object] | None:
    """The label's profile, links and release years, or None if no label name is close enough."""
    search = _discogs_get("database/search", {"q": name, "type": "label", "per_page": 10}, token)
    found = best_match(name, [(result.get("title", ""), result) for result in search.get("results", [])])
    if found is None:
        return None
    matched_name, candidate, kind = found
    label_id = candidate["id"]

    label = _discogs_get(f"labels/{label_id}", {}, token)
    release_list, total = [], 0
    for page in range(1, MAX_RELEASE_PAGES + 1):
        data = _discogs_get(
            f"labels/{label_id}/releases",
            {"per_page": PAGE_SIZE, "page": page, "sort": "year", "sort_order": "desc"},
            token,
        )
        pagination = data.get("pagination") or {}
        total = pagination.get("items", total)
        release_list.extend(data.get("releases", []))
        if page >= pagination.get("pages", page):
            break
    release_list.sort(key=lambda r: r.get("year") or 0, reverse=True)
    _add_exact_dates(release_list, token)
    years = [r.get("released") or str(r["year"]) for r in release_list if r.get("year")]
    years.sort(reverse=True)

    match = _match_info(
        "discogs", name, matched_name, kind, f"https://www.discogs.com/label/{label_id}"
    )
    lines = [
        f"Discogs label: {label.get('name', matched_name)}{_match_note(match)}",
        (label.get("profile") or "")[:1500],
        _coverage_note("Discogs", name, len(release_list), total),
    ]
    for release in release_list[:20]:
        lines.append(
            f"Release {release.get('released') or release.get('year') or 'undated'}: {release.get('artist', '')} - {release.get('title', '')}"
        )

    return {
        "source": "discogs",
        "title": label.get("name", matched_name),
        "text": "\n".join(lines),
        "links": list(label.get("urls") or []),
        "audience_size": None,
        "release_count": total or len(release_list),
        "release_dates": years,
        "match": match,
    }
