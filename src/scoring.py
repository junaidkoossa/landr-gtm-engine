"""Per-product scores, worked out in code from the evidence and the rules file.

Every lead gets a score out of 10 for each LANDR product it could buy or supply:

- mastering: AI mastering plugin and subscriptions (creators);
- distribution: release to digital streaming platforms (creators and small labels);
- samples: supplying packs to LANDR Samples (sample-pack creators and labels);
- plugins: LANDR's own plugins, sold to musicians, producers and mixing engineers (creators).

Each product adds up points for signals (weights in config/vertical_rules.json under
"products"). A signal no source could check counts half its points, so a lead is neither
buried nor promoted for missing data; when under LIMITED_SHARE of the points our own
sources could check (signals only enrichment answers are left out) were checked, the
product is marked limited and the lead goes to Clay whatever its score. The lead's score is its best product's
score, and the model may then move it by at most 1 point.

Reach in mastering is capped: acts over cap_above get capped_points, because very big
acts usually have a label and an engineer. A stable BIG_ACT_TEST_SHARE of those acts get
the full points instead (big_act_test), to test that theory against replies.
"""
import copy
import hashlib
import re
from datetime import date, datetime, timezone

from src.settings import BIG_ACT_TEST_SHARE

PRODUCTS = ("mastering", "distribution", "samples", "plugins")
PRODUCT_NAMES = {
    "mastering": "Mastering", "distribution": "Distribution", "samples": "Samples supplier", "plugins": "Plugins",
}
# Products a label can be pitched; creators can be pitched all four.
LABEL_PRODUCTS = ("distribution", "samples")
PITCH_ANGLES = {
    "mastering": "They release often: master every track with LANDR's AI mastering plugin or subscription, "
                 "without booking an engineer.",
    "distribution": "They self-release: distribute to every streaming platform with LANDR, or switch from "
                    "their current distributor.",
    "samples": "They make sample packs: supply them to LANDR Samples and reach LANDR's producers.",
    "plugins": "They produce or mix: LANDR's own plugins, built for how they already make music.",
}
DEFAULT_PRODUCTS = {
    "mastering": {
        "releases": {"points": 3, "bands": [6, 12, 24]},
        "recency": {"points": 2, "days": [90, 30]},
        "no_mastering_credit": {"points": 2},
        "self_released": {"points": 1},
        "reach": {"points": 2, "min": 10_000, "cap_above": 1_000_000, "capped_points": 1},
    },
    "distribution": {
        "self_released": {"points": 3},
        "competitor_distributor": {"points": 2, "none_found": 1},
        "releases": {"points": 2, "bands": [6, 12]},
        "recency": {"points": 1, "days": [30]},
        "spotify_listeners": {"points": 2, "bands": [1_000, 10_000]},
    },
    "samples": {
        "sells_sample_packs": {"points": 3},
        "pack_count": {"points": 2, "bands": [10, 50]},
        "new_pack": {"points": 2, "days": [90]},
        "sells_direct": {"points": 1},
        "followers": {"points": 2, "bands": [10_000, 100_000]},
    },
    "plugins": {
        "producer_engineer": {"points": 3},
        "production_content": {"points": 2},
        "third_party_plugins": {"points": 2},
        "releases": {"points": 2, "bands": [6, 12]},
        "recency": {"points": 1, "days": [30]},
    },
}
MAX_ADJUSTMENT = 1
LIMITED_SHARE = 0.6
# Signals only enrichment (Clay) can usually answer. They are left out of the limited-data
# share: before enrichment they're unknown for nearly everyone, so counting them would
# send every lead to Clay.
ENRICHMENT_ONLY = {"no_mastering_credit", "self_released", "spotify_listeners", "pack_count", "new_pack"}

# Phrases found in page text and video titles (whole words, any case), and link hosts.
KEYWORD_SIGNALS = {
    "producer_engineer": (
        "mixing engineer", "mix engineer", "mastering engineer", "audio engineer", "recording engineer",
        "producer", "mixed by", "engineered by", "mixing and mastering", "mix and master",
        # Beatmakers (Instagram/TikTok triggers): they make beats for others.
        "beatmaker", "beat maker", "type beat", "type beats", "beats for sale", "buy beats",
    ),
    "production_content": (
        "tutorial", "how to make", "how i made", "beat making", "beatmaking", "cookup", "cook up",
        "making a beat", "plugin review", "gear review", "in the studio", "studio vlog", "sound design",
        # Showing the process with a DAW (Instagram/TikTok triggers).
        "beat breakdown", "fl studio", "ableton", "logic pro", "pro tools", "bitwig", "studio one",
    ),
    "third_party_plugins": (
        "vst", "vst3", "plugin", "plugins", "serum", "fabfilter", "waves", "izotope", "soundtoys",
        "kontakt", "omnisphere", "valhalla", "arturia", "native instruments", "u-he", "xfer",
    ),
    "sells_sample_packs": (
        "sample pack", "sample packs", "drum kit", "drum kits", "loop kit", "loop kits", "sound kit",
        "sound kits", "sound library", "preset pack", "midi pack", "one shots", "one-shots",
    ),
    "self_released": ("self-released", "self released", "independent artist", "not on label"),
}
FLAG_WORDS = {
    "producer_engineer": ("produces or mixes for others", "no producing or mixing for others"),
    "production_content": ("makes production content", "no production content"),
    "sells_sample_packs": ("sells sample packs", "no sample packs"),
    "third_party_plugins": ("uses third-party plugins", "no third-party plugins mentioned"),
}
COMPETITOR_DISTRIBUTORS = {
    "distrokid": ("distrokid.com", "distrokid", "hyperfollow"),
    "tunecore": ("tunecore.com", "tunecore"),
    "cdbaby": ("cdbaby.com", "cd baby"),
    "amuse": ("amuse.io",),
    "ditto": ("dittomusic.com", "ditto music"),
    "unitedmasters": ("unitedmasters.com", "unitedmasters"),
    "routenote": ("routenote.com", "routenote"),
    "onerpm": ("onerpm.com", "onerpm"),
}
# Beat stores: a producer selling beats to artists.
PRODUCER_HOSTS = ("beatstars.com", "airbit.com", "traktrain.com")
_PATTERNS = {
    name: re.compile(r"(?<![\w-])(" + "|".join(re.escape(p) for p in phrases) + r")(?![\w-])", re.IGNORECASE)
    for name, phrases in KEYWORD_SIGNALS.items()
}


def detect_signals(text: str, links: list[str] | None = None) -> list[str]:
    """Product signals found in a lead's text and links: keyword signal names, plus
    "distributor:<name>" for each competitor distributor linked or named."""
    found = [name for name, pattern in _PATTERNS.items() if pattern.search(text or "")]
    if "producer_engineer" not in found and any(host in link.lower() for link in links or [] for host in PRODUCER_HOSTS):
        found.append("producer_engineer")
    haystack = f"{text or ''} {' '.join(links or [])}".lower()
    for name, needles in COMPETITOR_DISTRIBUTORS.items():
        if any(needle in haystack for needle in needles):
            found.append(f"distributor:{name}")
    return found


def default_products() -> dict:
    return copy.deepcopy(DEFAULT_PRODUCTS)


def _rising(values: object, positive: bool = True) -> bool:
    return (
        isinstance(values, list) and bool(values)
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and (v > 0 or not positive) for v in values)
    )


def validate_products(products: object) -> dict:
    """Check a "products" section from the rules file; raise ValueError if it's malformed.

    Each product must be listed; its signals must be ones the code knows for that
    product (one can be dropped to stop counting it), with whole-number points."""
    if not isinstance(products, dict) or set(products) != set(PRODUCTS):
        raise ValueError(f"'products' must have exactly: {', '.join(PRODUCTS)}")
    for product, signals in products.items():
        known = DEFAULT_PRODUCTS[product]
        if not isinstance(signals, dict) or not signals:
            raise ValueError(f"'products.{product}' must list its signals")
        unknown = [name for name in signals if name not in known]
        if unknown:
            raise ValueError(f"'products.{product}' has unknown signals: {', '.join(unknown)} "
                             f"(known: {', '.join(known)})")
        for name, settings in signals.items():
            where = f"'products.{product}.{name}"
            points = settings.get("points") if isinstance(settings, dict) else None
            if isinstance(points, bool) or not isinstance(points, int) or points < 0:
                raise ValueError(f"{where}.points' must be a whole number, 0 or more")
            for key in ("bands", "days"):
                if key in known[name]:
                    values = settings.get(key)
                    if not _rising(values):
                        raise ValueError(f"{where}.{key}' must be a list of numbers above 0")
                    if key == "bands" and values != sorted(values):
                        raise ValueError(f"{where}.bands' must rise")
            for key in ("min", "cap_above", "capped_points", "none_found"):
                if key in known[name] and not isinstance(settings.get(key), (int, float)):
                    raise ValueError(f"{where}.{key}' must be a number")
    return products


def _short(number: float | int | None) -> str:
    if number is None:
        return "unknown"
    for limit, suffix in ((1_000_000, "M"), (1_000, "K")):
        if number >= limit:
            return f"{number / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(number))


def _date(value: object) -> date | None:
    text = str(value or "")
    for length, pattern in ((10, "%Y-%m-%d"), (7, "%Y-%m")):
        if len(text) >= length:
            try:
                return datetime.strptime(text[:length], pattern).date()
            except ValueError:
                pass
    return None


def yes_no(value: object) -> bool | None:
    """True, False, or None (unknown) from a figure Clay or a person filled in."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in ("yes", "y", "true", "1"):
        return True
    if text in ("no", "n", "false", "0"):
        return False
    return None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _share(points: int, met: int, steps: int) -> int:
    return int(points * met / steps + 0.5) if steps else 0


def _flag(evidence: dict, keyword: str, clay_field: str | None = None) -> bool | None:
    """A yes/no signal: Clay's answer when it has one, else whether page text named it
    (None when there was no text or signal list to look in)."""
    if clay_field:
        clay = yes_no(evidence.get(clay_field))
        if clay is not None:
            return clay
    signals = evidence.get("product_signals")
    if signals is None:
        return None
    return keyword in signals


def big_act_test(key: str, share: float = BIG_ACT_TEST_SHARE) -> bool:
    """Whether this lead is in the big-act test group: stable for the same lead."""
    bucket = int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16) / 0x1_0000_0000
    return bucket < share


def _signal(name: str, settings: dict, evidence: dict, today: date, test_group: bool) -> tuple[int | None, str]:
    """(points earned, or None when unknown; a short description)."""
    points = settings["points"]
    if name == "releases":
        count = evidence.get("releases_last_12_months")
        if count is None:
            return None, "releases unknown"
        met = sum(1 for band in settings["bands"] if count >= band)
        return _share(points, met, len(settings["bands"])), f"{count} releases in 12 months"
    if name in ("recency", "new_pack"):
        field = "latest_pack_date" if name == "new_pack" else "latest_release_date"
        when = _date(evidence.get(field))
        label = "latest pack" if name == "new_pack" else "latest release"
        if when is None:
            return None, f"{label} unknown"
        days = (today - when).days
        met = sum(1 for limit in settings["days"] if days <= limit)
        return _share(points, met, len(settings["days"])), f"{label} {days} days ago"
    if name == "no_mastering_credit":
        credit = yes_no(evidence.get("mastering_credit"))
        if credit is None:
            return None, "mastering credit unknown"
        return (0, "has a mastering credit") if credit else (points, "no mastering credit")
    if name == "self_released":
        flag = _flag(evidence, "self_released", "self_released")
        if flag is None or (flag is False and yes_no(evidence.get("self_released")) is None):
            # Page text not saying "self-released" doesn't show a label exists.
            return (points, "self-released") if flag else (None, "label status unknown")
        return (points, "self-released") if flag else (0, "has a label")
    if name == "reach":
        values = [v for v in (_number(evidence.get("spotify_monthly_listeners")), _number(evidence.get("steady_views")))
                  if v is not None]
        if not values:
            return None, "reach unknown"
        reach = max(values)
        if reach < settings["min"]:
            return 0, f"reach {_short(reach)}"
        if reach > settings["cap_above"] and not test_group:
            return min(points, int(settings["capped_points"])), f"reach {_short(reach)}, over the cap"
        note = ", big-act test" if reach > settings["cap_above"] else ""
        return points, f"reach {_short(reach)}{note}"
    if name == "competitor_distributor":
        signals = evidence.get("product_signals")
        if signals is None:
            return None, "distributor unknown"
        names = [s.split(":", 1)[1] for s in signals if s.startswith("distributor:")]
        if names:
            return points, f"uses {', '.join(names)}"
        return min(points, int(settings["none_found"])), "no distributor found"
    if name in ("spotify_listeners", "followers", "pack_count"):
        field = {"spotify_listeners": "spotify_monthly_listeners", "followers": "audience_size",
                 "pack_count": "pack_count"}[name]
        value = _number(evidence.get(field))
        label = {"spotify_listeners": "Spotify listeners", "followers": "followers", "pack_count": "packs"}[name]
        if value is None:
            return None, f"{label} unknown"
        met = sum(1 for band in settings["bands"] if value >= band)
        return _share(points, met, len(settings["bands"])), f"{_short(value)} {label}"
    if name == "sells_direct":
        has_store = bool(evidence.get("storefront_links"))
        return (points, "sells direct") if has_store else (0, "no storefront")
    clay_fields = {"producer_engineer": "producer_or_engineer", "production_content": "makes_production_content",
                   "sells_sample_packs": None, "third_party_plugins": None}
    if name in clay_fields:
        flag = _flag(evidence, name, clay_fields[name])
        if name == "sells_sample_packs" and (_number(evidence.get("pack_count")) or 0) > 0:
            flag = True
        yes, no = FLAG_WORDS[name]
        if flag is None:
            return None, f"{yes} unknown"
        return (points, yes) if flag else (0, no)
    raise ValueError(f"unknown signal {name}")


def score_product(product: str, evidence: dict, signals: dict, today: date,
                  test_group: bool = False) -> dict[str, object]:
    """One product's score (1-10), how much of it could be checked, and its breakdown."""
    total = sum(settings["points"] for settings in signals.values())
    checkable = sum(settings["points"] for name, settings in signals.items() if name not in ENRICHMENT_ONLY)
    earned = checked = 0.0
    parts = []
    for name, settings in signals.items():
        got, text = _signal(name, settings, evidence, today, test_group)
        points = settings["points"]
        if got is None:
            earned += points / 2
            parts.append(f"{text} (half of {points} counted)")
        else:
            earned += got
            if name not in ENRICHMENT_ONLY:
                checked += points
            parts.append(f"{text} {got}/{points}")
    score = max(1, min(10, int(earned / total * 10 + 0.5))) if total else 1
    share = checked / checkable if checkable else 1.0
    return {"score": score, "checked_share": share, "limited": share < LIMITED_SHARE,
            "breakdown": "; ".join(parts)}


def score_products(evidence: dict, is_label: bool, products: dict | None = None, key: str = "",
                   today: date | None = None) -> dict[str, object]:
    """Every product's score, the best product, and the columns the pipeline stores.

    base_score is the best product's score and score_breakdown its breakdown. Products a
    label can't be pitched score 0."""
    products = products or DEFAULT_PRODUCTS
    today = today or datetime.now(timezone.utc).date()
    test_group = big_act_test(key) if key else False
    applicable = LABEL_PRODUCTS if is_label else PRODUCTS
    results = {p: score_product(p, evidence, products[p], today, test_group) for p in applicable}
    best = max(applicable, key=lambda p: (results[p]["score"], -PRODUCTS.index(p)))
    row: dict[str, object] = {f"score_{p}": results[p]["score"] if p in results else 0 for p in PRODUCTS}
    best_result = results[best]
    over_cap = test_group and not is_label and any(
        (_number(evidence.get(f)) or 0) > products["mastering"].get("reach", {}).get("cap_above", float("inf"))
        for f in ("spotify_monthly_listeners", "steady_views")
    )
    row.update({
        "best_product": best,
        "base_score": best_result["score"],
        "score_breakdown": f"{PRODUCT_NAMES[best]}: {best_result['breakdown']}",
        "product_breakdowns": " | ".join(
            f"{PRODUCT_NAMES[p]} {results[p]['score']}: {results[p]['breakdown']}" for p in applicable
        ),
        "limited_data": (
            f"{PRODUCT_NAMES[best]}: only {best_result['checked_share']:.0%} of its points could be checked"
            if best_result["limited"] else ""
        ),
        "big_act_test": "yes" if over_cap else "",
    })
    return row


def final_score(base: int, adjustment: int) -> int:
    return max(1, min(10, int(base) + max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, int(adjustment)))))
