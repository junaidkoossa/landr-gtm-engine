"""Creator evidence for TikTok through Bright Data's Web Scraper API.

Bright Data collects public TikTok data and bills per record (the free tier is a
monthly allowance of records). To stay inside it, collection is in two steps:

1. Profiles (1 record per creator): followers, likes, video count, bio.
2. Recent posts (TIKTOK_POSTS_PER_PROFILE records per creator), only for profiles with
   at least TIKTOK_POSTS_MIN_FOLLOWERS followers. Posts give per-video plays, which
   the steady-views score needs.

Each step is a Bright Data job: trigger it, check progress until it's ready, then
download the results. The pipeline starts the profile job at the beginning of a run and
collects other creators while it runs.

Needs BRIGHTDATA_API_TOKEN. Bright Data scrapes TikTok rather than using an official
API, so TikTok's terms of service apply to how the data is used.
"""
import logging
import os
import time
from datetime import date, datetime, timezone

import requests

from src.scraper import REQUEST_TIMEOUT, find_urls
from src.settings import (
    BRIGHTDATA_MAX_WAIT_SECONDS,
    BRIGHTDATA_POLL_SECONDS,
    TIKTOK_POSTS_MIN_FOLLOWERS,
    TIKTOK_POSTS_PER_PROFILE,
)
from src.social import tiktok_url
from src.youtube import looks_like_release

logger = logging.getLogger(__name__)

API_URL = "https://api.brightdata.com/datasets/v3"
PROFILES_DATASET = "gd_l1villgoiiidt09ci"  # TikTok profiles, collect by URL
POSTS_DATASET = "gd_lu702nij2f790tmv9h"  # TikTok posts, discover by profile URL
MAX_DESCRIPTION_LENGTH = 300
MAX_POSTS_LISTED = 25


def api_token() -> str | None:
    return os.getenv("BRIGHTDATA_API_TOKEN", "").strip() or None


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def trigger(token: str, dataset_id: str, inputs: list[dict], **params: str) -> str:
    """Start a collection job; returns its snapshot ID."""
    response = requests.post(
        f"{API_URL}/trigger",
        params={"dataset_id": dataset_id, "include_errors": "true", **params},
        json=inputs,
        headers=_headers(token),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    snapshot_id = response.json().get("snapshot_id")
    if not snapshot_id:
        raise ValueError(f"Bright Data didn't return a snapshot ID: {response.text[:200]}")
    return snapshot_id


def is_ready(token: str, snapshot_id: str) -> bool:
    response = requests.get(f"{API_URL}/progress/{snapshot_id}", headers=_headers(token), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    status = response.json().get("status")
    if status == "failed":
        raise ValueError(f"Bright Data job {snapshot_id} failed")
    return status == "ready"


def download(token: str, snapshot_id: str) -> list[dict]:
    response = requests.get(
        f"{API_URL}/snapshot/{snapshot_id}",
        params={"format": "json"},
        headers=_headers(token),
        timeout=REQUEST_TIMEOUT * 4,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def wait_and_download(token: str, snapshot_id: str, poll_seconds: float = BRIGHTDATA_POLL_SECONDS) -> list[dict]:
    deadline = time.monotonic() + BRIGHTDATA_MAX_WAIT_SECONDS
    while not is_ready(token, snapshot_id):
        if time.monotonic() > deadline:
            raise ValueError(f"Bright Data job {snapshot_id} wasn't ready after {BRIGHTDATA_MAX_WAIT_SECONDS}s")
        logger.info("Waiting for Bright Data job %s", snapshot_id)
        time.sleep(poll_seconds)
    return download(token, snapshot_id)


def _handle_of(url: str) -> str:
    """The lowercase handle in a tiktok.com/@handle URL."""
    for part in (url or "").split("?")[0].split("/"):
        if part.startswith("@"):
            return part[1:].lower()
    return ""


def _date(value: object) -> str:
    """A post's date as YYYY-MM-DD, from an ISO string or a Unix timestamp."""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
    return str(value or "")[:10]


def estimate_year(
    post_dates: list[str], release_count: int, sample_size: int, account_created: str = "",
    today: date | None = None,
) -> dict[str, object] | None:
    """Estimate a year of releases from the posting rate in a sample of recent posts.

    Only used when the sample was cut off (it holds TIKTOK_POSTS_PER_PROFILE posts) before
    reaching back a year: release posts per day in the sample x 365 (or the account's age,
    if younger). Months with a release assume releases are spread evenly.
    """
    today = today or datetime.now(timezone.utc).date()
    dates = sorted(d for d in post_dates if d)
    if sample_size < TIKTOK_POSTS_PER_PROFILE or not dates:
        return None
    span = max((today - date.fromisoformat(dates[0])).days, 1)
    if span >= 365:
        return None  # the sample already covers the year
    window = 365
    if account_created:
        window = min(window, max((today - date.fromisoformat(account_created)).days, span))
    releases = round(release_count / span * window)
    months = min(round(window / 30.4), releases, 12)
    return {
        "releases_last_12_months": releases,
        "active_months_last_12": months,
        "factor": releases / release_count if release_count else 1.0,
        "sample_days": span,
    }


def _profile_part(profile: dict, posts: list[dict] | None) -> dict[str, object]:
    handle = _handle_of(profile.get("url", "")) or profile.get("account_id", "")
    lines = [
        f"TikTok: @{handle} ({profile.get('nickname', '')})",
        f"Followers: {profile.get('followers', 'unknown')}; videos: {profile.get('videos_count', 'unknown')}; "
        f"total likes: {profile.get('likes', 'unknown')}",
        profile.get("biography", ""),
    ]
    links = find_urls(profile.get("biography", ""))
    if profile.get("bio_link"):
        links.append(profile["bio_link"] if "://" in profile["bio_link"] else f"https://{profile['bio_link']}")

    release_dates, releases = [], []
    if posts is None:
        lines.append(
            f"Recent posts not collected (under {TIKTOK_POSTS_MIN_FOLLOWERS:,} followers), so no per-video plays."
        )
    for post in posts or []:
        description = post.get("description") or ""
        posted = _date(post.get("create_time"))
        links.extend(find_urls(description))
        if not looks_like_release(description) or not posted:
            continue
        plays = post.get("play_count")
        release_dates.append(posted)
        releases.append({"date": posted, "views": int(plays) if plays is not None else None})
        if len(releases) <= MAX_POSTS_LISTED:
            shown = f"{int(plays):,} plays" if plays is not None else "plays unknown"
            lines.append(f"Release post {posted} ({shown}): {description[:MAX_DESCRIPTION_LENGTH]}")

    estimate = None
    if posts:
        estimate = estimate_year(
            [_date(post.get("create_time")) for post in posts],
            len(release_dates),
            len(posts),
            _date(profile.get("create_time")) if profile.get("create_time") else "",
        )
    if estimate:
        lines.append(
            f"Estimated from posting rate (last {estimate['sample_days']} days of posts): about "
            f"{estimate['releases_last_12_months']} release posts a year, in about "
            f"{estimate['active_months_last_12']} of 12 months."
        )

    followers = profile.get("followers")
    return {
        "estimate": estimate,
        "source": "tiktok",
        "title": profile.get("nickname") or f"@{handle}",
        "text": "\n".join(line for line in lines if line),
        "links": links,
        "audience_size": int(followers) if followers is not None else None,
        "release_count": len(release_dates) if posts is not None else None,
        "release_dates": release_dates,
        "releases": releases,
    }


class TikTokJobs:
    """The profile and posts jobs for one pipeline run."""

    def __init__(self, token: str, poll_seconds: float = BRIGHTDATA_POLL_SECONDS):
        self.token = token
        self.poll_seconds = poll_seconds
        self.handles: list[str] = []
        self.snapshot_id: str | None = None

    def start(self, handles: list[str]) -> None:
        """Trigger the profile job (returns straight away)."""
        self.handles = sorted(set(handles))
        if self.handles:
            self.snapshot_id = trigger(self.token, PROFILES_DATASET, [{"url": tiktok_url(h)} for h in self.handles])
            logger.info("Started Bright Data TikTok profile job %s (%d profiles)", self.snapshot_id, len(self.handles))

    def finish(self) -> dict[str, dict[str, object] | Exception]:
        """Wait for profiles, collect posts for the bigger ones, and return a part (or error) per handle."""
        if not self.snapshot_id:
            return {}
        profiles: dict[str, dict] = {}
        errors: dict[str, Exception] = {}
        for record in wait_and_download(self.token, self.snapshot_id, self.poll_seconds):
            handle = _handle_of(record.get("url") or (record.get("input") or {}).get("url", ""))
            if not handle:
                continue
            if record.get("error") or record.get("is_private"):
                errors[handle] = ValueError(f"TikTok profile @{handle} unavailable: {record.get('error') or 'private account'}")
            else:
                profiles[handle] = record

        big = [h for h, p in profiles.items() if (p.get("followers") or 0) >= TIKTOK_POSTS_MIN_FOLLOWERS]
        posts: dict[str, list[dict]] = {h: [] for h in big}
        if big:
            snapshot_id = trigger(
                self.token,
                POSTS_DATASET,
                [{"url": tiktok_url(h), "num_of_posts": TIKTOK_POSTS_PER_PROFILE} for h in big],
                type="discover_new",
                discover_by="profile_url",
            )
            logger.info("Started Bright Data TikTok posts job %s (%d profiles)", snapshot_id, len(big))
            for record in wait_and_download(self.token, snapshot_id, self.poll_seconds):
                handle = _handle_of(record.get("profile_url") or "") or str(record.get("profile_username") or "").lower()
                if handle in posts and not record.get("error"):
                    posts[handle].append(record)

        results: dict[str, dict[str, object] | Exception] = dict(errors)
        for handle, profile in profiles.items():
            results[handle] = _profile_part(profile, posts.get(handle) if handle in posts else None)
        for handle in self.handles:
            results.setdefault(handle, ValueError(f"Bright Data returned nothing for TikTok @{handle}"))
        return results
