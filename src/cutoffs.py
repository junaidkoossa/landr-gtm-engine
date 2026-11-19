"""Score cutoffs per target type: creators and labels each have their own.

The rules file sets them under "cutoffs":

    "cutoffs": {"creator": {"enrich": 6, "hook": 7, "hubspot": 7},
                "label":   {"enrich": 6, "hook": 7, "hubspot": 7}}

- enrich: leads at or above it go to enrichment (Clay, Apollo or Hunter).
- hook: Claude writes an outreach hook for leads at or above it, once the score is final.
- hubspot: leads at or above it (with an email) go to HubSpot.

ENRICH_MIN_SCORE, HOOK_MIN_SCORE and HUBSPOT_MIN_SCORE, when set in the environment,
override the rules file for both types (handy for a one-off run).
"""
import os

from src.settings import ENRICH_MIN_SCORE, HOOK_MIN_SCORE, HUBSPOT_MIN_SCORE

TARGET_TYPES = ("creator", "label")
# kind -> (environment override, default)
KINDS = {
    "enrich": ("ENRICH_MIN_SCORE", ENRICH_MIN_SCORE),
    "hook": ("HOOK_MIN_SCORE", HOOK_MIN_SCORE),
    "hubspot": ("HUBSPOT_MIN_SCORE", HUBSPOT_MIN_SCORE),
}
LABEL_TYPES = {"label", "labels"}


def default_cutoffs() -> dict[str, dict[str, int]]:
    return {target: {kind: default for kind, (_, default) in KINDS.items()} for target in TARGET_TYPES}


def _valid_score(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
        raise ValueError(f"{where} must be a whole number from 1 to 10, not {value!r}")
    return value


def validate_cutoffs(value: object) -> dict[str, dict[str, int]]:
    """The rules file's cutoffs, with defaults for anything left out."""
    if value is None:
        return default_cutoffs()
    if not isinstance(value, dict):
        raise ValueError("'cutoffs' must be an object with 'creator' and 'label' sections")
    unknown = set(value) - set(TARGET_TYPES)
    if unknown:
        raise ValueError(f"'cutoffs' has unknown target types: {', '.join(sorted(unknown))}")
    cutoffs = default_cutoffs()
    for target, kinds in value.items():
        if not isinstance(kinds, dict):
            raise ValueError(f"'cutoffs.{target}' must be an object")
        unknown = set(kinds) - set(KINDS)
        if unknown:
            raise ValueError(f"'cutoffs.{target}' has unknown cutoffs: {', '.join(sorted(unknown))}")
        for kind, score in kinds.items():
            cutoffs[target][kind] = _valid_score(score, f"cutoffs.{target}.{kind}")
    return cutoffs


def env_override(kind: str) -> int | None:
    name = KINDS[kind][0]
    value = os.getenv(name)
    if not value:
        return None
    try:
        score = int(value)
    except ValueError:
        raise ValueError(f"{name} must be a whole number from 1 to 10, not {value!r}") from None
    return _valid_score(score, name)


def target_of(row: dict[str, object]) -> str:
    return "label" if str(row.get("target_type") or "").strip().casefold() in LABEL_TYPES else "creator"


class Cutoffs:
    """The cutoff that applies to each lead, by its target type."""

    def __init__(self, rules: dict[str, object] | None = None):
        table = (rules or {}).get("cutoffs")
        self.table = validate_cutoffs(table) if table is not None else default_cutoffs()

    def get(self, target: str, kind: str) -> int:
        override = env_override(kind)
        return override if override is not None else self.table[target][kind]

    def for_row(self, row: dict[str, object], kind: str) -> int:
        return self.get(target_of(row), kind)

    def describe(self, kind: str) -> str:
        creator, label = self.get("creator", kind), self.get("label", kind)
        return str(creator) if creator == label else f"{creator} for creators, {label} for labels"
