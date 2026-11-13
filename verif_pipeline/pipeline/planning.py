"""Offline rain-gauge network planning (pure Python, no network access).

The hydrological group has a fixed budget to add automatic rain gauges in
mountain terrain.  This module answers: *which candidate construction
sites should be bought so that the most important river reaches have as
many independent rain-gauge sources as possible?*

Inputs
------
* existing stations: already installed, always available as sources;
  they never consume this purchase budget;
* candidate construction sites: each has a key, coordinates and a one-off
  purchase cost;
* priority samples: river-reach points with a key, coordinates and a
  positive importance weight.

Both CSV and JSON are accepted for every input; the field names are
documented in ``PLANNING_SPEC`` (also printed by
``scripts/planning/run_gauge_plan.py --fields``) and in the README.

Rules
-----
* Distance is the Haversine great-circle distance on a spherical earth
  (radius 6371.0 km).  A station serves a sample when the distance is
  ``<= radius_km``.
* A sample with exactly one serving source has *single coverage*; with
  at least two distinct serving stations it has *redundant coverage*.
  A chosen candidate is one source, never two, and cannot be chosen twice.
* Existing stations do not cost anything; a chosen new site does.
* Required candidates must be bought even if their combined cost exceeds
  the budget -- that is reported as a budget conflict, and no purchase
  recommendation is emitted.  Forbidden candidates may never be bought.  A
  candidate marked both required and forbidden is rejected.
* Optimisation compares, in this order:
    1. maximise the total weight of samples with >= 1 source;
    2. maximise the total weight of samples with >= 2 sources;
    3. minimise the new-station purchase cost;
    4. tie-break by the candidate-key ordering (stable result).
  This is a genuine four-layer lexicographic objective, not a weighted
  sum with a magic constant.  The optimiser is exact (see ``solve``).

Outputs
-------
``plan_summary.json``      objective values, budget, chosen keys, verdict
``purchase_recommendation.csv``  one row per candidate (chosen / not / why not)
``sample_coverage.csv``    per-sample explanation before/after, with the
                           distance and in-radius flag of every source
``unselected_improvements.csv``  for every not-chosen candidate, the samples
                           it would newly cover or upgrade
``plan_validation.json``   diagnostics of the input checks

Invalid data is reported with the exact record *before* solving; the
command exits non-zero and does not write a "successful-looking" empty
recommendation.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

EARTH_RADIUS_KM = 6371.0
LAT_MIN, LAT_MAX = -90.0, 90.0
LON_MIN, LON_MAX = -180.0, 180.0

STATION_FIELDS = ("station_id", "lat", "lon")
CANDIDATE_FIELDS = ("candidate_id", "lat", "lon", "cost", "required", "forbidden")
SAMPLE_FIELDS = ("sample_id", "lat", "lon", "weight", "reach")

_FIELD_DOCS = {
    "stations": {
        "file": "existing_stations",
        "required_columns": ["station_id", "lat", "lon"],
        "optional_columns": ["region", "weight"],
        "meaning": "stations already installed; sources come free of charge",
    },
    "candidates": {
        "file": "candidates",
        "required_columns": ["candidate_id", "lat", "lon", "cost"],
        "optional_columns": ["required", "forbidden", "name"],
        "meaning": "possible new construction sites; cost in currency units",
        "required_forbidden": "0/1 or true/false; both 1 on one row is rejected",
    },
    "samples": {
        "file": "samples",
        "required_columns": ["sample_id", "lat", "lon", "weight"],
        "optional_columns": ["reach"],
        "meaning": "priority river-reach sampling points; weight must be > 0",
    },
}

PLANNING_SPEC = {
    "fields": _FIELD_DOCS,
    "inputs": "CSV or JSON for each of the three tables; JSON is a list of objects "
              "or an object with a key equal to the table name",
    "distance": "Haversine, earth radius 6371.0 km; serving iff distance <= radius_km",
    "objectives": [
        "maximise total weight of samples with >= 1 source",
        "maximise total weight of samples with >= 2 distinct sources",
        "minimise purchase cost of chosen new sites",
        "tie-break by candidate key order (ascending key; at the first differing "
        "key the plan that includes it wins)",
    ],
    "budget": "sum of costs of chosen candidates must be <= budget; required "
              "candidates are part of the sum and a conflict is reported, never "
              "silently dropped; existing stations cost nothing",
    "outputs": {
        "plan_summary.json": "verdict, chosen keys, cost, four objective values",
        "purchase_recommendation.csv": "one row per candidate",
        "sample_coverage.csv": "per-sample before/after explanation and sources",
        "unselected_improvements.csv": "what each not-chosen candidate would add",
        "plan_validation.json": "input-validation diagnostics",
    },
}

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlanningError(Exception):
    """Input or configuration error, reported before any solving happens."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__(self.errors[0]["message"] if self.errors else "planning error")

    @property
    def budget_conflict(self):
        return any(e.get("kind") == "budget_conflict" for e in self.errors)


def _err(kind, message, **where):
    return {"kind": kind, "message": message, **where}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Station:
    key: str
    lat: float
    lon: float
    region: str = ""
    weight: float = 1.0
    source: str = ""


@dataclass
class Candidate:
    key: str
    lat: float
    lon: float
    cost: float
    required: bool = False
    forbidden: bool = False
    name: str = ""
    source: str = ""
    order: int = 0


@dataclass
class Sample:
    key: str
    lat: float
    lon: float
    weight: float
    reach: str = ""
    source: str = ""


@dataclass
class PlanInputs:
    stations: list
    candidates: list
    samples: list
    budget: float
    radius_km: float
    required: list
    forbidden: list
    source_files: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


# Amounts (budget / cost / sample weight) are handled as Decimal
# end-to-end so "budget exactly used up" and "one cent over budget" are
# exact comparisons, not the mercy of binary floating point.
# ``fmt_amount`` gives one deterministic textual form for CSV/JSON output.
def to_amount(value):
    """Convert a user-supplied budget/cost/weight to an exact Decimal."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        text = str(value).strip()
        if text == "":
            return None
        return Decimal(text)
    except (InvalidOperation, ValueError, AttributeError):
        return None


def fmt_amount(value):
    """Deterministic decimal text: no exponent, trailing zeros trimmed."""
    if value is None:
        return ""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


# ---------------------------------------------------------------------------
# Reading (CSV or JSON)
# ---------------------------------------------------------------------------


def _is_json(path):
    return path.lower().endswith(".json")


def _read_table(path, key_name):
    """Read a CSV or JSON table into a list of dicts.

    JSON may be a list of objects, or an object holding the list under
    ``key_name`` (e.g. ``{"candidates": [...]}``).  Blank CSV lines and
    blank lines are ignored.  ``_line`` records the physical record
    position so diagnostics can point at the exact record.
    """
    if not os.path.exists(path):
        raise PlanningError([_err("missing_input", f"input file not found: {path}",
                                 file=path)])
    if _is_json(path):
        def no_constants(x):
            raise ValueError(f"non-finite JSON number is not allowed: {x}")

        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f, parse_constant=no_constants)
        except (json.JSONDecodeError, ValueError) as exc:
            raise PlanningError([_err("bad_json", f"{path}: {exc}", file=path)])
        if isinstance(raw, dict):
            for cand in (key_name, key_name.rstrip("s"),
                         key_name[:-1] if key_name.endswith("s") else key_name):
                if isinstance(raw.get(cand), list):
                    raw = raw[cand]
                    break
        if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
            raise PlanningError([
                _err("bad_json", f"{path}: expected a list of objects (or an object "
                     f"with a '{key_name}' list)", file=path)])
        rows = []
        for i, r in enumerate(raw, start=1):
            rows.append({**r, "_line": i})
        return rows
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise PlanningError([_err("bad_csv", f"{path}: no header row", file=path)])
        for i, r in enumerate(reader, start=2):
            if all((v is None or str(v).strip() == "") for v in r.values()):
                continue
            rows.append({**r, "_line": i})
    return rows


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "y"}
_FALSE = {"", "0", "false", "no", "n"}


def _raw(row, field_name):
    value = row.get(field_name)
    return "" if value is None else str(value).strip()


def _where(table, path, row, key=""):
    return {"table": table, "file": path, "line": row.get("_line"), "key": key}


def _parse_number(value, table, path, row, field_name, errors, key,
                  *, positive=False, nonnegative=False):
    if value == "":
        errors.append(_err(
            "missing_field",
            f"{table} record {row.get('_line')} (key '{key}') is missing required "
            f"field '{field_name}'",
            **_where(table, path, row, key)))
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors.append(_err(
            "non_numeric",
            f"{table} record {row.get('_line')} (key '{key}') field '{field_name}' "
            f"is not a number: {value!r}",
            **_where(table, path, row, key)))
        return None
    if not math.isfinite(number):
        errors.append(_err(
            "non_finite",
            f"{table} record {row.get('_line')} (key '{key}') field '{field_name}' "
            f"is not finite: {value!r}",
            **_where(table, path, row, key)))
        return None
    if positive and number <= 0.0:
        errors.append(_err(
            "out_of_range",
            f"{table} record {row.get('_line')} (key '{key}') field '{field_name}' "
            f"must be > 0, got {number!r}",
            **_where(table, path, row, key)))
        return None
    if nonnegative and number < 0.0:
        errors.append(_err(
            "out_of_range",
            f"{table} record {row.get('_line')} (key '{key}') field '{field_name}' "
            f"must be >= 0, got {number!r}",
            **_where(table, path, row, key)))
        return None
    return number


def _parse_coords(row, table, path, errors, key):
    lat = lon = None
    for name, lo, hi in (("lat", LAT_MIN, LAT_MAX), ("lon", LON_MIN, LON_MAX)):
        value = _parse_number(_raw(row, name), table, path, row, name, errors, key)
        if value is not None and not (lo <= value <= hi):
            errors.append(_err(
                "out_of_range",
                f"{table} record {row.get('_line')} (key '{key}') field '{name}' "
                f"is outside [{lo}, {hi}]: {value!r}",
                **_where(table, path, row, key)))
            value = None
        if name == "lat":
            lat = value
        else:
            lon = value
    return lat, lon


def _parse_amounts(value, table, path, row, field_name, errors, key):
    """Parse a money/weight amount exactly (no binary rounding)."""
    if value == "":
        errors.append(_err(
            "missing_field",
            f"{table} record {row.get('_line')} (key '{key}') is missing required "
            f"field '{field_name}'",
            **_where(table, path, row, key)))
        return None
    amount = to_amount(value)
    if amount is None:
        errors.append(_err(
            "non_numeric",
            f"{table} record {row.get('_line')} (key '{key}') field '{field_name}' "
            f"is not a valid decimal number: {value!r}",
            **_where(table, path, row, key)))
        return None
    if not amount.is_finite():
        errors.append(_err(
            "non_finite",
            f"{table} record {row.get('_line')} (key '{key}') field '{field_name}' "
            f"is not finite: {value!r}",
            **_where(table, path, row, key)))
        return None
    if amount < 0:
        errors.append(_err(
            "out_of_range",
            f"{table} record {row.get('_line')} (key '{key}') field '{field_name}' "
            f"must be >= 0, got {value!r}",
            **_where(table, path, row, key)))
        return None
    if field_name == "weight" and amount <= 0:
        errors.append(_err(
            "out_of_range",
            f"{table} record {row.get('_line')} (key '{key}') field 'weight' "
            f"must be > 0, got {value!r}",
            **_where(table, path, row, key)))
        return None
    return amount


def _flag(row, table, path, field_name, errors, key):
    value = _raw(row, field_name).lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    errors.append(_err(
        "bad_flag",
        f"{table} record {row.get('_line')} (key '{key}') field '{field_name}' "
        f"must be 0/1 or true/false, got {_raw(row, field_name)!r}",
        **_where(table, path, row, key)))
    return False


def _key_of(row, table, path, errors):
    key = _raw(row, "candidate_id" if table == "candidates"
               else "sample_id" if table == "samples" else "station_id")
    if key == "":
        errors.append(_err(
            "missing_key",
            f"{table} record {row.get('_line')} has an empty key",
            **_where(table, path, row, "")))
    return key


def _parse_stations(rows, path, errors):
    out = []
    for row in rows:
        key = _key_of(row, "stations", path, errors)
        lat, lon = _parse_coords(row, "stations", path, errors, key)
        region = _raw(row, "region")
        weight = 1.0
        if _raw(row, "weight") != "":
            weight = _parse_number(_raw(row, "weight"), "stations", path, row,
                                   "weight", errors, key)
        if key and lat is not None and lon is not None:
            out.append(Station(key=key, lat=lat, lon=lon, region=region,
                               weight=weight if weight is not None else 1.0,
                               source=path))
    return out


def _parse_candidates(rows, path, errors):
    out = []
    for row in rows:
        key = _key_of(row, "candidates", path, errors)
        lat, lon = _parse_coords(row, "candidates", path, errors, key)
        cost = _parse_amounts(_raw(row, "cost"), "candidates", path, row,
                              "cost", errors, key)
        required = _flag(row, "candidates", path, "required", errors, key)
        forbidden = _flag(row, "candidates", path, "forbidden", errors, key)
        if key and lat is not None and lon is not None and cost is not None:
            out.append(Candidate(key=key, lat=lat, lon=lon, cost=cost,
                                 required=required, forbidden=forbidden,
                                 name=_raw(row, "name"), source=path,
                                 order=row.get("_line", 0)))
    return out


def _parse_samples(rows, path, errors):
    out = []
    for row in rows:
        key = _key_of(row, "samples", path, errors)
        lat, lon = _parse_coords(row, "samples", path, errors, key)
        weight = _parse_amounts(_raw(row, "weight"), "samples", path, row,
                                "weight", errors, key)
        if weight is not None and weight <= 0:
            errors.append(_err(
                "out_of_range",
                f"samples record {row.get('_line')} (key '{key}') field 'weight' "
                f"must be > 0, got {_raw(row, 'weight')!r}",
                **_where("samples", path, row, key)))
            weight = None
        if key and lat is not None and lon is not None and weight is not None:
            out.append(Sample(key=key, lat=lat, lon=lon, weight=weight,
                              reach=_raw(row, "reach"), source=path))
    return out


def _check_unique(items, table, path, errors):
    seen = {}
    for item in items:
        if item.key in seen:
            errors.append(_err(
                "duplicate_key",
                f"duplicate {table} key '{item.key}' (also in {seen[item.key]})",
                table=table, file=path, key=item.key))
        else:
            seen[item.key] = item.source
    return seen


def validate_options(budget, radius_km, required, forbidden, errors=None):
    """Validate the scalar options and the required/forbidden key lists.

    ``budget`` is returned as an exact Decimal; ``radius_km`` as float.
    Problems are appended to ``errors`` (a list owned by the caller), so a
    loader can report them together with record-level problems.
    """
    if errors is None:
        errors = []
    budget_v = _coerce_budget(budget, errors)
    radius_v = _coerce_option(radius_km, "radius_km", errors, positive=True)
    for key in list(required) + list(forbidden):
        if not isinstance(key, str) or key.strip() == "":
            errors.append(_err(
                "bad_option",
                f"required/forbidden key must be a non-empty string, got {key!r}",
                table="options", key=str(key)))
    return (budget_v, radius_v,
            [k.strip() for k in required], [k.strip() for k in forbidden])


def _coerce_budget(value, errors):
    """Budget as exact Decimal; negative or non-finite values are rejected."""
    if value is None:
        errors.append(_err("missing_option", "option 'budget' is required"))
        return None
    amount = to_amount(value)
    if amount is None:
        errors.append(_err("bad_option", f"option 'budget' is not a number: {value!r}"))
        return None
    if not amount.is_finite():
        errors.append(_err("non_finite", f"option 'budget' is not finite: {value!r}"))
        return None
    if amount < 0:
        errors.append(_err("out_of_range",
                           f"option 'budget' must be >= 0, got {value!r}"))
        return None
    return amount


def _coerce_option(value, name, errors, *, positive=False, nonnegative=False):
    if value is None:
        errors.append(_err("missing_option", f"option '{name}' is required"))
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors.append(_err("bad_option", f"option '{name}' is not a number: {value!r}"))
        return None
    if not math.isfinite(number):
        errors.append(_err("non_finite", f"option '{name}' is not finite: {value!r}"))
        return None
    if positive and number <= 0.0:
        errors.append(_err("out_of_range", f"option '{name}' must be > 0, got {number!r}"))
        return None
    if nonnegative and number < 0.0:
        errors.append(_err("out_of_range", f"option '{name}' must be >= 0, got {number!r}"))
        return None
    return number


# ---------------------------------------------------------------------------
# Loading: read all three tables, validate everything before solving
# ---------------------------------------------------------------------------


def load_plan_inputs(samples_path, candidates_path, stations_path,
                     budget, radius_km, required=None, forbidden=None):
    """Load and validate all planning inputs.

    Raises :class:`PlanningError` listing every invalid record it found
    (not just the first), so the operator can fix the data in one pass.
    """
    errors = []
    sample_rows = _read_table(samples_path, "samples")
    candidate_rows = _read_table(candidates_path, "candidates")
    station_rows = _read_table(stations_path, "stations") if stations_path else []

    stations = _parse_stations(station_rows, stations_path, errors)
    candidates = _parse_candidates(candidate_rows, candidates_path, errors)
    samples = _parse_samples(sample_rows, samples_path, errors)

    _check_unique(stations, "stations", stations_path, errors)
    candidate_keys = _check_unique(candidates, "candidates", candidates_path, errors)
    sample_keys = _check_unique(samples, "samples", samples_path, errors)

    # A new site and an existing station must not share a key: otherwise
    # "distinct source" would be ambiguous and the existing station would
    # be counted as a free source of the candidate.
    station_keys = {s.key for s in stations}
    for c in candidates:
        if c.key in station_keys:
            errors.append(_err(
                "key_collision",
                f"candidate '{c.key}' reuses an existing station key",
                table="candidates", file=candidates_path, key=c.key))
    # Candidate sites must be distinct places; two rows at exactly
    # identical coordinates would be the same construction site.
    seen_coords = {}
    for c in candidates:
        coord = (c.lat, c.lon)
        if coord in seen_coords:
            errors.append(_err(
                "duplicate_coordinates",
                f"candidate '{c.key}' has the same coordinates as candidate "
                f"'{seen_coords[coord]}': ({c.lat}, {c.lon})",
                table="candidates", file=candidates_path, key=c.key))
        else:
            seen_coords[coord] = c.key
    if not samples:
        errors.append(_err("empty_input", f"no valid sample points in {samples_path}",
                           table="samples", file=samples_path))
    if not candidates:
        errors.append(_err("empty_input", f"no valid candidate sites in {candidates_path}",
                           table="candidates", file=candidates_path))

    budget_v, radius_v, req_opts, forb_opts = validate_options(
        budget, radius_km, required or [], forbidden or [], errors)

    # Required/forbidden: union of the command-line options and the optional
    # in-file flags.  A key in both sets is rejected (never guessed around).
    required_set = set(req_opts) | {c.key for c in candidates if c.required}
    forbidden_set = set(forb_opts) | {c.key for c in candidates if c.forbidden}
    # A key that appears in the candidate table counts as "known" even if
    # its row had another problem (that row is already reported above); this
    # avoids a misleading second "unknown candidate" error for the same row.
    all_known = set(candidate_keys) | {
        _raw(row, "candidate_id") for row in candidate_rows
        if _raw(row, "candidate_id")}
    for key in sorted(required_set - all_known):
        errors.append(_err(
            "unknown_required_candidate",
            f"required candidate '{key}' is not in the candidate table",
            table="required", file=candidates_path, key=key))
    for key in sorted(forbidden_set - all_known):
        errors.append(_err(
            "unknown_forbidden_candidate",
            f"forbidden candidate '{key}' is not in the candidate table",
            table="forbidden", file=candidates_path, key=key))
    for key in sorted(required_set & forbidden_set):
        errors.append(_err(
            "required_forbidden_conflict",
            f"candidate '{key}' is both required and forbidden",
            table="candidates", file=candidates_path, key=key))

    inputs = PlanInputs(
        stations=stations,
        candidates=candidates,
        samples=samples,
        budget=budget_v,
        radius_km=radius_v,
        required=sorted(required_set),
        forbidden=sorted(forbidden_set),
        source_files={
            "samples": samples_path,
            "candidates": candidates_path,
            "existing_stations": stations_path or "",
        },
    )

    # A required set that cannot fit the budget is a hard conflict: report
    # it explicitly and never emit a purchase recommendation that silently
    # drops a required site.
    if budget_v is not None and not errors:
        required_cost = sum((c.cost for c in candidates if c.key in required_set),
                            start=Decimal(0))
        if required_cost > budget_v:
            errors.append(_err(
                "budget_conflict",
                f"required candidates cost {required_cost} > budget {budget_v}; "
                f"required sites are not silently dropped",
                required_cost=fmt_amount(required_cost),
                budget=fmt_amount(budget_v),
                required_keys=sorted(required_set)))

    if errors:
        raise PlanningError(errors)
    return inputs


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km on a sphere of radius 6371.0 km."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2.0) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2.0) ** 2)
    # Clamp against round-off outside [0, 1] before asin.
    a = min(1.0, max(0.0, a))
    return 2.0 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Model: coverage masks and distances
# ---------------------------------------------------------------------------


@dataclass
class CoverageModel:
    samples: list                  # sorted by key
    candidates: list               # sorted by key
    stations: list                 # sorted by key
    sample_index: dict             # sample key -> position
    candidate_by_key: dict
    existing_counts: list          # number of existing stations within radius per sample
    candidate_masks: dict          # candidate key -> bit mask of samples it serves
    station_distances: dict        # station key -> list[float] per sample
    candidate_distances: dict      # candidate key -> list[float] per sample
    weight_sums: dict              # mask -> Decimal weight sum (cached)
    sample_weights: list           # Decimal per sample


def build_model(inputs):
    """Sort inputs by key (input order must not change the result) and
    precompute distances and coverage bit masks."""
    samples = sorted(inputs.samples, key=lambda s: s.key)
    candidates = sorted(inputs.candidates, key=lambda c: c.key)
    stations = sorted(inputs.stations, key=lambda s: s.key)
    radius = inputs.radius_km
    n = len(samples)

    station_distances = {}
    for st in stations:
        station_distances[st.key] = [
            haversine_km(st.lat, st.lon, s.lat, s.lon) for s in samples]
    candidate_distances = {}
    candidate_masks = {}
    existing_counts = [0] * n
    for st in stations:
        for i, d in enumerate(station_distances[st.key]):
            if d <= radius:
                existing_counts[i] += 1
    for c in candidates:
        dists = [haversine_km(c.lat, c.lon, s.lat, s.lon) for s in samples]
        candidate_distances[c.key] = dists
        mask = 0
        for i, d in enumerate(dists):
            if d <= radius:
                mask |= 1 << i
        candidate_masks[c.key] = mask
    return CoverageModel(
        samples=samples,
        candidates=candidates,
        stations=stations,
        sample_index={s.key: i for i, s in enumerate(samples)},
        candidate_by_key={c.key: c for c in candidates},
        existing_counts=existing_counts,
        candidate_masks=candidate_masks,
        station_distances=station_distances,
        candidate_distances=candidate_distances,
        weight_sums={},
        sample_weights=[s.weight for s in samples],
    )


def mask_weight(model, mask):
    """Exact total weight of the samples selected by a bit mask."""
    if mask in model.weight_sums:
        return model.weight_sums[mask]
    total = Decimal(0)
    remaining = mask
    while remaining:
        low = remaining & -remaining
        i = low.bit_length() - 1
        total += model.sample_weights[i]
        remaining ^= low
    model.weight_sums[mask] = total
    return total


def status_for(count):
    if count <= 0:
        return "uncovered"
    if count == 1:
        return "single"
    return "redundant"


def _with_candidate(once, twice, cmask):
    """Add one candidate's coverage to a (once, twice) signature."""
    return once | cmask, twice | (once & cmask)


def _mask_counts(once, twice, n):
    """Per-sample source counts capped at 2 from a signature."""
    counts = []
    for i in range(n):
        bit = 1 << i
        counts.append(2 if twice & bit else (1 if once & bit else 0))
    return counts


# ---------------------------------------------------------------------------
# Exact solver
# ---------------------------------------------------------------------------


@dataclass
class PlanState:
    once: int
    twice: int
    cost: Decimal
    bits: int          # tie-break bit vector over optional candidates


@dataclass
class PlanResult:
    inputs: PlanInputs
    model: CoverageModel
    chosen: list                # chosen candidate keys, sorted
    chosen_required: list
    chosen_optional: list
    total_cost: Decimal
    required_cost: Decimal
    optional_cost: Decimal
    remaining_budget: Decimal
    before_counts: list
    after_counts: list
    layer1_before: Decimal     # weight with >= 1 source
    layer2_before: Decimal     # weight with >= 2 sources
    layer1_after: Decimal
    layer2_after: Decimal
    states_explored: int
    subsets_equivalent: int    # = 2 ** n_optional (search space size)
    dp_states_final: int
    n_optional: int
    component_states: list = field(default_factory=list)
    component_sizes: list = field(default_factory=list)


def _baseline(model, inputs):
    """Coverage signature contributed by existing stations plus required
    candidates (which are always bought)."""
    once = twice = 0
    for i, count in enumerate(model.existing_counts):
        if count >= 1:
            once |= 1 << i
        if count >= 2:
            twice |= 1 << i
    for key in inputs.required:
        cmask = model.candidate_masks[key]
        once, twice = _with_candidate(once, twice, cmask)
    return once, twice


def _coverage_bits(model, counts):
    once = twice = 0
    for i, count in enumerate(counts):
        if count >= 1:
            once |= 1 << i
        if count >= 2:
            twice |= 1 << i
    return once, twice


def _optional_components(model, optional):
    """Split optional candidates into coverage-independent components.

    Two candidates are connected when their serve-masks share at least one
    sample.  Components therefore serve disjoint sample sets, so their
    coverage contributions combine by disjoint union and the four-layer
    objective decomposes additively.  Components are ordered by their
    smallest candidate key, matching the stable tie-break order.
    """
    parent = {c.key: c.key for c in optional}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    mask_owner = {}
    for cand in optional:
        cmask = model.candidate_masks[cand.key]
        while cmask:
            low = cmask & -cmask
            i = low.bit_length() - 1
            if i in mask_owner:
                union(cand.key, mask_owner[i])
            else:
                mask_owner[i] = cand.key
            cmask ^= low
    groups = {}
    for cand in optional:
        groups.setdefault(find(cand.key), []).append(cand)
    components = [sorted(members, key=lambda c: c.key)
                  for members in groups.values()]
    components.sort(key=lambda members: members[0].key)
    return components


def _component_plans(model, members, base_once, base_twice, budget, sig_by_key):
    """Exact Pareto plans for one component, cost-indexed.

    The component is enumerated over its 2^m subsets via a signature DP
    keyed by (served-once mask, served-twice mask); per signature only the
    cheapest plan survives (ties: larger key-bit vector).  The surviving
    plans are then reduced per cost to the lexicographically best
    (newly-covered weight, newly-redundant weight, key bits), which is
    safe because the objective across components is additive and only the
    remaining budget (i.e. the chosen cost) matters.

    Returns a dict ``cost -> (covered_weight, redundant_weight, bits, keys)``.
    """
    states = {(0, 0): PlanState(0, 0, Decimal(0), 0)}
    for cand in members:
        cmask = model.candidate_masks[cand.key]
        sig = sig_by_key[cand.key]
        nxt = dict(states)
        for key, st in states.items():
            cost = st.cost + cand.cost
            if cost > budget:
                continue
            once, twice = key
            new_once, new_twice = _with_candidate(once, twice, cmask)
            new_state = PlanState(new_once, new_twice, cost, st.bits | sig)
            cur = nxt.get((new_once, new_twice))
            if cur is None or _state_better(new_state, cur):
                nxt[(new_once, new_twice)] = new_state
        states = nxt

    by_cost = {}
    for (once, twice), st in states.items():
        keys = _mask_keys(st.bits, sig_by_key, members)
        covered = mask_weight(model, once & ~base_once)
        redundant = mask_weight(model, (twice | (once & base_once)) & ~base_twice)
        entry = (covered, redundant, st.bits, keys)
        cur = by_cost.get(st.cost)
        if cur is None or _plan_entry_better(entry, cur):
            by_cost[st.cost] = entry
    return by_cost


def _mask_keys(bits, sig_by_key, members):
    return [c.key for c in members if bits & sig_by_key[c.key]]


def _plan_entry_better(a, b):
    """Higher covered weight, then higher redundant weight, then larger
    key-bit vector (same cost by construction)."""
    if a[0] != b[0]:
        return a[0] > b[0]
    if a[1] != b[1]:
        return a[1] > b[1]
    return a[2] > b[2]


def solve(inputs):
    """Exact four-layer lexicographic optimisation.

    Optional candidates are split into coverage-independent components
    (components serve disjoint sample sets).  Each component is solved
    exactly and reduced to its best plan per purchase cost.  Components
    are then combined exactly by a knapsack over total cost: for each
    total cost only the lexicographically best (covered weight, redundant
    weight, key bits) survives, which dominates every other plan of that
    cost because later contributions are additive and depend only on the
    remaining budget.  The final state is chosen by the four-layer
    objective.  This is exact, not a heuristic or a weighted sum.
    """
    model = build_model(inputs)
    n_samples = len(model.samples)

    required = set(inputs.required)
    forbidden = set(inputs.forbidden)
    required_cost = sum((c.cost for c in model.candidates if c.key in required),
                        start=Decimal(0))
    optional = [c for c in model.candidates if c.key not in required
                and c.key not in forbidden]
    optional_budget = inputs.budget - required_cost

    base_once, base_twice = _baseline(model, inputs)
    before_counts = list(model.existing_counts)
    before_once, before_twice = _coverage_bits(model, before_counts)

    n_optional = len(optional)
    sorted_optional_keys = sorted(c.key for c in optional)
    sig_by_key = {key: 1 << (n_optional - 1 - i)
                  for i, key in enumerate(sorted_optional_keys)}

    components = _optional_components(model, optional)
    plans_by_component = [
        _component_plans(model, members, base_once, base_twice,
                         optional_budget, sig_by_key)
        for members in components
    ]
    component_states = [len(plans) for plans in plans_by_component]
    component_sizes = [len(members) for members in components]

    # Knapsack combine over total optional cost.  State = (cost, covered,
    # redundant, bits, keys); for each cost keep the lexicographically
    # best.  Coverage weights are additive across components.
    states = {Decimal(0): (Decimal(0), Decimal(0), 0, ())}
    for plans in plans_by_component:
        nxt = {}
        for total_cost, (covered, redundant, bits, keys) in states.items():
            for cost, (c_cov, c_red, c_bits, c_keys) in plans.items():
                new_cost = total_cost + cost
                if new_cost > optional_budget:
                    continue
                cand = (covered + c_cov, redundant + c_red, bits | c_bits,
                        keys + tuple(c_keys))
                cur = nxt.get(new_cost)
                if cur is None or _global_entry_better(cand, cur):
                    nxt[new_cost] = cand
        states = nxt

    best = None
    for cost, (covered, redundant, bits, keys) in states.items():
        entry = (cost, covered, redundant, bits, keys)
        if best is None or _final_better(entry, best):
            best = entry
    optional_cost, _, _, _, chosen_optional = best

    after_once = base_once
    after_twice = base_twice
    for key in chosen_optional:
        cmask = model.candidate_masks[key]
        after_once, after_twice = _with_candidate(after_once, after_twice, cmask)

    chosen_required = sorted(required)
    chosen = sorted(chosen_required + list(chosen_optional))
    total_cost = required_cost + optional_cost

    return PlanResult(
        inputs=inputs,
        model=model,
        chosen=chosen,
        chosen_required=chosen_required,
        chosen_optional=sorted(chosen_optional),
        total_cost=total_cost,
        required_cost=required_cost,
        optional_cost=optional_cost,
        remaining_budget=inputs.budget - total_cost,
        before_counts=before_counts,
        after_counts=_mask_counts(after_once, after_twice, n_samples),
        layer1_before=mask_weight(model, before_once),
        layer2_before=mask_weight(model, before_twice),
        layer1_after=mask_weight(model, after_once),
        layer2_after=mask_weight(model, after_twice),
        states_explored=len(states),
        subsets_equivalent=2 ** n_optional,
        dp_states_final=len(states),
        n_optional=n_optional,
        component_states=component_states,
        component_sizes=component_sizes,
    )


def _global_entry_better(a, b):
    """Same total cost: higher covered weight, then higher redundant
    weight, then larger key-bit vector."""
    if a[0] != b[0]:
        return a[0] > b[0]
    if a[1] != b[1]:
        return a[1] > b[1]
    return a[2] > b[2]


def _final_better(a, b):
    """Four-layer comparison over (cost, covered, redundant, bits, keys)."""
    cost_a, cov_a, red_a, bits_a = a[0], a[1], a[2], a[3]
    cost_b, cov_b, red_b, bits_b = b[0], b[1], b[2], b[3]
    if cov_a != cov_b:
        return cov_a > cov_b
    if red_a != red_b:
        return red_a > red_b
    if cost_a != cost_b:
        return cost_a < cost_b
    return bits_a > bits_b


def _state_better(new, current):
    # Lower cost wins; at equal cost the larger bit vector wins.
    if new.cost != current.cost:
        return new.cost < current.cost
    return new.bits > current.bits


def brute_force(inputs):
    """Independent exhaustive enumeration of every feasible subset.

    Used by the tests to verify the dynamic program's four-layer objective
    and stable tie-break.  Enumerates ``2 ** len(candidates)`` masks and
    keeps the best feasible one; never used by the production path.

    Tie-break rule (identical to the production one): among plans tied on
    both coverage layers and on cost, look at the candidates in ascending
    key order and prefer the plan that includes the first key where the
    two plans differ.
    """
    model = build_model(inputs)
    required = set(inputs.required)
    forbidden = set(inputs.forbidden)
    candidates = model.candidates
    n = len(candidates)
    required_mask = 0
    for i, cand in enumerate(candidates):
        if cand.key in required:
            required_mask |= 1 << i
    best = None
    for mask in range(1 << n):
        if mask & required_mask != required_mask:
            continue
        if any(mask & (1 << i) for i, cand in enumerate(candidates)
               if cand.key in forbidden):
            continue
        cost = sum((candidates[i].cost for i in range(n) if mask & (1 << i)),
                   start=Decimal(0))
        if cost > inputs.budget:
            continue
        once = twice = 0
        for i, count in enumerate(model.existing_counts):
            if count >= 1:
                once |= 1 << i
            if count >= 2:
                twice |= 1 << i
        for i in range(n):
            if mask & (1 << i):
                once, twice = _with_candidate(once, twice,
                                              model.candidate_masks[candidates[i].key])
        chosen = [candidates[i].key for i in range(n) if mask & (1 << i)]
        score = (mask_weight(model, once), mask_weight(model, twice), cost)
        entry = (score, chosen)
        if best is None or _brute_better(entry, best):
            best = entry
    return best


def _brute_better(a, b):
    sa, ca = a
    sb, cb = b
    if sa[0] != sb[0]:
        return sa[0] > sb[0]
    if sa[1] != sb[1]:
        return sa[1] > sb[1]
    if sa[2] != sb[2]:
        return sa[2] < sb[2]
    # Same coverage, same cost: walk keys in ascending order; the first
    # key in one plan but not the other wins for the plan that has it.
    for x in sorted(set(ca) | set(cb)):
        if (x in ca) != (x in cb):
            return x in ca
    return False


# ---------------------------------------------------------------------------
# Reporting: per-sample explanation, purchase recommendation
# ---------------------------------------------------------------------------


def _sources_for_sample(result, i, include_new):
    """Distances of every existing station (and, if requested, every
    chosen new site) to sample ``i``, with the radius decision."""
    model = result.model
    radius = result.inputs.radius_km
    sources = []
    for st in model.stations:
        d = model.station_distances[st.key][i]
        sources.append({
            "source_id": st.key,
            "kind": "existing",
            "distance_km": round(d, 6),
            "in_radius": d <= radius,
            "counted": d <= radius,
        })
    if include_new:
        chosen = set(result.chosen)
        for cand in model.candidates:
            if cand.key not in chosen:
                continue
            d = model.candidate_distances[cand.key][i]
            sources.append({
                "source_id": cand.key,
                "kind": "new",
                "distance_km": round(d, 6),
                "in_radius": d <= radius,
                "counted": d <= radius,
            })
    # deterministic order: counted sources first, then distance, then key
    sources.sort(key=lambda s: (not s["in_radius"], s["distance_km"], s["source_id"]))
    return sources


def _nearest_outside(sources):
    outside = [s for s in sources if not s["in_radius"]]
    if not outside:
        return None
    return min(outside, key=lambda s: (s["distance_km"], s["source_id"]))


def _uncovered_reason(sources, radius_km):
    """Human-readable explanation why a sample has no source."""
    outside = [s for s in sources if not s["in_radius"]]
    if not outside:
        return (f"no source in the input tables (radius {fmt_amount(Decimal(str(radius_km)))} km)")
    near = _nearest_outside(sources)
    return (f"nearest source {near['source_id']} is {_fmt_distance(near['distance_km'])} km "
            f"away (> radius {fmt_amount(Decimal(str(radius_km)))} km)")


def _fmt_distance(km):
    return f"{km:.6f}"


def _sample_rows(result):
    model = result.model
    radius = result.inputs.radius_km
    rows = []
    for i, sample in enumerate(model.samples):
        before_sources = _sources_for_sample(result, i, include_new=False)
        after_sources = _sources_for_sample(result, i, include_new=True)
        before = result.before_counts[i]
        after = result.after_counts[i]
        if before == 0 and after >= 1:
            gain = "newly_covered"
        elif before <= 1 and after >= 2 and before >= 1:
            gain = "newly_redundant"
        elif before == 0 and after == 0:
            gain = "still_uncovered"
        else:
            gain = "none"
        if after == 0:
            reason = _uncovered_reason(after_sources, radius)
        elif after == 1:
            near = _nearest_outside(after_sources)
            reason = "single source only"
            if near is not None:
                reason += (f"; next-nearest source {near['source_id']} is "
                           f"{_fmt_distance(near['distance_km'])} km away")
        else:
            reason = ""
        rows.append({
            "sample_id": sample.key,
            "reach": sample.reach,
            "lat": f"{sample.lat:.10g}",
            "lon": f"{sample.lon:.10g}",
            "weight": fmt_amount(sample.weight),
            "status_before": status_for(before),
            "status_after": status_for(after),
            "sources_before": before,
            "sources_after": after,
            "sources_before_json": json.dumps(before_sources, sort_keys=True,
                                              separators=(",", ":")),
            "sources_after_json": json.dumps(after_sources, sort_keys=True,
                                             separators=(",", ":")),
            "coverage_gap_reason": reason,
            "gain_type": gain,
        })
    return rows


def _served_samples(model, mask):
    return [model.samples[i] for i in range(len(model.samples)) if mask & (1 << i)]


def _marginal(result, cand):
    """Samples that adding ``cand`` to the plan *without it* would newly
    cover or upgrade to redundant, and the corresponding weight.

    For a chosen candidate this is its contribution inside the
    recommendation (computed against the chosen plan minus itself); for a
    not-chosen candidate it is what buying it would add on top of the
    recommendation.
    """
    model = result.model
    selected = set(result.chosen)
    also_selected = selected - {cand.key}
    base_counts = list(result.before_counts)
    for key in selected:
        if key == cand.key:
            continue
        for i in range(len(model.samples)):
            if model.candidate_masks[key] & (1 << i):
                base_counts[i] += 1
    cmask = model.candidate_masks[cand.key]
    newly_covered, newly_redundant = [], []
    for i in range(len(model.samples)):
        if not (cmask & (1 << i)):
            continue
        if base_counts[i] == 0:
            newly_covered.append(i)
        elif base_counts[i] == 1:
            newly_redundant.append(i)
    weight_covered = sum((model.samples[i].weight for i in newly_covered),
                         start=Decimal(0))
    weight_redundant = sum((model.samples[i].weight for i in newly_redundant),
                           start=Decimal(0))
    return newly_covered, newly_redundant, weight_covered, weight_redundant


def _candidate_decision(result, cand):
    if cand.key in result.chosen_required:
        return "chosen_required"
    if cand.key in result.chosen_optional:
        return "chosen_optional"
    if cand.key in result.inputs.forbidden:
        return "forbidden"
    return "not_chosen"


def _marginal_verdict(result, cand, newly_covered, newly_redundant):
    """Explain why a not-chosen candidate was not bought."""
    model = result.model
    if cand.key in result.inputs.forbidden:
        return "not chosen: marked forbidden"
    if not newly_covered and not newly_redundant:
        return "not chosen: adds no sample to the recommended plan"
    if cand.cost > result.remaining_budget:
        return (f"not chosen: cost {fmt_amount(cand.cost)} exceeds the remaining "
                f"budget {fmt_amount(result.remaining_budget)}")
    # Affordable and useful, yet not selected: replacing a chosen site would
    # lower an earlier objective layer (or cost more at equal coverage).
    covered = sum((model.samples[i].weight for i in newly_covered), start=Decimal(0))
    redundant = sum((model.samples[i].weight for i in newly_redundant),
                    start=Decimal(0))
    return ("not chosen: affordable, but swapping it for a chosen site would reduce "
            "an earlier objective layer (marginal "
            f"+{fmt_amount(covered)} covered / +{fmt_amount(redundant)} redundant "
            "weight against the current plan)")


def _candidate_rows(result):
    model = result.model
    rows = []
    for cand in model.candidates:
        decision = _candidate_decision(result, cand)
        served = _served_samples(model, model.candidate_masks[cand.key])
        newly_covered, newly_redundant, w_cov, w_red = _marginal(result, cand)
        chosen = decision in ("chosen_required", "chosen_optional")
        if chosen:
            label = ("chosen: required site" if decision == "chosen_required"
                     else "chosen: optimiser selected this site")
            reason = (f"{label}; contribution in this plan "
                      f"+{fmt_amount(w_cov)} covered / +{fmt_amount(w_red)} "
                      "redundant weight")
        else:
            reason = _marginal_verdict(result, cand, newly_covered, newly_redundant)
        effect = "contribution_" if chosen else "newly_"
        improvements = (
            [f"{model.samples[i].key}:{effect}covered" for i in newly_covered]
            + [f"{model.samples[i].key}:{effect}redundant"
               for i in newly_redundant])
        rows.append({
            "candidate_id": cand.key,
            "name": cand.name,
            "lat": f"{cand.lat:.10g}",
            "lon": f"{cand.lon:.10g}",
            "cost": fmt_amount(cand.cost),
            "decision": decision,
            "reason": reason,
            "serves_samples": " | ".join(s.key for s in served),
            "n_serves_samples": len(served),
            ("improves_if_added" if not chosen else "contribution_samples"):
                " | ".join(sorted(improvements)),
            "newly_covered_weight": fmt_amount(w_cov),
            "newly_redundant_weight": fmt_amount(w_red),
            "fits_remaining_budget": (
                "" if chosen
                else ("yes" if cand.cost <= result.remaining_budget else "no")),
            "selected": "yes" if cand.key in set(result.chosen) else "no",
        })
    return rows


def _improvement_rows(result):
    model = result.model
    rows = []
    for cand in model.candidates:
        if cand.key in set(result.chosen):
            continue
        newly_covered, newly_redundant, _, _ = _marginal(result, cand)
        for i in newly_covered:
            rows.append({
                "candidate_id": cand.key,
                "sample_id": model.samples[i].key,
                "effect": "newly_covered",
                "sample_weight": fmt_amount(model.samples[i].weight),
                "candidate_cost": fmt_amount(cand.cost),
                "fits_remaining_budget": ("yes" if cand.cost <= result.remaining_budget
                                          else "no"),
            })
        for i in newly_redundant:
            rows.append({
                "candidate_id": cand.key,
                "sample_id": model.samples[i].key,
                "effect": "newly_redundant",
                "sample_weight": fmt_amount(model.samples[i].weight),
                "candidate_cost": fmt_amount(cand.cost),
                "fits_remaining_budget": ("yes" if cand.cost <= result.remaining_budget
                                          else "no"),
            })
    return sorted(rows, key=lambda r: (r["candidate_id"], r["sample_id"], r["effect"]))


def _status_counts(counts):
    return {"uncovered": sum(1 for c in counts if c == 0),
            "single": sum(1 for c in counts if c == 1),
            "redundant": sum(1 for c in counts if c >= 2)}


def summary_payload(result, output_dir=None):
    model = result.model
    unselected = []
    for cand in model.candidates:
        if cand.key in set(result.chosen):
            continue
        newly_covered, newly_redundant, w_cov, w_red = _marginal(result, cand)
        unselected.append({
            "candidate_id": cand.key,
            "decision": _candidate_decision(result, cand),
            "reason": _marginal_verdict(result, cand, newly_covered, newly_redundant),
            "cost": fmt_amount(cand.cost),
            "fits_remaining_budget": cand.cost <= result.remaining_budget,
            "newly_covered": [model.samples[i].key for i in newly_covered],
            "newly_redundant": [model.samples[i].key for i in newly_redundant],
            "newly_covered_weight": fmt_amount(w_cov),
            "newly_redundant_weight": fmt_amount(w_red),
        })
    total_weight = sum(model.sample_weights, start=Decimal(0))
    return {
        "verdict": "ok",
        "parameters": {
            "budget": fmt_amount(result.inputs.budget),
            "radius_km": fmt_amount(result.inputs.radius_km),
            "required_candidates": result.chosen_required,
            "forbidden_candidates": sorted(result.inputs.forbidden),
        },
        "purchase": {
            "chosen_candidates": result.chosen,
            "n_chosen": len(result.chosen),
            "cost_total": fmt_amount(result.total_cost),
            "cost_required": fmt_amount(result.required_cost),
            "cost_optional": fmt_amount(result.optional_cost),
            "budget_remaining": fmt_amount(result.remaining_budget),
            "existing_stations_are_free": True,
        },
        "coverage_summary": {
            "total_sample_weight": fmt_amount(total_weight),
            "weight_with_at_least_one_source": {
                "before": fmt_amount(result.layer1_before),
                "after": fmt_amount(result.layer1_after),
            },
            "weight_with_at_least_two_sources": {
                "before": fmt_amount(result.layer2_before),
                "after": fmt_amount(result.layer2_after),
            },
            "samples_before": _status_counts(result.before_counts),
            "samples_after": _status_counts(result.after_counts),
            "uncovered_after": [
                {"sample_id": model.samples[i].key,
                 "weight": fmt_amount(model.samples[i].weight),
                 "reason": _uncovered_reason(
                     _sources_for_sample(result, i, include_new=True),
                     result.inputs.radius_km)}
                for i, c in enumerate(result.after_counts) if c == 0],
            "single_source_after": [
                {"sample_id": model.samples[i].key,
                 "weight": fmt_amount(model.samples[i].weight),
                 "reason": "only one source within the radius"}
                for i, c in enumerate(result.after_counts) if c == 1],
        },
        "objectives": [
            {"layer": 1, "direction": "maximise",
             "name": "total weight of samples with >= 1 source",
             "before": fmt_amount(result.layer1_before),
             "after": fmt_amount(result.layer1_after)},
            {"layer": 2, "direction": "maximise",
             "name": "total weight of samples with >= 2 distinct sources",
             "before": fmt_amount(result.layer2_before),
             "after": fmt_amount(result.layer2_after)},
            {"layer": 3, "direction": "minimise",
             "name": "purchase cost of chosen new sites",
             "value": fmt_amount(result.total_cost)},
            {"layer": 4, "direction": "stable key order",
             "name": "candidate key ordering tie-break",
             "value": " | ".join(result.chosen)},
        ],
        "algorithm": {
            "method": ("exact: optional candidates split into coverage-independent "
                       "components; each component solved by a signature dynamic "
                       "program (covered-once / covered-twice masks, one cheapest "
                       "plan per signature, key-bit tie-break), then components "
                       "combined by an exact knapsack over total purchase cost"),
            "parts": {
                "n_components": len(result.component_sizes),
                "component_sizes": result.component_sizes,
                "component_state_counts": result.component_states,
            },
            "worst_case_complexity": (
                "sum over components of O(m_c * S_c), S_c <= min(2^m_c, 3^s_c) "
                "distinct coverage signatures (m_c candidates, s_c served "
                "samples in component c); the combine is O(n_cost_levels^2). "
                "Equivalent to enumerating 2^n_optional subsets, but merges "
                "identical coverage signatures and splits on independent "
                "components"),
            "n_candidates": len(model.candidates),
            "n_optional_candidates": result.n_optional,
            "constructed_subsets_enumerated_equivalently": str(result.subsets_equivalent),
            "dp_states_final": result.dp_states_final,
            "dp_states_created": result.states_explored,
        },
        "unselected_improvements": unselected,
        "output_dir": output_dir or "",
    }


def validation_payload(inputs):
    return {
        "verdict": "ok",
        "checked": {
            "samples": len(inputs.samples),
            "candidates": len(inputs.candidates),
            "existing_stations": len(inputs.stations),
        },
        "rules": {
            "coordinates": "finite; lat in [-90, 90]; lon in [-180, 180]",
            "keys": "unique within each table; candidate keys disjoint from station keys",
            "candidate_coordinates": "distinct between candidates",
            "sample_weight": "> 0",
            "candidate_cost": ">= 0",
            "budget": ">= 0",
            "radius_km": "> 0",
            "required_forbidden": "disjoint, all named candidates exist, "
                                  "required cost <= budget",
        },
        "source_files": inputs.source_files,
        "warnings": inputs.warnings,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _atomic_write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _csv_text(fieldnames, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    w.writeheader()
    for row in rows:
        w.writerow({name: row.get(name, "") for name in fieldnames})
    return buf.getvalue()


def _json_text(payload):
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_outputs(result, output_dir):
    """Write the five planning files into ``output_dir`` (atomic replace).

    Each run overwrites exactly these files; no forecast score, telemetry
    or observation file is touched.
    """
    os.makedirs(output_dir, exist_ok=True)
    paths = {
        "summary": os.path.join(output_dir, "plan_summary.json"),
        "validation": os.path.join(output_dir, "plan_validation.json"),
        "purchase": os.path.join(output_dir, "purchase_recommendation.csv"),
        "coverage": os.path.join(output_dir, "sample_coverage.csv"),
        "improvements": os.path.join(output_dir, "unselected_improvements.csv"),
    }
    _atomic_write_text(paths["summary"], _json_text(summary_payload(result, output_dir)))
    _atomic_write_text(paths["validation"], _json_text(validation_payload(result.inputs)))
    _atomic_write_text(paths["purchase"], _csv_text(
        ("candidate_id", "name", "lat", "lon", "cost", "decision", "selected",
         "reason", "serves_samples", "n_serves_samples", "contribution_samples",
         "improves_if_added", "newly_covered_weight", "newly_redundant_weight",
         "fits_remaining_budget"),
        _candidate_rows(result)))
    _atomic_write_text(paths["coverage"], _csv_text(
        ("sample_id", "reach", "lat", "lon", "weight", "status_before",
         "status_after", "sources_before", "sources_after",
         "sources_before_json", "sources_after_json",
         "coverage_gap_reason", "gain_type"),
        _sample_rows(result)))
    _atomic_write_text(paths["improvements"], _csv_text(
        ("candidate_id", "sample_id", "effect", "sample_weight",
         "candidate_cost", "fits_remaining_budget"),
        _improvement_rows(result)))
    return paths


def run_plan(samples_path, candidates_path, stations_path, budget, radius_km,
             required=None, forbidden=None, output_dir=None):
    """Load, validate, solve and (optionally) write outputs.

    Validation failures raise :class:`PlanningError` *before* solving or
    writing anything, so no empty "success" recommendation is produced.
    """
    inputs = load_plan_inputs(samples_path, candidates_path, stations_path,
                              budget, radius_km, required, forbidden)
    result = solve(inputs)
    paths = write_outputs(result, output_dir) if output_dir else {}
    return result, paths
