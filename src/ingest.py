"""Load the input CSV of creators.

Columns (only name and target_type are required, plus at least one link):

    name, target_type, website_url, youtube_url, storefront_url, instagram, tiktok, email

- website_url: the creator's own site; it's scraped.
- youtube_url: a YouTube channel; read with the YouTube Data API.
- storefront_url: where they sell (Bandcamp, Beatstars, Gumroad...); always counts as a
  storefront, whatever the site.
- instagram / tiktok: a handle (@name) or profile URL.
- email: a contact email, used for HubSpot only.

Older files with a single website_url column still work: a YouTube, Instagram or TikTok
link there is moved to its own column when it's read.
"""
import csv
import logging
from pathlib import Path

from src.social import INSTAGRAM_HOSTS, TIKTOK_HOSTS, on_host
from src.youtube import is_youtube_url

REQUIRED_COLUMNS = ("name", "target_type")
LINK_COLUMNS = ("website_url", "youtube_url", "storefront_url", "instagram", "tiktok")
INPUT_COLUMNS = REQUIRED_COLUMNS + LINK_COLUMNS + ("email",)
logger = logging.getLogger(__name__)


def _sort_links(creator: dict[str, str]) -> None:
    """Move a YouTube, Instagram or TikTok link given as website_url to its own column."""
    url = creator["website_url"]
    for column, matches in (
        ("youtube_url", is_youtube_url),
        ("instagram", lambda u: on_host(u, INSTAGRAM_HOSTS)),
        ("tiktok", lambda u: on_host(u, TIKTOK_HOSTS)),
    ):
        if url and matches(url) and not creator[column]:
            creator[column], creator["website_url"] = url, ""
            return


def load_creators(path: str | Path) -> list[dict[str, str]]:
    """Load complete creator records and warn about rows that cannot be used."""
    creators = []
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is empty or has no CSV header")

        missing_columns = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
        if not any(column in reader.fieldnames for column in LINK_COLUMNS):
            missing_columns.append("at least one of " + ", ".join(LINK_COLUMNS))
        if missing_columns:
            raise ValueError(f"{path} is missing required columns: {', '.join(missing_columns)}")

        for row_number, row in enumerate(reader, start=2):
            creator = {column: (row.get(column) or "").strip() for column in INPUT_COLUMNS}
            missing_values = [column for column in REQUIRED_COLUMNS if not creator[column]]
            if not any(creator[column] for column in LINK_COLUMNS):
                missing_values.append("a link (" + ", ".join(LINK_COLUMNS) + ")")
            if missing_values:
                logger.warning(
                    "Skipping input row %d; missing required values: %s",
                    row_number,
                    ", ".join(missing_values),
                )
                continue
            _sort_links(creator)
            creators.append(creator)

    if not creators:
        raise ValueError(f"{path} contains no complete creator records")
    return creators
