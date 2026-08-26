"""
Property type standardization — shared across every state spider.

Different states (and even different towns within the same state, see
NH's Lincoln vs. Lebanon) use completely different vocabulary for the
same underlying property-type concept: NH/VGSI says "one fam", MA/MassGIS
says "Single Family Residential", GRANIT-fed NH towns say "Single
Family" -- all the same thing downstream. This used to be solved with a
separate hardcoded dict per spider (NH's vgsi_assessment_scraper.py had
its own LAND_USE_STANDARDIZATION); this module replaces every one of
those with a single lookup function, so a new state adds rows to a csv,
not a new function in a new file.

MAPPING FILES: one property_type_mapping.csv PER STATE, living alongside
that state's spider (spiders/ct/property_type_mapping.csv,
spiders/vt/property_type_mapping.csv, etc.) -- NOT a single shared file
anymore. This module discovers every spiders/*/property_type_mapping.csv
at load time (skipping spiders/common/ itself, which hosts this module
but no mapping file of its own) and merges them into one canonical
lookup dict, same as if they were still one file. This replaces the
single spiders/common/property_type_mapping.csv this module used to
read directly -- if you're looking for that file, it no longer exists;
each state's rows moved to that state's own spider directory.

WHY PER-STATE FILES, SAME MERGED DICT: splitting the file so each
state's actual contributed aliases are visible in one place (open
spiders/ct/property_type_mapping.csv, see exactly what CT needed,
without scrolling past 50 states' worth of aliases) -- while keeping
the SAME collision-checked merge behavior as a single file, so the
underlying vocabulary is still enforced as one canonical set. If two
states' files ever disagree about what the same raw value should
standardize to, loading still fails loudly (see _load_mapping() below)
instead of silently drifting into two different buckets that some
downstream comps/gap-analysis code has no way to detect.

Each file uses the same two columns:
    standardized,raw_aliases
    Single Family,single family|one fam|single family residential
    Vacant Land,vacant land|developable residential land

The first column is the canonical name every spider should emit. The
second is a "|"-separated list of every raw source value that maps to
it. A state does NOT have to contribute every standardized category --
e.g. VT's file only has rows for "Single Family" and "Vacant Land",
deliberately not adding "Mobile Home"/"Commercial"/"Farm"/"Utility" as
new shared categories just because VT's own source data happens to
have those concepts (see spiders/vt/vt_spider.py's VT_CAT_DESC comments
for the reasoning) -- that curation choice lives in what rows a state's
CSV contains, not in a separate code path. Adding a new alias, or a
whole new standardized category, means editing that state's CSV -- not
writing code, and not touching every other state's file.

MATCHING: case-insensitive and outer-whitespace-trimmed, but otherwise
an EXACT match -- no substring/fuzzy matching, so an unfamiliar raw
value never silently gets merged into the wrong bucket. Matches are
literal beyond casing/trim: if a real source value has internal double
spaces (confirmed real case: NH VGSI's "Res  PUD"), the CSV alias must
have them too.

UNRECOGNIZED VALUES: same convention as the dict this replaces -- a raw
value with no match in any state's CSV is returned UNCHANGED (not
blanked to None, not guessed at). Standardization only touches the
values actually listed in some state's CSV; every other property_type
(e.g. a new commercial subtype nobody's seen yet) passes through as-is
and behaves as "unknown" downstream, until someone adds a row for it.
"""

import csv
from pathlib import Path
from functools import lru_cache

# spiders/common/property_types.py -> parent is spiders/common/, parent.parent is spiders/
SPIDERS_DIR = Path(__file__).parent.parent


def _discover_mapping_files() -> list[Path]:
    """Every spiders/<state>/property_type_mapping.csv, sorted for a
    deterministic load order (matters for which file's error message
    surfaces first on a collision -- see _load_mapping() below).
    spiders/common/ is explicitly excluded: it hosts this module, not a
    per-state mapping file, by design -- there is no "common" state."""
    files = []
    for child in sorted(SPIDERS_DIR.iterdir()):
        if not child.is_dir() or child.name == "common":
            continue
        candidate = child / "property_type_mapping.csv"
        if candidate.exists():
            files.append(candidate)
    return files


@lru_cache(maxsize=1)
def _load_mapping() -> dict[str, str]:
    """Returns {lowercased raw alias: standardized name}, merged across
    every state's own property_type_mapping.csv. Cached so every file is
    only ever read once per process, no matter how many towns or spiders
    call standardize_property_type()."""
    mapping: dict[str, str] = {}
    for csv_path in _discover_mapping_files():
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                standardized = (row.get("standardized") or "").strip()
                raw_aliases = (row.get("raw_aliases") or "").strip()
                if not standardized:
                    continue  # skip blank/malformed rows rather than guessing
                for raw in raw_aliases.split("|"):
                    raw = raw.strip()
                    if not raw:
                        continue
                    key = raw.lower()
                    if key in mapping and mapping[key] != standardized:
                        # Names the offending file explicitly -- with one
                        # shared file this used to just say "the CSV";
                        # with per-state files, knowing WHICH state's
                        # file caused the collision is the whole point.
                        raise ValueError(
                            f"{csv_path}: '{raw}' is mapped to '{standardized}', but "
                            f"another state's property_type_mapping.csv already maps "
                            f"'{raw}' to '{mapping[key]}' -- ambiguous across states, "
                            f"fix one of the two CSVs"
                        )
                    mapping[key] = standardized
                # A standardized name is also its own valid input (e.g.
                # NH's GRANIT/VGSI join already emits "Single Family"
                # directly for some towns) -- registering it here means
                # callers never need a separate "is it already
                # standardized?" check.
                mapping.setdefault(standardized.lower(), standardized)
    return mapping


def standardize_property_type(raw_value: str | None) -> str | None:
    """Look up raw_value (case-insensitive, outer-trimmed) across every
    state's merged CSV mapping. Returns the standardized name if found,
    otherwise returns raw_value UNCHANGED (just outer-trimmed) so
    unmapped types aren't silently lost. None in, None out."""
    if raw_value is None:
        return None
    trimmed = raw_value.strip()
    if not trimmed:
        return None
    return _load_mapping().get(trimmed.lower(), trimmed)