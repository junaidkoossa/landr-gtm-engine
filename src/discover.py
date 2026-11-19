"""Find new leads in Discogs and MusicBrainz and add them to the input CSV.

    python -m src.discover                 # run every saved search, add new leads
    python -m src.discover --dry-run       # show what would be added, write nothing
    python -m src.discover --limit 20      # stop after 20 new leads

The saved searches are in config/discovery.json: which genres and countries to look in,
how far back, and which LANDR product each kind of lead points to. Each search is run
for every genre and country listed, and every lead it finds gets:

- its links (website, YouTube, storefront, Instagram, TikTok) from the artist's or
  label's own Discogs and MusicBrainz pages, plus an email if a label lists one;
- lead_source "discovered:<search name>" (rows you add yourself are "input");
- product_fit, the LANDR products the search points to (for example "Distribution;
  Mastering"), or "Partnership" for a major label;
- self_released "yes" for searches marked self_released (Discogs' "Not On Label"),
  which the scoring counts as the self_released signal.

A lead is skipped when it is already in the input or output CSV, or when no link was
found for it (the pipeline needs at least one). The normal run (python -m src.main)
then collects evidence and scores the new rows like any other.

Discogs needs DISCOGS_TOKEN; MusicBrainz needs no key but allows one request a second.
MusicBrainz's web service is free for non-commercial use only.
"""
import argparse
import csv
import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from src.ingest import DISCOVERY_COLUMNS, INPUT_COLUMNS, LINK_COLUMNS
from src.labels import _discogs_get, _musicbrainz_get
from src.matching import best_match
from src.scraper import STOREFRONT_DOMAINS
from src.social import INSTAGRAM_HOSTS, TIKTOK_HOSTS, on_host
from src.store import CreatorStore, creator_key, name_key
from src.youtube import is_youtube_url

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path("config/discovery.json")
DEFAULT_INPUT = Path("data/input_creators.csv")
DEFAULT_OUTPUT = Path("data/enriched_creators.csv")
INPUT_SOURCE = "input"
PARTNERSHIP = "Partnership"
# Discogs marks self-released records with this label (and a few longer variants).
SELF_RELEASED = "not on label"
# Links that say nothing about the artist's own presence: databases and streaming pages.
IGNORED_HOSTS = (
    "discogs.com", "musicbrainz.org", "wikidata.org", "wikipedia.org", "allmusic.com",
    "spotify.com", "music.apple.com", "itunes.apple.com", "deezer.com", "tidal.com",
    "last.fm", "genius.com", "rateyourmusic.com", "imdb.com", "viaf.org", "isni.org",
    "facebook.com", "x.com", "twitter.com", "threads.net", "soundcloud.com", "linktr.ee",
)
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def load_config(path: str | Path) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    for search in config.get("searches", []):
        missing = [key for key in ("name", "source", "kind") if not search.get(key)]
        if missing:
            raise ValueError(f"A search in {path} is missing {', '.join(missing)}")
        if search["source"] not in ("discogs", "musicbrainz"):
            raise ValueError(f"Search {search['name']!r}: source must be discogs or musicbrainz")
        if search["kind"] not in ("artist", "label"):
            raise ValueError(f"Search {search['name']!r}: kind must be artist or label")
    return config


def sort_links(urls: list[str]) -> dict[str, str]:
    """Put each link in its input column; the first link of each kind wins."""
    columns: dict[str, str] = {}
    for url in urls:
        if not url or not url.startswith(("http://", "https://")):
            continue
        host = (urlparse(url).hostname or "").lower()
        if is_youtube_url(url):
            column = "youtube_url"
        elif on_host(url, INSTAGRAM_HOSTS):
            column = "instagram"
        elif on_host(url, TIKTOK_HOSTS):
            column = "tiktok"
        elif any(host == d or host.endswith(f".{d}") for d in STOREFRONT_DOMAINS):
            column = "storefront_url"
        elif any(host == d or host.endswith(f".{d}") for d in IGNORED_HOSTS):
            continue
        else:
            column = "website_url"
        columns.setdefault(column, url)
    return columns


def is_major(name: str, config: dict) -> bool:
    """A label from one of the major groups (by name, so sublabels need to be listed)."""
    key = name_key(name)
    return any(name_key(major) in key for major in config.get("major_labels", []))


def _lead(name: str, kind: str, links: list[str], search: dict, email: str = "") -> dict[str, str]:
    lead = {column: "" for column in INPUT_COLUMNS + DISCOVERY_COLUMNS}
    lead.update(sort_links(links))
    lead.update(
        name=name,
        target_type="label" if kind == "label" else "creator",
        email=email,
        lead_source=f"discovered:{search['name']}",
        product_fit="; ".join(search.get("products", [])),
        self_released="yes" if search.get("self_released") else "",
    )
    return lead


# --- Discogs -------------------------------------------------------------------------

def _discogs_entity(kind: str, name: str, token: str) -> dict | None:
    """The artist's or label's own Discogs page (profile, urls, contact), matched by name."""
    found = _discogs_get("database/search", {"q": name, "type": kind, "per_page": 10}, token)
    match = best_match(name, [(r.get("title", ""), r) for r in found.get("results", [])])
    if match is None or match[2] != "exact":
        return None
    return _discogs_get(f"{kind}s/{match[1]['id']}", {}, token)


def discogs_names(search: dict, genre: dict, country: str, year: int, token: str) -> list[str]:
    """Artist or label names on recent releases matching one genre, country and year."""
    params = {"type": "release", "country": country, "year": year,
              "per_page": min(100, search.get("per_query", 50)), **search.get("params", {})}
    params.update({k: v for k, v in genre.items() if k in ("genre", "style")})
    names: list[str] = []
    for result in _discogs_get("database/search", params, token).get("results", []):
        if search["kind"] == "label":
            candidates = [label for label in result.get("label", [])
                          if not label.lower().startswith(SELF_RELEASED)]
        else:
            # Release titles read "Artist - Title"; "Various" is a compilation.
            artist = result.get("title", "").split(" - ")[0].strip()
            artist = re.sub(r"\s*\(\d+\)$", "", artist)  # Discogs adds (2) to repeated names
            candidates = [artist] if artist and artist.lower() != "various" else []
        names.extend(name for name in candidates[:1] if name not in names)
    return names


def discogs_lead(name: str, search: dict, token: str, config: dict) -> dict | None:
    entity = _discogs_entity(search["kind"], name, token)
    if entity is None:
        return None
    email = next(iter(EMAIL_PATTERN.findall(entity.get("contact_info") or "")), "")
    lead = _lead(entity.get("name") or name, search["kind"], entity.get("urls") or [], search, email)
    if search["kind"] == "label" and is_major(lead["name"], config):
        lead["product_fit"] = PARTNERSHIP
    return lead


# --- MusicBrainz ---------------------------------------------------------------------

def musicbrainz_entities(search: dict, genre: dict, country: str, since: int) -> list[dict]:
    """Labels or artists matching one genre tag and country, via a Lucene query."""
    parts = [search.get("query", "")]
    if genre.get("tag"):
        parts.append(f'tag:"{genre["tag"]}"')
    if country:
        parts.append(f"country:{country}")
    if search.get("founded_since"):
        parts.append(f"begin:[{since} TO *]")
    query = " AND ".join(part for part in parts if part)
    entity = search["kind"]
    data = _musicbrainz_get(entity, {"query": query, "limit": min(100, search.get("per_query", 25))})
    return data.get(f"{entity}s", [])


def musicbrainz_lead(entity: dict, search: dict, config: dict) -> dict:
    kind = search["kind"]
    detail = _musicbrainz_get(f"{kind}/{entity['id']}", {"inc": "url-rels"})
    links = [r["url"]["resource"] for r in detail.get("relations", []) if r.get("url", {}).get("resource")]
    lead = _lead(entity.get("name", ""), kind, links, search)
    if kind == "label" and is_major(lead["name"], config):
        lead["product_fit"] = PARTNERSHIP
    return lead


# --- Running the searches ------------------------------------------------------------

class Known:
    """Leads already in the input or output CSV, by link and by name."""

    def __init__(self, rows: list[dict]):
        self.keys = {creator_key(row) for row in rows}
        self.names = {name_key(row.get("name") or "") for row in rows} - {""}

    def has(self, lead: dict) -> bool:
        return creator_key(lead) in self.keys or name_key(lead["name"]) in self.names

    def add(self, lead: dict) -> None:
        self.keys.add(creator_key(lead))
        self.names.add(name_key(lead["name"]))


def _candidates(search: dict, config: dict, known: Known, token: str, today: date):
    """Leads for one saved search, across its genres and countries (None = not usable)."""
    source = search["source"]
    years = [today.year - back for back in range(config.get("years_back", 1) + 1)]
    since = today.year - config.get("founded_within_years", 5)
    for genre in config.get("genres", []):
        terms = genre.get(source)
        if not terms:
            continue
        for country in config.get("countries", {}).get(source, []):
            try:
                if source == "discogs":
                    for year in years:
                        for name in discogs_names(search, terms, country, year, token):
                            if name_key(name) not in known.names:
                                yield discogs_lead(name, search, token, config)
                else:
                    for entity in musicbrainz_entities(search, terms, country, since):
                        if name_key(entity.get("name", "")) not in known.names:
                            yield musicbrainz_lead(entity, search, config)
            except requests.RequestException as error:
                logger.warning("Search %r (%s, %s) failed: %s", search["name"], genre.get("name"), country, error)


def discover(config: dict, known: Known, limit: int | None = None, today: date | None = None) -> list[dict]:
    """Run every saved search and return the new leads, in the order found.

    Each search stops at its max_leads, and the whole run at limit (or the config's
    max_new_leads), since every lead costs a few rate-limited calls."""
    today = today or date.today()
    limit = limit or config.get("max_new_leads")
    token = os.getenv("DISCOGS_TOKEN", "").strip()
    found: list[dict] = []
    for search in config.get("searches", []):
        if search.get("enabled") is False:
            continue
        if search["source"] == "discogs" and not token:
            logger.warning("Skipping search %r: set DISCOGS_TOKEN to search Discogs", search["name"])
            continue
        from_search = 0
        for lead in _candidates(search, config, known, token, today):
            if lead is None or not lead["name"] or known.has(lead):
                continue
            if not any(lead[column] for column in LINK_COLUMNS):
                logger.info("No links found for %s; skipped", lead["name"])
                continue
            known.add(lead)
            found.append(lead)
            from_search += 1
            logger.info("Found %s (%s, %s)", lead["name"], lead["lead_source"], lead["product_fit"])
            if limit and len(found) >= limit:
                return found
            if search.get("max_leads") and from_search >= search["max_leads"]:
                break
    return found


# --- The input CSV -------------------------------------------------------------------

def read_input(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists():
        return list(INPUT_COLUMNS) + list(DISCOVERY_COLUMNS), []
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        return list(reader.fieldnames or INPUT_COLUMNS), list(reader)


def append_leads(path: Path, leads: list[dict]) -> None:
    """Add the leads to the input CSV, adding lead_source and product_fit columns if needed.

    Rows already there keep every column they had; those without a lead_source are
    marked "input"."""
    columns, rows = read_input(path)
    for column in list(INPUT_COLUMNS) + list(DISCOVERY_COLUMNS):
        if column not in columns:
            columns.append(column)
    for row in rows:
        row["lead_source"] = row.get("lead_source") or INPUT_SOURCE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows + leads)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Find new leads in Discogs and MusicBrainz.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="the input CSV to add leads to")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="the scored CSV (to skip leads already in it)")
    parser.add_argument("--limit", type=int, help="stop after this many new leads")
    parser.add_argument("--dry-run", action="store_true", help="list the new leads without writing them")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        config = load_config(args.config)
        input_path = Path(args.input)
        _, input_rows = read_input(input_path)
        known = Known(input_rows + list(CreatorStore(args.output).rows.values()))
        leads = discover(config, known, args.limit)
    except (OSError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")

    if args.dry_run:
        for lead in leads:
            print(f"{lead['name']} ({lead['target_type']}): {creator_key(lead)} [{lead['product_fit']}]")
        logger.info("Dry run: %d new leads found, nothing written", len(leads))
    else:
        if leads:
            append_leads(input_path, leads)
        logger.info("Added %d new leads to %s", len(leads), input_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
