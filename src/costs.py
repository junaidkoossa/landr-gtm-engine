"""What each run spends, logged to one cost file next to the output CSV.

Every model call (scoring and hooks) logs its token counts, and every lead sent to
Clay (or looked up on Apollo or Hunter) logs one row. Each line is priced from
config/costs.json when it is written, so the cost file (default
data/enriched_creators.costs.csv) holds both kinds of cost side by side and adds up to
the run's final cost:

    logged_at, run_id, lead_key, name, kind, task, provider, model, batch,
    input_tokens, output_tokens, rows, cost_usd

kind is "tokens" (a model call) or "rows" (a flat per-row charge). cost_usd is blank
when the model has no price in config/costs.json; the report lists those.

Logging is on only while a run is open (start_run ... finish_run in src/main.py), so
tests and one-off calls log nothing. A failed log line is a warning, never a stopped run.
"""
import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.store import creator_key, now_iso, related_path

logger = logging.getLogger(__name__)

DEFAULT_PRICES_PATH = Path("config/costs.json")
COST_COLUMNS = (
    "logged_at", "run_id", "lead_key", "name", "kind", "task", "provider", "model", "batch",
    "input_tokens", "output_tokens", "rows", "cost_usd",
)
TOKENS = "tokens"
ROWS = "rows"

_ledger: "CostLedger | None" = None


def costs_path(output_path: str | Path) -> Path:
    return related_path(output_path, "costs.csv")


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def load_prices(path: str | Path = DEFAULT_PRICES_PATH) -> dict:
    """config/costs.json, or empty prices (every cost blank) if the file is missing."""
    path = Path(path)
    if not path.exists():
        logger.warning("No %s: costs are logged without prices", path)
        return {"token_prices": {}, "row_prices": {}, "batch_discount": 1.0}
    prices = json.loads(path.read_text(encoding="utf-8"))
    prices.setdefault("token_prices", {})
    prices.setdefault("row_prices", {})
    prices.setdefault("batch_discount", 1.0)
    return prices


def token_cost(prices: dict, model: str, input_tokens: int, output_tokens: int,
               batch: bool = False) -> float | None:
    """Dollars for one call, or None if the model has no price."""
    price = prices["token_prices"].get(model)
    if price is None:
        return None
    cost = (input_tokens * price["input_per_million"] + output_tokens * price["output_per_million"]) / 1_000_000
    return cost * prices["batch_discount"] if batch else cost


def row_cost(prices: dict, provider: str, rows: int) -> float | None:
    """Dollars for rows sent to a provider, or None if its price isn't set (missing or 0:
    Clay, Apollo and Hunter rows are never free, so 0 means "not filled in yet")."""
    price = prices["row_prices"].get(provider)
    return price * rows if price else None


class CostLedger:
    """Appends priced lines to the cost file as they happen, so a stopped run keeps its costs."""

    def __init__(self, path: str | Path, run_id: str, prices: dict):
        self.path = Path(path)
        self.run_id = run_id
        self.prices = prices
        self.total = 0.0
        self.unpriced: set[str] = set()

    def _write(self, line: dict[str, object]) -> None:
        line = {"logged_at": now_iso(), "run_id": self.run_id, **line}
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=COST_COLUMNS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow(line)

    def tokens(self, task: str, provider: str, model: str, input_tokens: int, output_tokens: int,
               lead: dict | None = None, batch: bool = False) -> None:
        cost = token_cost(self.prices, model, input_tokens, output_tokens, batch)
        self._add(cost, model)
        self._write({
            **_lead_columns(lead), "kind": TOKENS, "task": task, "provider": provider, "model": model,
            "batch": "yes" if batch else "", "input_tokens": input_tokens, "output_tokens": output_tokens,
            "rows": "", "cost_usd": _money(cost),
        })

    def rows(self, provider: str, count: int = 1, lead: dict | None = None, task: str = "enrich") -> None:
        cost = row_cost(self.prices, provider, count)
        self._add(cost, provider)
        self._write({
            **_lead_columns(lead), "kind": ROWS, "task": task, "provider": provider, "model": "",
            "batch": "", "input_tokens": "", "output_tokens": "", "rows": count, "cost_usd": _money(cost),
        })

    def _add(self, cost: float | None, name: str) -> None:
        if cost is None:
            self.unpriced.add(name)
        else:
            self.total += cost


def _lead_columns(lead: dict | None) -> dict[str, object]:
    if not lead:
        return {"lead_key": "", "name": ""}
    return {"lead_key": creator_key(lead), "name": lead.get("name") or ""}


def _money(cost: float | None) -> str:
    return "" if cost is None else f"{cost:.6f}"


# The run-wide ledger. Model and enrichment code call log_tokens / log_rows; they do
# nothing unless src/main.py has started a run.

def start_run(output_path: str | Path, run_id: str | None = None,
              prices_path: str | Path = DEFAULT_PRICES_PATH) -> CostLedger:
    global _ledger
    _ledger = CostLedger(costs_path(output_path), run_id or new_run_id(), load_prices(prices_path))
    return _ledger


def finish_run() -> CostLedger | None:
    """Close the run's ledger and log what it spent."""
    global _ledger
    ledger, _ledger = _ledger, None
    if ledger:
        logger.info("Run %s cost $%.4f (details in %s)", ledger.run_id, ledger.total, ledger.path)
        if ledger.unpriced:
            logger.warning("No price in config/costs.json for: %s (their cost is left blank)",
                           ", ".join(sorted(ledger.unpriced)))
    return ledger


def current_run_id() -> str | None:
    return _ledger.run_id if _ledger else None


def log_tokens(task: str, provider: str, model: str, input_tokens: int | None, output_tokens: int | None,
               lead: dict | None = None, batch: bool = False) -> None:
    if _ledger is None:
        return
    try:
        _ledger.tokens(task, provider, model, int(input_tokens or 0), int(output_tokens or 0), lead, batch)
    except (OSError, ValueError, TypeError, KeyError) as error:
        logger.warning("Couldn't log the cost of a %s call: %s", provider, error)


def log_rows(provider: str, count: int = 1, lead: dict | None = None, task: str = "enrich") -> None:
    if _ledger is None or count <= 0:
        return
    try:
        _ledger.rows(provider, count, lead, task)
    except (OSError, ValueError, TypeError, KeyError) as error:
        logger.warning("Couldn't log the cost of %d %s rows: %s", count, provider, error)


def claude_usage(message) -> tuple[int, int]:
    """Input and output tokens of a Claude reply. Cache reads and writes count as input
    (the engine doesn't use prompt caching, so they are normally 0)."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return 0, 0
    total_in = sum(
        int(getattr(usage, field, 0) or 0)
        for field in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    return total_in, int(getattr(usage, "output_tokens", 0) or 0)


def gemini_usage(response) -> tuple[int, int]:
    """Gemini bills thinking tokens as output."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0, 0
    output = int(getattr(usage, "candidates_token_count", 0) or 0) + int(getattr(usage, "thoughts_token_count", 0) or 0)
    return int(getattr(usage, "prompt_token_count", 0) or 0), output


def read_costs(path: str | Path) -> list[dict[str, str]]:
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))
