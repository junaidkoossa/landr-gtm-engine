"""Creator evidence from the YouTube Data API v3 (free, needs YOUTUBE_API_KEY).

Quota: 10,000 units a day by default. A channel lookup, each page of 50 uploads and
each view-count lookup for 50 release uploads cost 1 unit, so up to about 21 units per
creator (500 uploads a year). Channels given as /c/<name> URLs need a
search call, which YouTube caps at 100 a day.

Only uploads whose titles look like releases (RELEASE_TITLE_WORDS) count towards
release figures; vlogs, tutorials and other videos are listed separately.
"""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

import requests

from src.scraper import REQUEST_TIMEOUT, find_urls
from src.settings import MAX_UPLOAD_PAGES

# Words and phrases in a video title that mark it as a release. Matched as whole
# words, ignoring case. "type beat" titles are common for beat sellers.
RELEASE_TITLE_WORDS = (
    "sample pack", "sample packs", "samples", "sample", "pack", "packs",
    "loop kit", "loops", "loop", "drum kit", "sound kit", "kit", "kits",
    "construction kit", "one shots", "one-shots", "one shot", "stems", "midi",
    "beat", "beats", "beat tape", "type beat", "instrumental", "instrumentals",
    "preset", "presets", "preset bank", "sound bank", "soundbank", "sound library",
    "plugin", "plug-in", "vst", "vst3",
    "ep", "lp", "album", "single", "mixtape", "remix",
    "release", "out now", "new release", "free download",
)
RELEASE_TITLE_PATTERN = re.compile(
    r"(?<![0-9a-z])(" + "|".join(re.escape(word) for word in RELEASE_TITLE_WORDS) + r")(?![0-9a-z])",
    re.IGNORECASE,
)

API_URL = "https://www.googleapis.com/youtube/v3"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
PAGE_SIZE = 50
MAX_DESCRIPTION_LENGTH = 500
MAX_OTHER_UPLOADS_LISTED = 10


def looks_like_release(title: str) -> bool:
    return bool(RELEASE_TITLE_PATTERN.search(title or ""))


def is_youtube_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in YOUTUBE_HOSTS


def channel_lookup_params(url: str) -> dict[str, str] | None:
    """Parameters for channels.list from a channel URL, or None if it needs a search."""
    parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
    if not parts:
        return None
    if parts[0].startswith("@"):
        return {"forHandle": parts[0]}
    if parts[0] == "channel" and len(parts) > 1:
        return {"id": parts[1]}
    if parts[0] == "user" and len(parts) > 1:
        return {"forUsername": parts[1]}
    return None


def _get(endpoint: str, params: dict[str, object], api_key: str) -> dict:
    response = requests.get(
        f"{API_URL}/{endpoint}", params={**params, "key": api_key}, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def _search_channel_id(query: str, api_key: str) -> str:
    data = _get(
        "search",
        {"part": "snippet", "type": "channel", "q": query, "maxResults": 1},
        api_key,
    )
    items = data.get("items") or []
    if not items:
        raise ValueError(f"No YouTube channel found for {query!r}")
    return items[0]["id"]["channelId"]


def _published(item: dict) -> str:
    return (
        item.get("contentDetails", {}).get("videoPublishedAt")
        or item.get("snippet", {}).get("publishedAt")
        or ""
    )[:10]


def _view_counts(video_ids: list[str], api_key: str) -> dict[str, int]:
    """Views per video, 50 videos per call (1 quota unit each)."""
    views = {}
    for start in range(0, len(video_ids), PAGE_SIZE):
        data = _get(
            "videos",
            {"part": "statistics", "id": ",".join(video_ids[start:start + PAGE_SIZE]), "maxResults": PAGE_SIZE},
            api_key,
        )
        for item in data.get("items") or []:
            count = item.get("statistics", {}).get("viewCount")
            if count is not None:
                views[item["id"]] = int(count)
    return views


def fetch_youtube_channel(creator: dict[str, str], api_key: str) -> dict[str, object]:
    """Channel details plus its latest uploads, as one evidence part."""
    url = creator.get("youtube_url") or creator["website_url"]
    params = channel_lookup_params(url)
    if params is None:
        parts = [part for part in urlparse(url).path.split("/") if part]
        query = unquote(parts[-1]) if parts and parts[0] == "c" else creator["name"]
        params = {"id": _search_channel_id(query, api_key)}

    data = _get("channels", {"part": "snippet,statistics,contentDetails", **params}, api_key)
    items = data.get("items") or []
    if not items:
        raise ValueError(f"YouTube has no channel at {url}")
    channel = items[0]
    snippet = channel.get("snippet", {})
    statistics = channel.get("statistics", {})
    uploads_id = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")

    # Uploads come newest first; read pages until they are over a year old.
    uploads = []
    if uploads_id:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()
        page_token = None
        for _ in range(MAX_UPLOAD_PAGES):
            params = {"part": "snippet,contentDetails", "playlistId": uploads_id, "maxResults": PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            playlist = _get("playlistItems", params, api_key)
            items = playlist.get("items") or []
            uploads.extend(items)
            page_token = playlist.get("nextPageToken")
            oldest = _published(items[-1]) if items else ""
            if not page_token or (oldest and oldest < cutoff):
                break

    total_videos = statistics.get("videoCount")
    lines = [
        f"Channel: {snippet.get('title', '')}",
        f"Total videos on channel: {total_videos or 'unknown'}",
        snippet.get("description", ""),
    ]
    links = find_urls(snippet.get("description", ""))
    release_ids = [
        item.get("contentDetails", {}).get("videoId")
        for item in uploads
        if looks_like_release(item.get("snippet", {}).get("title", ""))
    ]
    views = _view_counts([video_id for video_id in release_ids if video_id], api_key)

    release_dates, releases, other_titles = [], [], []
    for item in uploads:
        video = item.get("snippet", {})
        title = video.get("title", "")
        published = _published(item)
        description = video.get("description", "")
        links.extend(find_urls(description))
        if not looks_like_release(title):
            other_titles.append(f"{published} {title}")
            continue
        video_views = views.get(item.get("contentDetails", {}).get("videoId"))
        if published:
            release_dates.append(published)
            releases.append({"date": published, "views": video_views})
        shown_views = f", {video_views:,} views" if video_views is not None else ""
        lines.append(
            f"Release upload {published}{shown_views}: {title}. {description[:MAX_DESCRIPTION_LENGTH]}"
        )
    if other_titles:
        lines.append(
            f"Other recent uploads, not counted as releases ({len(other_titles)}): "
            + "; ".join(other_titles[:MAX_OTHER_UPLOADS_LISTED])
        )

    subscribers = None
    if not statistics.get("hiddenSubscriberCount") and statistics.get("subscriberCount"):
        subscribers = int(statistics["subscriberCount"])

    return {
        "source": "youtube",
        "title": snippet.get("title", ""),
        "text": "\n".join(lines),
        "links": links,
        "audience_size": subscribers,
        # Release-like uploads among those read (the last year, up to MAX_UPLOAD_PAGES pages).
        "release_count": len(release_dates),
        "release_dates": release_dates,
        "releases": releases,
    }
