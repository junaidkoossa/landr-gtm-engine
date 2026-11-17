"""Creator evidence from Instagram's Business Discovery API (free, official).

Business Discovery lets your own Instagram professional account look up another
professional (Business or Creator) account: followers, post count and recent posts
with likes, comments and views. Personal and age-gated accounts return nothing.

Needs INSTAGRAM_ACCESS_TOKEN (a long-lived Facebook user token with instagram_basic,
instagram_manage_insights and pages_read_engagement) and INSTAGRAM_BUSINESS_ID (your
own Instagram professional account's ID). Calls are spaced out to stay under about
200 an hour (INSTAGRAM_CALLS_PER_HOUR).
"""
import os
from datetime import datetime, timedelta, timezone

import requests

from src.labels import Throttle
from src.scraper import REQUEST_TIMEOUT, find_urls
from src.settings import (
    GRAPH_API_VERSION,
    INSTAGRAM_CALLS_PER_HOUR,
    INSTAGRAM_MAX_PAGES,
    INSTAGRAM_MEDIA_LIMIT,
)
from src.youtube import looks_like_release

GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
MAX_CAPTION_LENGTH = 300
MAX_POSTS_LISTED = 25
# Graph API error codes that mean "slow down" rather than "this account can't be read".
RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}

throttle = Throttle(3600 / INSTAGRAM_CALLS_PER_HOUR)


class InstagramUnavailable(ValueError):
    """The account isn't a professional account, or doesn't exist; retrying won't help."""


def credentials() -> tuple[str, str] | None:
    token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
    business_id = os.getenv("INSTAGRAM_BUSINESS_ID", "").strip()
    return (token, business_id) if token and business_id else None


MEDIA_FIELDS = "{caption,timestamp,media_type,like_count,comments_count,view_count,permalink}"


def _fields(username: str, after: str | None = None) -> str:
    media = f"media.after({after}).limit({INSTAGRAM_MEDIA_LIMIT})" if after else f"media.limit({INSTAGRAM_MEDIA_LIMIT})"
    if after:  # later pages only need the posts
        return f"business_discovery.username({username}){{{media}{MEDIA_FIELDS}}}"
    return (
        f"business_discovery.username({username})"
        f"{{username,name,biography,website,followers_count,media_count,{media}{MEDIA_FIELDS}}}"
    )


def _discover(username: str, token: str, business_id: str, after: str | None = None) -> dict:
    throttle.wait()
    response = requests.get(
        f"{GRAPH_URL}/{business_id}",
        params={"fields": _fields(username, after), "access_token": token},
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code >= 400:
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        if error.get("code") in RATE_LIMIT_CODES or response.status_code >= 500:
            raise requests.HTTPError(f"Instagram rate limit or server error: {error.get('message', response.status_code)}")
        if error.get("code") == 190:
            raise RuntimeError(
                f"INSTAGRAM_ACCESS_TOKEN was rejected (expired or revoked; a system user token "
                f"doesn't expire, see the README): {error.get('message', '')}"
            )
        raise InstagramUnavailable(
            f"Instagram has no professional account @{username} "
            f"(personal, age-gated or missing): {error.get('message', response.status_code)}"
        )
    profile = response.json().get("business_discovery") or {}
    if not profile:
        raise InstagramUnavailable(f"Instagram returned nothing for @{username}")
    return profile


def fetch_instagram_profile(username: str, token: str, business_id: str) -> dict[str, object]:
    """One evidence part for an Instagram professional account."""
    profile = _discover(username, token, business_id)
    posts = list((profile.get("media") or {}).get("data") or [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()
    after = ((profile.get("media") or {}).get("paging") or {}).get("cursors", {}).get("after")
    for _ in range(INSTAGRAM_MAX_PAGES - 1):
        oldest = (posts[-1].get("timestamp") or "")[:10] if posts else ""
        if not after or (oldest and oldest < cutoff):
            break
        media = _discover(username, token, business_id, after).get("media") or {}
        posts.extend(media.get("data") or [])
        after = (media.get("paging") or {}).get("cursors", {}).get("after")

    lines = [
        f"Instagram: @{profile.get('username', username)} ({profile.get('name', '')})",
        f"Followers: {profile.get('followers_count', 'unknown')}; posts: {profile.get('media_count', 'unknown')}",
        profile.get("biography", ""),
    ]
    links = find_urls(profile.get("biography", ""))
    if profile.get("website"):
        links.append(profile["website"])

    release_dates, releases, listed = [], [], 0
    for post in posts:
        caption = post.get("caption") or ""
        posted = (post.get("timestamp") or "")[:10]
        links.extend(find_urls(caption))
        if not looks_like_release(caption) or not posted:
            continue
        views = post.get("view_count")
        release_dates.append(posted)
        releases.append({"date": posted, "views": int(views) if views is not None else None})
        if listed < MAX_POSTS_LISTED:
            listed += 1
            figures = ", ".join(
                f"{post[key]:,} {label}"
                for key, label in (("view_count", "views"), ("like_count", "likes"), ("comments_count", "comments"))
                if post.get(key) is not None
            )
            lines.append(f"Release post {posted} ({post.get('media_type', '')}; {figures}): {caption[:MAX_CAPTION_LENGTH]}")

    followers = profile.get("followers_count")
    return {
        "source": "instagram",
        "title": profile.get("name") or f"@{username}",
        "text": "\n".join(line for line in lines if line),
        "links": links,
        "audience_size": int(followers) if followers is not None else None,
        # Release-like posts among those read (the last year, up to INSTAGRAM_MAX_PAGES pages).
        "release_count": len(release_dates),
        "release_dates": release_dates,
        "releases": releases,
    }
