"""
Property type standardization — shared across every state spider.

Different states (and even different towns within the same state, see
NH's Lincoln vs. Lebanon) use completely different vocabulary for the
same underlying property-type concept: NH/VGSI says "one fam", MA/MassGIS
says "Single Family Residential", GRANIT-fed NH towns say "Single
Family" -- all the same thing downstream. This used to be solved with a
separate hardcoded dict per spider (NH's vgsi_assessment_scraper.py had
its own LAND_USE_STANDARDIZATION); this module replaces every one of
those with a single shared CSV + one lookup function, so a new state
adds rows to a CSV, not a new function in a new file.

MAPPING FILE: property_type_mapping.csv, alongside this file. Two
columns:
    standardized,raw_aliases
    Single Family,single family|one fam|single family residential
    Vacant Land,vacant land|developable residential land

The first column is the canonical name every spider should emit. The
second is a "|"-separated list of every raw source value (from any
state) that maps to it. Adding a new alias, or a whole new standardized
category, means editing the CSV -- not writing code.

MATCHING: case-insensitive and outer-whitespace-trimmed, but otherwise
an EXACT match -- no substring/fuzzy matching, so an unfamiliar raw
value never silently gets merged into the wrong bucket. Matches are
literal beyond casing/trim: if a real source value has internal double
spaces (confirmed real case: NH VGSI's "Res  PUD"), the CSV alias must
have them too.

UNRECOGNIZED VALUES: same convention as the dict this replaces -- a raw
value with no match in the CSV is returned UNCHANGED (not blanked to
None, not guessed at). Standardization only touches the values actually
listed in the CSV; every other property_type (e.g. a new commercial
subtype nobody's seen yet) passes through as-is and behaves as "unknown"
downstream, until someone adds a row for it.
"""

import csv
from pathlib import Path
from functools import lru_cache

MAPPING_CSV_PATH = Path(__file__).parent / "property_type_mapping.csv"


@lru_cache(maxsize=1)
def _load_mapping() -> dict[str, str]:
    """Returns {lowercased raw alias: standardized name}. Cached so the
    CSV is only ever read once per process, no matter how many towns or
    spiders call standardize_property_type()."""
    mapping: dict[str, str] = {}
    with open(MAPPING_CSV_PATH, newline="") as f:
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
                    raise ValueError(
                        f"property_type_mapping.csv: '{raw}' is mapped to both "
                        f"'{mapping[key]}' and '{standardized}' -- ambiguous, fix the CSV"
                    )
                mapping[key] = standardized
            # A standardized name is also its own valid input (e.g. NH's
            # GRANIT/VGSI join already emits "Single Family" directly for
            # some towns) -- registering it here means callers never need
            # a separate "is it already standardized?" check.
            mapping.setdefault(standardized.lower(), standardized)
    return mapping


def standardize_property_type(raw_value: str | None) -> str | None:
    """Look up raw_value (case-insensitive, outer-trimmed) in the shared
    CSV mapping. Returns the standardized name if found, otherwise
    returns raw_value UNCHANGED (just outer-trimmed) so unmapped types
    aren't silently lost. None in, None out."""
    if raw_value is None:
        return None
    trimmed = raw_value.strip()
    if not trimmed:
        return None
    return _load_mapping().get(trimmed.lower(), trimmed)