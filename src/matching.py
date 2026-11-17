"""Name normalisation and label-name matching.

Label names match in two ways:
- exact: the same letters and digits, ignoring case, spaces and punctuation
  ("Monster Cat" = "Monstercat");
- variant: the same words once plurals and common label filler words are set aside
  ("Ninja Tune" ~ "Ninja Tunes", "Hospital" ~ "Hospital Records", "Cymatics" ~ "Cymatics.fm").
Any other difference, even one letter inside a word ("Spice" vs "Splice"), is no match.
Variant matches are used but flagged for review.
"""
import re

# Words labels often add or drop ("Loopmasters Ltd", "Hospital Records", "Cymatics.fm").
LABEL_FILLER_WORDS = {
    "the", "records", "record", "recordings", "music", "label", "labels", "audio",
    "sound", "sounds", "ltd", "llc", "inc", "limited", "co", "group", "entertainment",
    "fm", "com", "net", "io",
}


def name_key(name: str) -> str:
    """Case, spacing and punctuation-insensitive form of a name, used to spot duplicates."""
    return re.sub(r"[^0-9a-z]+", "", name.casefold())


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def variant_key(name: str) -> str:
    """Words of a name with plurals made singular and label filler words removed."""
    words = [_singular(word) for word in re.findall(r"[0-9a-z]+", name.casefold())]
    core = [word for word in words if word not in LABEL_FILLER_WORDS]
    return "".join(core or words)


def match_type(wanted: str, candidate: str) -> str | None:
    """"exact", "variant", or None when the names are different."""
    if name_key(wanted) and name_key(wanted) == name_key(candidate):
        return "exact"
    if variant_key(wanted) and variant_key(wanted) == variant_key(candidate):
        return "variant"
    return None


def best_match(name: str, candidates: list[tuple[str, object]]):
    """The first exact match, else the first variant match, as (name, value, type); or None."""
    variant = None
    for candidate, value in candidates:
        kind = match_type(name, candidate)
        if kind == "exact":
            return candidate, value, kind
        if kind == "variant" and variant is None:
            variant = (candidate, value, kind)
    return variant
