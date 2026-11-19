"""Pick the data sources for each creator and merge what they return into one evidence record.

Each link column in the input is a source, and every source a creator has is used:

- website_url: the page is scraped. Linktree, Facebook, X and similar pages are skipped,
  because they show a login wall or need JavaScript.
- youtube_url: YouTube Data API.
- storefront_url: always counts as a storefront; the page is scraped when it can be.
- Labels (target_type "label"): also MusicBrainz and Discogs, searched by name.
- instagram: Business Discovery API.
- tiktok: Bright Data, collected as a job for the whole run (src/tiktok.py) and passed in.
"""
import logging
import os
from datetime import date, datetime, timedelta, timezone
from statistics import median
from urllib.parse import urlparse

import requests

from src.instagram import InstagramUnavailable, fetch_instagram_profile
from src.instagram import credentials as instagram_credentials
from src.labels import fetch_discogs_label, fetch_musicbrainz_label
from src.scraper import (
    MAX_SOURCE_TEXT_LENGTH,
    find_release_signals,
    find_storefront_links,
    scrape_website,
)
from src.scoring import detect_signals
from src.settings import VIEW_MATURITY_DAYS
from src.social import INSTAGRAM_HOSTS, TIKTOK_HOSTS, instagram_handle, on_host, tiktok_handle
from src.youtube import fetch_youtube_channel, is_youtube_url

logger = logging.getLogger(__name__)
LABEL_TYPES = {"label", "labels"}
MAX_RECENT_RELEASES = 10
SOCIAL_ONLY_DOMAINS = (
    "instagram.com", "tiktok.com", "linktr.ee", "facebook.com", "fb.com",
    "x.com", "twitter.com", "threads.net", "snapchat.com",
)


class UnsupportedSource(ValueError):
    """A URL the pipeline can't read; retrying won't help."""


class MissingCredentials(ValueError):
    """The creator's only sources need a token that isn't set; skipped without counting a failure."""


def is_social_only(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in SOCIAL_ONLY_DOMAINS)


def is_label(creator: dict[str, str]) -> bool:
    return creator["target_type"].strip().casefold() in LABEL_TYPES


def _parse_date(value: str) -> date | None:
    """Full dates and year-month dates; a bare year is too vague to place in the last 12 months."""
    for length, pattern in ((10, "%Y-%m-%d"), (7, "%Y-%m")):
        if len(value) >= length:
            try:
                return datetime.strptime(value[:length], pattern).date()
            except ValueError:
                pass
    return None


def _view_figures(
    parts: list[dict[str, object]], today: date
) -> dict[str, object]:
    """Views on releases from the last 12 months, leaving out the newest VIEW_MATURITY_DAYS.

    steady_views adds up, per platform, the median views per release times the number of
    releases with views. A single breakout video barely moves a median, so a creator who
    is consistently watched scores higher than one carried by one hit.
    """
    start = today - timedelta(days=365)
    end = today - timedelta(days=VIEW_MATURITY_DAYS)
    steady, all_views = 0.0, []
    for part in parts:
        views = [
            int(release["views"])
            for release in part.get("releases") or []
            if release.get("views") is not None
            and (parsed := _parse_date(str(release.get("date") or "")))
            and start <= parsed <= end
        ]
        if views:
            # A part estimated from a short sample (TikTok) is scaled up to a year.
            factor = (part.get("estimate") or {}).get("factor", 1.0)
            steady += median(views) * len(views) * factor
            all_views.extend(views)
    if not all_views:
        return {
            "views_last_12_months": None,
            "median_views_per_release": None,
            "steady_views": None,
            "top_video_share": None,
        }
    total = sum(all_views)
    return {
        "views_last_12_months": total,
        "median_views_per_release": int(median(all_views)),
        "steady_views": int(steady),
        "top_video_share": round(max(all_views) / total, 3) if total else 0.0,
    }


def merge_evidence(
    parts: list[dict[str, object]], signal_keywords: list[str], today: date | None = None
) -> dict[str, object]:
    today = today or datetime.now(timezone.utc).date()
    text = "\n\n".join(f"[{part['source']}]\n{part['text']}" for part in parts)
    links = [link for part in parts for link in part["links"]]
    storefront_links = find_storefront_links(links)
    dates = sorted({d for part in parts for d in part["release_dates"]}, reverse=True)
    cutoff = today - timedelta(days=365)

    def recent(values: list[str]) -> list[str]:
        return [d for d in values if (parsed := _parse_date(d)) and cutoff <= parsed <= today]

    # Sources often list the same releases (MusicBrainz and Discogs, or a beat posted on
    # YouTube and TikTok), so the 12-month count is the highest single source, not a sum.
    recent_counts = [len(recent(part["release_dates"])) for part in parts if part["release_dates"]]
    active_months = {d[:7] for d in recent(dates)}
    estimates = [part["estimate"] for part in parts if part.get("estimate")]
    recent_counts += [estimate["releases_last_12_months"] for estimate in estimates]
    has_months = any(_parse_date(d) for d in dates) or bool(estimates)
    months = max([len(active_months)] + [e["active_months_last_12"] for e in estimates])
    audiences = [part["audience_size"] for part in parts if part["audience_size"] is not None]
    counts = [part["release_count"] for part in parts if part["release_count"] is not None]

    return {
        "page_title": next((part["title"] for part in parts if part["title"]), ""),
        "source_text": text[:MAX_SOURCE_TEXT_LENGTH],
        "storefront_links": storefront_links,
        "release_signals": find_release_signals(text, storefront_links, signal_keywords),
        # Product signals (src/scoring.py): producer, sample packs, competitor distributor...
        "product_signals": detect_signals(text, links),
        "data_sources": [part["source"] for part in parts],
        "audience_size": max(audiences) if audiences else None,
        "release_count": max(counts) if counts else None,
        "releases_last_12_months": max(recent_counts) if recent_counts else None,
        "latest_release_date": dates[0] if dates else "",
        "recent_releases": dates[:MAX_RECENT_RELEASES],
        # Months with a dated release; None when no source gave dates more exact than a year.
        "active_months_last_12": months if has_months else None,
        # Sources whose 12-month figures are estimated from a shorter sample of posts.
        "estimated_sources": [part["source"] for part in parts if part.get("estimate")],
        **_view_figures(parts, today),
        # Name matches the pipeline used but a person should check.
        "review_matches": [
            part["match"] for part in parts if part.get("match") and not part["match"]["exact"]
        ],
    }


def _storefront_part(url: str) -> dict[str, object]:
    """The storefront page if it can be read (Bandcamp and Gumroad can; Beatstars needs
    JavaScript); otherwise just the link, which still counts as a storefront."""
    try:
        part = scrape_website(url)
    except (requests.RequestException, ValueError) as error:
        logger.info("Couldn't read storefront %s: %s", url, error)
        part = {"title": "", "text": "", "links": [], "audience_size": None,
                "release_count": None, "release_dates": []}
    return {**part, "source": "storefront", "links": [url] + list(part["links"])}


def collect_evidence(
    creator: dict[str, str],
    signal_keywords: list[str],
    tiktok: dict[str, object] | Exception | None = None,
) -> dict[str, object]:
    """Merge every source for one creator. `tiktok` is this creator's result from the
    run's TikTok job (a part, an error, or None if it has no TikTok or none was collected).

    A source that fails is noted and skipped as long as another source worked; if none
    did, the most telling error is raised (one a retry could fix comes first)."""
    parts: list[dict[str, object]] = []
    problems: list[Exception] = []

    def attempt(name: str, lookup) -> None:
        try:
            part = lookup()
        except (requests.RequestException, ValueError, KeyError) as error:
            problems.append(error if isinstance(error, (requests.RequestException, ValueError))
                            else ValueError(f"{name}: {error}"))
            return
        if part is None:
            problems.append(ValueError(f"{name}: no label named {creator['name']!r}"))
        else:
            parts.append(part)

    website = creator.get("website_url") or ""
    youtube = creator.get("youtube_url") or ""
    if website and is_youtube_url(website) and not youtube:
        youtube, website = website, ""  # an older input file with YouTube as the website
    if website and (on_host(website, INSTAGRAM_HOSTS) or on_host(website, TIKTOK_HOSTS)):
        website = ""  # read through the Instagram/TikTok handles below
    if website and is_social_only(website):
        problems.append(UnsupportedSource(
            f"{urlparse(website).hostname} pages can't be read; give a website, YouTube channel, "
            "storefront, Instagram or TikTok instead"
        ))
        website = ""

    if website:
        attempt("website", lambda: scrape_website(website))
    if youtube:
        api_key = os.getenv("YOUTUBE_API_KEY")
        if api_key:
            attempt("YouTube", lambda: fetch_youtube_channel({**creator, "youtube_url": youtube}, api_key))
        else:
            problems.append(MissingCredentials("YouTube needs YOUTUBE_API_KEY"))
    if creator.get("storefront_url"):
        parts.append(_storefront_part(creator["storefront_url"]))
    if is_label(creator):
        attempt("MusicBrainz", lambda: fetch_musicbrainz_label(creator["name"]))
        token = os.getenv("DISCOGS_TOKEN")
        if token:
            attempt("Discogs", lambda: fetch_discogs_label(creator["name"], token))

    handle = instagram_handle(creator)
    if handle:
        found = instagram_credentials()
        if found is None:
            problems.append(MissingCredentials("Instagram needs INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_BUSINESS_ID"))
        else:
            try:
                parts.append(fetch_instagram_profile(handle, *found))
            except InstagramUnavailable as error:
                problems.append(UnsupportedSource(str(error)))
            except requests.RequestException as error:
                problems.append(error)

    if tiktok_handle(creator):
        if isinstance(tiktok, Exception):
            problems.append(tiktok)
        elif tiktok is not None:
            parts.append(tiktok)
        else:
            problems.append(MissingCredentials("TikTok needs BRIGHTDATA_API_TOKEN"))

    if not parts:
        if not problems:
            raise ValueError("no data source for this creator")
        for kind in (requests.RequestException, MissingCredentials, UnsupportedSource):
            for problem in problems:
                if isinstance(problem, kind):
                    raise problem
        raise ValueError("no data source had this creator (" + "; ".join(map(str, problems)) + ")")
    for problem in problems:
        logger.info("%s: %s", creator["name"], problem)
    evidence = merge_evidence(parts, signal_keywords)
    storefront = creator.get("storefront_url")
    if storefront and storefront not in evidence["storefront_links"]:
        evidence["storefront_links"].insert(0, storefront)  # given as a store, whatever the site
    return evidence
