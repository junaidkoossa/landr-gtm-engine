import argparse
import json
import logging
import os
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

from src.activate import WEBHOOK_FORMATS, send_to_webhook
from src.collect import MissingCredentials, UnsupportedSource, collect_evidence, is_label
from src.enricher import (
    EVIDENCE_FIELDS,
    ScoringUnavailable,
    batch_model,
    batch_results,
    build_request,
    claude_unavailable,
    enrich_creator,
    live_model,
    scoring_provider,
    submit_batch,
    wait_for_batch,
)
from src.guard import suspicious_phrases
from src.hooks import HookWriter, hook_min_score, hook_provider
from src.hubspot import access_token as hubspot_token
from src.hubspot import upsert_contacts
from src.ingest import load_creators
from src.matching import name_key
from src.scoring import base_score, final_score, limited_data
from src.instagram import credentials as instagram_credentials
from src.social import instagram_handle, tiktok_handle
from src.tiktok import TikTokJobs
from src.tiktok import api_token as brightdata_token
from src.rules import load_vertical_rules
from src.settings import (
    BATCH_POLL_SECONDS,
    BATCH_SIZE,
    BATCH_THRESHOLD,
    MAX_ATTEMPTS,
    RESCORE_AFTER_DAYS,
)
from src.store import (
    BELOW_MIN_SCORE,
    FAILED,
    NO_EMAIL,
    NO_WEBHOOK,
    NOT_CONFIGURED,
    SENT,
    SYNCED,
    CreatorStore,
    FailureLog,
    ReviewLog,
    creator_key,
    is_stale,
    now_iso,
    related_path,
)

logger = logging.getLogger(__name__)

# What Claude sees about the creator (never the email).
CREATOR_FIELDS = ("name", "target_type", "website_url", "youtube_url", "storefront_url", "instagram", "tiktok")


def run_files(output_path: str | Path) -> dict[str, Path]:
    """The files kept next to the output CSV."""
    return {
        "unsent": related_path(output_path, "unsent.csv"),
        "failures": related_path(output_path, "failures.csv"),
        "review": related_path(output_path, "review_matches.csv"),
        "pending": related_path(output_path, "pending_batches.json"),
    }


class PendingBatches:
    """Batches submitted but not yet collected, saved so a stopped run can pick them up."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: list[dict] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        )

    def add(self, batch_id: str, records: dict[str, dict]) -> None:
        self.entries.append({"batch_id": batch_id, "records": records})
        self.save()

    def remove(self, batch_id: str) -> None:
        self.entries = [entry for entry in self.entries if entry["batch_id"] != batch_id]
        self.save()

    def save(self) -> None:
        if self.entries:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.entries, ensure_ascii=False), encoding="utf-8")
        elif self.path.exists():
            self.path.unlink()


def review_reasons(record: dict[str, object]) -> list[str]:
    """Why a person should check this creator: inexact label matches and suspicious page text."""
    reasons = [
        f"label name variant on {match['source']}: '{match['matched_name']}'"
        for match in record.get("review_matches") or []
    ]
    text = json.dumps(
        {field: record.get(field) for field in EVIDENCE_FIELDS}, ensure_ascii=False, default=str
    )
    reasons += [f"suspicious page text: '{phrase}'" for phrase in suspicious_phrases(text)]
    return reasons


class Run:
    """One pipeline run: what has been scored, what failed, and the files that record it."""

    def __init__(self, output_path, rules, client, poll_seconds, provider="claude", hooks="same"):
        self.files = run_files(output_path)
        self.store = CreatorStore(output_path)
        self.failures = FailureLog(self.files["failures"], MAX_ATTEMPTS)
        self.reviews = ReviewLog(self.files["review"])
        self.pending = PendingBatches(self.files["pending"])
        self.rules = rules
        self.client = client
        self.provider = provider
        # "same": the scoring reply has the hook; "claude": written after scoring, for
        # top-scored creators only; "none": no hooks.
        self.hooks = hooks
        self.with_hook = hooks == "same"
        self.hook_writer = HookWriter(client) if hooks == "claude" else None
        self.poll_seconds = poll_seconds
        self.scored: list[dict[str, object]] = []
        self.failed = 0

    def finish(self, record: dict[str, object], enrichment: dict[str, object]) -> None:
        if "base_score" not in record:  # e.g. a batch submitted before weighted scoring
            score, breakdown = base_score(record, is_label(record), self.rules["scoring"])
            record = {**record, "base_score": score, "score_breakdown": breakdown,
                      "limited_data": limited_data(record, is_label(record))}
        reasons = review_reasons(record)
        if any(reason.startswith("suspicious") for reason in reasons):
            logger.warning("Page text for %s looks like it is aimed at the scorer", record["name"])
        score = final_score(record["base_score"], enrichment["score_adjustment"])
        if self.hook_writer:
            creator = {field: record.get(field, "") for field in CREATOR_FIELDS}
            enrichment = {**enrichment, **self.hook_writer.write(creator, record, self.rules, enrichment, score)}
        elif self.with_hook:
            enrichment = {**enrichment, "hook_by": enrichment.get("scored_by", "")}
        else:
            enrichment = {**enrichment, "custom_hook": "", "hook_by": ""}
        row = self.store.upsert(
            {
                **record,
                **enrichment,
                "score": score,
                "needs_review": "yes" if reasons else "",
                "review_reason": "; ".join(reasons),
                "enriched_at": now_iso(),
            },
            replace_key=record.get("replace_key"),
        )
        self.failures.clear(record)
        self.scored.append(row)

    def fail(self, record: dict[str, object], error: object, permanent: bool = False) -> None:
        self.failed += 1
        self.failures.record(record, error, permanent=permanent)
        logger.warning("Skipping %s (%s): %s", record["name"], creator_key(record), error)

    def save(self) -> None:
        self.store.save()
        self.failures.save()
        self.reviews.save()

    def score_in_batch(self, records: list[dict[str, object]]) -> None:
        """Submit one batch; it is collected later by collect_batches()."""
        offset = sum(len(entry["records"]) for entry in self.pending.entries)
        by_id = {f"creator-{offset + index}": record for index, record in enumerate(records)}
        model = batch_model()
        batch_id = submit_batch(
            self.client,
            {
                custom_id: build_request(
                    {field: record.get(field, "") for field in CREATOR_FIELDS}, record, self.rules, model,
                    with_hook=self.with_hook,
                )
                for custom_id, record in by_id.items()
            },
        )
        self.pending.add(batch_id, by_id)
        logger.info("Submitted scoring batch %s (%d creators, %s)", batch_id, len(by_id), model)

    def collect_batches(self) -> None:
        for entry in list(self.pending.entries):
            batch_id, records = entry["batch_id"], entry["records"]
            logger.info("Waiting for scoring batch %s (%d creators)", batch_id, len(records))
            wait_for_batch(self.client, batch_id, self.poll_seconds)
            for custom_id, outcome in batch_results(self.client, batch_id, records, self.with_hook).items():
                if isinstance(outcome, Exception):
                    self.fail(records[custom_id], outcome)
                else:
                    self.finish(records[custom_id], outcome)
            self.save()
            self.pending.remove(batch_id)

    def score_live(self, record: dict[str, object]) -> None:
        creator = {field: record.get(field, "") for field in CREATOR_FIELDS}
        try:
            enrichment = enrich_creator(creator, record, self.rules, self.client, self.provider, self.with_hook)
        except anthropic.APIError as error:
            reason = claude_unavailable(error)
            if reason:
                # Every later creator would fail the same way: stop, and don't count it
                # against this creator.
                raise ScoringUnavailable(f"Stopped scoring: {reason}. {error}") from error
            self.fail(record, error)
            return
        except ValueError as error:
            self.fail(record, error)
            return
        self.finish(record, enrichment)
        self.save()


def _send_webhook(store, rows, min_score, webhook_url, webhook_format) -> tuple[int, int]:
    to_send = []
    for row in rows:
        if row.get("webhook_status") == SENT:
            continue  # already in Clay; a re-score doesn't send it again
        if int(row["score"]) < min_score:
            store.set_webhook_status(row, BELOW_MIN_SCORE)
        elif not webhook_url:
            store.set_webhook_status(row, NO_WEBHOOK)
        else:
            to_send.append(row)
    to_send.sort(key=lambda row: int(row["score"]), reverse=True)

    sent = failed = 0
    for row, error in send_to_webhook(to_send, webhook_url, webhook_format) if to_send else []:
        if error is None:
            store.set_webhook_status(row, SENT)
            sent += 1
        else:
            store.set_webhook_status(row, FAILED)
            failed += 1
            logger.warning("Webhook didn't take %s: %s", row["name"], error)
    return sent, failed


def _sync_hubspot(store, rows, min_score, token) -> tuple[int, int]:
    """Upsert contacts by email. Unlike the webhook, a re-scored creator is synced again,
    so HubSpot always has the latest score (an upsert updates the same contact)."""
    to_sync = []
    for row in rows:
        if not token:
            store.set_hubspot_status(row, NOT_CONFIGURED)
        elif int(row["score"]) < min_score:
            store.set_hubspot_status(row, BELOW_MIN_SCORE)
        elif not str(row.get("email") or "").strip():
            store.set_hubspot_status(row, NO_EMAIL)
        else:
            to_sync.append(row)

    synced = failed = 0
    for row, error in upsert_contacts(to_sync, token) if to_sync else []:
        if error is None:
            store.set_hubspot_status(row, SYNCED)
            synced += 1
        else:
            store.set_hubspot_status(row, FAILED)
            failed += 1
            logger.warning("HubSpot didn't take %s: %s", row["name"], error)
    return synced, failed


def deliver(
    store: CreatorStore,
    rows: list[dict[str, object]],
    min_score: int,
    webhook_url: str | None,
    webhook_format: str,
    hubspot: str | None = None,
) -> dict[str, int]:
    """Send qualifying rows to the webhook and to HubSpot, recording each row's status."""
    sent, webhook_failed = _send_webhook(store, rows, min_score, webhook_url, webhook_format)
    synced, hubspot_failed = _sync_hubspot(store, rows, min_score, hubspot)
    return {
        "webhook_sent": sent,
        "webhook_failed": webhook_failed,
        "hubspot_synced": synced,
        "hubspot_failed": hubspot_failed,
    }


def run_pipeline(
    input_path: str | Path,
    rules_path: str | Path,
    output_path: str | Path,
    client: anthropic.Anthropic | None = None,
    webhook_url: str | None = None,
    webhook_format: str = "clay",
    scoring: str = "auto",
    poll_seconds: float = BATCH_POLL_SECONDS,
    hubspot: str | None = None,
    rescore_all: bool = False,
    provider: str | None = None,
    retry_failed: bool = False,
) -> list[dict[str, object]]:
    """Score new and stale creators, keep the output CSV up to date, and return this run's rows.

    rescore_all scores every creator in the input again, however recently it was scored
    (used once to move older rows onto a new scoring scale). retry_failed empties the
    failures file first, so creators skipped after failed attempts are tried again."""
    creators = load_creators(input_path)
    rules = load_vertical_rules(rules_path)
    # A client passed in is a Claude client, so it means Claude unless told otherwise.
    provider = scoring_provider(provider or ("claude" if client is not None else None))
    if provider == "gemini":
        # Gemini is for small test runs: always live, never the Batch API.
        from src.gemini import gemini_model, has_api_key

        if not has_api_key():
            raise RuntimeError("Set GEMINI_API_KEY to score with Gemini")
        scoring = "live"
        logger.info("Scoring with Gemini (%s), live, one creator at a time", gemini_model())
    elif provider == "ollama":
        # A model on this computer: free, always live. Stop now if Ollama isn't up,
        # before any creator is collected or counted.
        from src.ollama import check_ready, ollama_host, ollama_model

        check_ready()
        scoring = "live"
        logger.info("Scoring with Ollama (%s at %s), live, one creator at a time", ollama_model(), ollama_host())
    else:
        if client is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    "Set ANTHROPIC_API_KEY to score with Claude, or set SCORING_PROVIDER=gemini "
                    "and GEMINI_API_KEY to score with Gemini, or SCORING_PROVIDER=ollama to score on this computer"
                )
            client = anthropic.Anthropic()
        logger.info("Scoring with Claude (live: %s, batch: %s)", live_model(), batch_model())
    hooks = hook_provider(provider)
    if hooks == "claude":
        if client is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    "Set ANTHROPIC_API_KEY so Claude can write the outreach hooks, or set "
                    "HOOK_PROVIDER=none (no hooks) or HOOK_PROVIDER=same (the scoring model writes them)"
                )
            client = anthropic.Anthropic()
        logger.info("Outreach hooks: Claude (%s), only for creators scoring %d or more",
                    live_model(), hook_min_score())
    elif hooks == "none":
        logger.info("Outreach hooks: none (HOOK_PROVIDER=none)")
    else:
        logger.info("Outreach hooks: written by the scoring model, for every creator")
    if not os.getenv("YOUTUBE_API_KEY") and any(c.get("youtube_url") for c in creators):
        raise RuntimeError("The input has YouTube channels; set YOUTUBE_API_KEY first")

    run = Run(output_path, rules, client, poll_seconds, provider, hooks)
    if retry_failed and run.failures.rows:
        logger.info("Clearing %d failed attempts so those creators are tried again", len(run.failures.rows))
        run.failures.rows.clear()
        run.failures.save()
    if run.pending.entries and provider == "claude":
        run.collect_batches()  # finish what a stopped run left behind
    elif run.pending.entries:
        logger.warning("Claude batches from an earlier run are waiting; run with Claude to collect them")

    # Pick the creators that are new or were scored over RESCORE_AFTER_DAYS ago.
    todo: list[tuple[dict[str, str], tuple | None]] = []
    seen_urls, seen_names = set(), set()
    up_to_date = skipped_failures = duplicates = waiting_for_tokens = 0
    new_emails: list[dict[str, object]] = []
    for creator in creators:
        url, name = creator_key(creator), name_key(creator["name"])
        if url in seen_urls or (name and name in seen_names):
            duplicates += 1
            continue
        seen_urls.add(url)
        seen_names.add(name)

        found = run.store.find(creator)
        if found and not rescore_all and not is_stale(found[1], RESCORE_AFTER_DAYS):
            up_to_date += 1
            row = found[1]
            if creator.get("email") and creator["email"] != row.get("email"):
                # Not re-scored, but the new email lets it go to HubSpot now.
                row["email"] = creator["email"]
                if row.get("hubspot_status") != SYNCED:
                    new_emails.append(row)
            continue
        if run.failures.should_skip(creator):
            skipped_failures += 1
            continue
        todo.append((creator, found))

    # TikTok comes from a Bright Data job: start it now, collect everyone else while it
    # runs, then finish the creators with a TikTok once it's ready.
    tiktok_jobs = None
    tiktok_results: dict[str, object] = {}
    token = brightdata_token()
    with_tiktok = [(c, f) for c, f in todo if tiktok_handle(c)]
    if with_tiktok and token:
        tiktok_jobs = TikTokJobs(token)
        try:
            tiktok_jobs.start([tiktok_handle(c) for c, _ in with_tiktok])
        except (requests.RequestException, ValueError) as error:
            logger.warning("Couldn't start the TikTok job; TikTok is left out this run: %s", error)
            tiktok_results = {tiktok_handle(c): error for c, _ in with_tiktok}
            tiktok_jobs = None
    elif with_tiktok:
        logger.warning("%d creators have TikTok but BRIGHTDATA_API_TOKEN is not set", len(with_tiktok))
    deferred = [item for item in todo if tiktok_jobs and tiktok_handle(item[0])]
    deferred_ids = {id(creator) for creator, _ in deferred}
    rest = [item for item in todo if id(item[0]) not in deferred_ids]
    # Instagram allows about 200 calls an hour, so its creators go after everyone else
    # (but before the TikTok group, whose job runs in the background meanwhile).
    slow = (lambda c: bool(instagram_handle(c))) if instagram_credentials() else (lambda c: False)
    ordered = [item for item in rest if not slow(item[0])] + [item for item in rest if slow(item[0])]

    # Every BATCH_SIZE creators go to a batch straight away, while collection carries on.
    queue: list[dict[str, object]] = []
    batching = scoring == "batch"

    def collect(creator: dict[str, str], found: tuple | None) -> None:
        nonlocal queue, batching, waiting_for_tokens
        handle = tiktok_handle(creator)
        try:
            evidence = collect_evidence(
                creator, rules["signal_keywords"], tiktok_results.get(handle) if handle else None
            )
        except MissingCredentials as error:
            waiting_for_tokens += 1  # not a failure: it'll work once the token is set
            logger.warning("Skipping %s for now: %s", creator["name"], error)
            return
        except UnsupportedSource as error:
            run.fail(creator, error, permanent=True)
            return
        except (requests.RequestException, ValueError) as error:
            run.fail(creator, error)
            return
        for match in evidence["review_matches"]:
            run.reviews.add(creator, match)
        score, breakdown = base_score(evidence, is_label(creator), rules["scoring"])
        queue.append({
            **creator,
            **evidence,
            "base_score": score,
            "score_breakdown": breakdown,
            "limited_data": limited_data(evidence, is_label(creator)),
            "replace_key": found[0] if found else None,
        })
        if scoring != "live" and len(queue) >= BATCH_SIZE:
            batching = True
            run.score_in_batch(queue)
            queue = []

    for position, (creator, found) in enumerate(ordered, start=1):
        if position % 50 == 0:
            run.failures.save()
        collect(creator, found)
    if tiktok_jobs:
        try:
            tiktok_results = tiktok_jobs.finish()
        except (requests.RequestException, ValueError) as error:
            logger.warning("The TikTok job failed; TikTok is left out this run: %s", error)
            tiktok_results = {tiktok_handle(c): error for c, _ in deferred}
        for creator, found in deferred:
            collect(creator, found)

    if scoring == "auto" and len(queue) >= BATCH_THRESHOLD:
        batching = True
    if batching and queue:
        run.score_in_batch(queue)
    else:
        for record in queue:
            run.score_live(record)
    if provider == "claude":
        run.collect_batches()

    counts = deliver(
        run.store,
        run.scored + new_emails,
        rules["activation_min_score"],
        webhook_url,
        webhook_format,
        hubspot,
    )
    run.save()
    run.store.save_unsent(run.files["unsent"])
    logger.info(
        "Scored %d creators (webhook: %d sent, %d failed; HubSpot: %d synced, %d failed); "
        "%d up to date; %d duplicates in input; %d failed this run; "
        "%d skipped after %d failed attempts; %d waiting for an Instagram or TikTok token",
        len(run.scored), counts["webhook_sent"], counts["webhook_failed"],
        counts["hubspot_synced"], counts["hubspot_failed"], up_to_date, duplicates,
        run.failed, skipped_failures, MAX_ATTEMPTS, waiting_for_tokens,
    )
    return sorted(run.scored, key=lambda row: int(row["score"]), reverse=True)


def resend_unsent(
    rules_path: str | Path,
    output_path: str | Path,
    webhook_url: str | None,
    webhook_format: str = "clay",
    hubspot: str | None = None,
) -> dict[str, int]:
    """Retry delivery for creators whose send failed or who were held back (no webhook,
    no HubSpot token, or no email at the time). No scoring happens."""
    if not webhook_url and not hubspot:
        raise RuntimeError("Set ACTIVATION_WEBHOOK_URL or HUBSPOT_ACCESS_TOKEN to resend creators")
    rules = load_vertical_rules(rules_path)
    store = CreatorStore(output_path)
    min_score = rules["activation_min_score"]
    webhook_rows = [
        row for row in store.rows.values()
        if webhook_url and row.get("webhook_status") in (FAILED, NO_WEBHOOK)
    ]
    hubspot_rows = [
        row for row in store.rows.values()
        if hubspot and row.get("hubspot_status") in (FAILED, NO_EMAIL, NOT_CONFIGURED, "")
    ]
    sent, webhook_failed = _send_webhook(store, webhook_rows, min_score, webhook_url, webhook_format)
    synced, hubspot_failed = _sync_hubspot(store, hubspot_rows, min_score, hubspot)
    store.save()
    store.save_unsent(run_files(output_path)["unsent"])
    logger.info(
        "Webhook: resent %d, %d still failed. HubSpot: synced %d, %d failed",
        sent, webhook_failed, synced, hubspot_failed,
    )
    return {
        "webhook_sent": sent,
        "webhook_failed": webhook_failed,
        "hubspot_synced": synced,
        "hubspot_failed": hubspot_failed,
    }


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Enrich creator prospects for a GTM workflow.")
    parser.add_argument("--input", default="data/input_creators.csv")
    parser.add_argument("--rules", default="config/vertical_rules.json")
    parser.add_argument("--output", default="data/enriched_creators.csv")
    parser.add_argument(
        "--scoring",
        choices=("auto", "batch", "live"),
        default="auto",
        help=f"batch: Message Batches API (half price, can take up to 24h); live: one call per "
        f"creator; auto: batch when {BATCH_THRESHOLD} or more creators need scoring",
    )
    parser.add_argument(
        "--provider",
        choices=("claude", "gemini", "ollama"),
        help="scoring model provider (default: SCORING_PROVIDER; if unset, gemini when only "
        "GEMINI_API_KEY is set, otherwise claude). Gemini is for "
        "small test runs; ollama uses a free model on this computer. Both score live, "
        "one creator at a time",
    )
    parser.add_argument(
        "--rescore-all",
        action="store_true",
        help="score every creator in the input again, even if scored recently "
        "(e.g. to move older rows onto the current scoring scale)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="clear the failures file first, so creators that failed before are tried again",
    )
    parser.add_argument(
        "--resend-unsent",
        action="store_true",
        help="only retry the webhook and HubSpot for creators not yet delivered (no scoring)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    webhook_url = os.getenv("ACTIVATION_WEBHOOK_URL") or None
    webhook_format = os.getenv("ACTIVATION_WEBHOOK_FORMAT") or "clay"
    hubspot = hubspot_token()
    if webhook_format not in WEBHOOK_FORMATS:
        parser.exit(1, f"error: ACTIVATION_WEBHOOK_FORMAT must be one of {', '.join(WEBHOOK_FORMATS)}\n")

    try:
        if args.resend_unsent:
            resend_unsent(args.rules, args.output, webhook_url, webhook_format, hubspot)
        else:
            run_pipeline(
                args.input,
                args.rules,
                args.output,
                webhook_url=webhook_url,
                webhook_format=webhook_format,
                scoring=args.scoring,
                hubspot=hubspot,
                rescore_all=args.rescore_all,
                provider=args.provider,
                retry_failed=args.retry_failed,
            )
    except (OSError, ValueError, RuntimeError, requests.RequestException, anthropic.APIError) as error:
        parser.exit(1, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
