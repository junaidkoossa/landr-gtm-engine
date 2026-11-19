"""The CSV files the pipeline keeps between runs.

- Output CSV (default data/enriched_creators.csv): one row per creator, kept up to date.
  Rows are never dropped; a re-scored creator's row is replaced in place.
- Unsent CSV: every creator in the output that hasn't reached the webhook or HubSpot.
- Failures CSV: creators whose data or scoring failed, with an attempt count.
- Review CSV: label matches that weren't exact, for a person to check.

Enrichment columns (see src/enrichment.py) are kept when a creator is scored again,
so figures Clay found aren't lost on a 90-day re-score.
"""
import csv
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from src.matching import name_key
from src.social import instagram_handle, tiktok_handle

OUTPUT_COLUMNS = (
    "name",
    "website_url",
    "youtube_url",
    "storefront_url",
    "target_type",
    "email",
    "instagram",
    "tiktok",
    "lead_source",
    "page_title",
    "storefront_links",
    "release_signals",
    "product_signals",
    "score",
    "best_product",
    "score_mastering",
    "score_distribution",
    "score_samples",
    "score_plugins",
    "base_score",
    "score_adjustment",
    "adjustment_reason",
    "score_breakdown",
    "product_breakdowns",
    "big_act_test",
    "scoring_version",
    "scored_by",
    "bottleneck",
    "custom_hook",
    "hook_by",
    "data_sources",
    "audience_size",
    "release_count",
    "releases_last_12_months",
    "latest_release_date",
    "active_months_last_12",
    "views_last_12_months",
    "median_views_per_release",
    "steady_views",
    "top_video_share",
    "estimated_sources",
    "limited_data",
    "spotify_monthly_listeners",
    "clay_followers",
    "roster_size",
    "self_released",
    "mastering_credit",
    "pack_count",
    "latest_pack_date",
    "producer_or_engineer",
    "makes_production_content",
    "clay_research",
    "clay_score_mastering",
    "clay_score_distribution",
    "clay_score_samples",
    "clay_score_plugins",
    "clay_score_version",
    "email_source",
    "score_before_enrichment",
    "enrichment_provider",
    "enrichment_status",
    "enrichment_experiment",
    "exploration",
    "enrichment_sent_at",
    "enrichment_returned_at",
    "needs_review",
    "review_reason",
    "enriched_at",
    "webhook_status",
    "webhook_sent_at",
    "hubspot_status",
    "hubspot_synced_at",
)
LIST_COLUMNS = ("storefront_links", "release_signals", "product_signals", "data_sources", "estimated_sources")
LIST_SEPARATOR = " | "

# webhook_status values
SENT = "sent"
FAILED = "failed"
BELOW_MIN_SCORE = "below_min_score"
NO_WEBHOOK = "no_webhook"
# hubspot_status values (FAILED and BELOW_MIN_SCORE are shared with the webhook)
SYNCED = "synced"
NO_EMAIL = "no_email"
NOT_CONFIGURED = "not_configured"
AWAITING_ENRICHMENT = "awaiting_enrichment"
# Kept from the earlier row when a creator is scored again, unless the new record has them.
ENRICHMENT_COLUMNS = (
    "spotify_monthly_listeners",
    "clay_followers",
    "roster_size",
    "self_released",
    "mastering_credit",
    "pack_count",
    "latest_pack_date",
    "producer_or_engineer",
    "makes_production_content",
    "clay_research",
    "clay_score_version",
    "email_source",
    "score_before_enrichment",
    "enrichment_provider",
    "enrichment_status",
    "enrichment_experiment",
    "enrichment_sent_at",
    "enrichment_returned_at",
)

FAILURE_COLUMNS = (
    "name", "website_url", "target_type", "attempts", "last_error", "last_attempt_at", "status",
)
REVIEW_COLUMNS = (
    "name", "website_url", "source", "matched_name", "match_type", "matched_url", "flagged_at",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def url_key(url: str) -> str:
    """A website ignoring scheme, "www.", case, query and trailing slash."""
    url = (url or "").strip()
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return f"{host}{parsed.path.rstrip('/').lower()}" if host else ""


def creator_key(record: dict[str, object]) -> str:
    """The creator's first link (website, YouTube, storefront, Instagram, TikTok), or its name."""
    for column in ("website_url", "youtube_url", "storefront_url"):
        if record.get(column):
            return url_key(str(record[column]))
    if instagram_handle(record):
        return f"instagram.com/{instagram_handle(record)}"
    if tiktok_handle(record):
        return f"tiktok.com/@{tiktok_handle(record)}"
    return f"name:{name_key(str(record.get('name') or ''))}"


def _read_csv(path: Path, columns: tuple[str, ...]) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        fields = tuple(reader.fieldnames or ())
        unknown = [field for field in fields if field not in columns]
        if "name" not in fields or unknown:
            raise ValueError(
                f"{path} doesn't look like a file this pipeline wrote "
                f"(unexpected columns: {', '.join(unknown) or 'name missing'})"
            )
        # Older files may lack newer columns; those start out blank.
        return [{column: row.get(column) or "" for column in columns} for row in reader]


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    """Write via a temporary file and swap it in, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def related_path(output_path: str | Path, suffix: str) -> Path:
    output_path = Path(output_path)
    return output_path.with_name(f"{output_path.stem}.{suffix}")


class CreatorStore:
    """The output CSV, held in memory and matched by website or by name."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.rows: dict[str, dict[str, object]] = {}
        self.by_name: dict[str, str] = {}
        for row in _read_csv(self.path, OUTPUT_COLUMNS):
            self._index(row)

    def _index(self, row: dict[str, object]) -> None:
        key = creator_key(row)
        self.rows[key] = row
        if name_key(str(row["name"])):
            self.by_name.setdefault(name_key(str(row["name"])), key)

    def find(self, creator: dict[str, object]) -> tuple[str, dict[str, object]] | None:
        """The stored creator with the same website or, failing that, the same name."""
        key = creator_key(creator)
        if key in self.rows:
            return key, self.rows[key]
        by_name = self.by_name.get(name_key(str(creator.get("name") or "")))
        if by_name:
            return by_name, self.rows[by_name]
        return None

    def upsert(self, record: dict[str, object], replace_key: str | None = None) -> dict[str, object]:
        """Store a scored creator, replacing its earlier row but keeping its delivery history."""
        previous = self.rows.pop(replace_key, None) if replace_key else None
        row = {column: record.get(column) for column in OUTPUT_COLUMNS}
        for column in LIST_COLUMNS:
            value = row[column]
            row[column] = LIST_SEPARATOR.join(value) if isinstance(value, list) else (value or "")
        if previous:
            row["webhook_status"] = previous.get("webhook_status") or ""
            row["webhook_sent_at"] = previous.get("webhook_sent_at") or ""
            row["hubspot_status"] = previous.get("hubspot_status") or ""
            row["hubspot_synced_at"] = previous.get("hubspot_synced_at") or ""
            for column in ("email", "instagram", "tiktok") + ENRICHMENT_COLUMNS:
                if row[column] in (None, ""):
                    row[column] = previous.get(column) or ""
        if str(row.get("email") or "").strip() and not row.get("email_source"):
            row["email_source"] = "input"
        self._index(row)
        return row

    def set_webhook_status(self, row: dict[str, object], status: str) -> None:
        row["webhook_status"] = status
        if status == SENT:
            row["webhook_sent_at"] = now_iso()

    def set_hubspot_status(self, row: dict[str, object], status: str) -> None:
        row["hubspot_status"] = status
        if status == SYNCED:
            row["hubspot_synced_at"] = now_iso()

    def unsent_rows(self) -> list[dict[str, object]]:
        """Creators that reached neither the webhook nor HubSpot."""
        return [
            row for row in self.rows.values()
            if row.get("webhook_status") != SENT and row.get("hubspot_status") != SYNCED
        ]

    def save(self) -> None:
        _write_csv(self.path, OUTPUT_COLUMNS, list(self.rows.values()))

    def save_unsent(self, path: str | Path) -> None:
        _write_csv(Path(path), OUTPUT_COLUMNS, self.unsent_rows())


def is_stale(row: dict[str, object], days: int) -> bool:
    """True if the row was scored more than `days` ago (or has no usable date)."""
    try:
        scored = datetime.fromisoformat(str(row.get("enriched_at") or ""))
    except ValueError:
        return True
    if scored.tzinfo is None:
        scored = scored.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - scored > timedelta(days=days)


class FailureLog:
    """Creators that failed, with how many runs in a row they have failed."""

    def __init__(self, path: str | Path, max_attempts: int):
        self.path = Path(path)
        self.max_attempts = max_attempts
        self.rows = {creator_key(row): row for row in _read_csv(self.path, FAILURE_COLUMNS)}

    def attempts(self, creator: dict[str, object]) -> int:
        row = self.rows.get(creator_key(creator))
        try:
            return int(row["attempts"]) if row else 0
        except ValueError:
            return 0

    def should_skip(self, creator: dict[str, object]) -> bool:
        return self.attempts(creator) >= self.max_attempts

    def record(self, creator: dict[str, object], error: object, permanent: bool = False) -> None:
        attempts = self.max_attempts if permanent else self.attempts(creator) + 1
        self.rows[creator_key(creator)] = {
            "name": creator.get("name", ""),
            "website_url": creator.get("website_url", ""),
            "target_type": creator.get("target_type", ""),
            "attempts": attempts,
            "last_error": str(error),
            "last_attempt_at": now_iso(),
            "status": "skipped" if attempts >= self.max_attempts else "will retry",
        }

    def clear(self, creator: dict[str, object]) -> None:
        self.rows.pop(creator_key(creator), None)

    def save(self) -> None:
        if self.rows or self.path.exists():
            _write_csv(self.path, FAILURE_COLUMNS, list(self.rows.values()))


class ReviewLog:
    """Loose label matches for a person to confirm or reject."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.rows = _read_csv(self.path, REVIEW_COLUMNS)
        self.seen = {self._key(row) for row in self.rows}

    @staticmethod
    def _key(row: dict[str, object]) -> tuple:
        return (name_key(str(row["name"])), row["source"], name_key(str(row["matched_name"])))

    def add(self, creator: dict[str, object], match: dict[str, object]) -> None:
        row = {
            "name": creator.get("name", ""),
            "website_url": creator.get("website_url", ""),
            "source": match["source"],
            "matched_name": match["matched_name"],
            "match_type": match["match_type"],
            "matched_url": match.get("matched_url", ""),
            "flagged_at": now_iso(),
        }
        if self._key(row) not in self.seen:
            self.seen.add(self._key(row))
            self.rows.append(row)

    def save(self) -> None:
        if self.rows:
            _write_csv(self.path, REVIEW_COLUMNS, self.rows)
