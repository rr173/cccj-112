#!/usr/bin/env bash
# End-to-end CLI demonstration for column_store.
#
#   bash examples/run_example.sh [/tmp/fresh-dir]
#
# Writes only into a fresh (empty or absent) directory.  Each step is a real
# CLI invocation in its own process, including the failure cases, whose exit
# statuses and untouched files are checked.  Exits non-zero on any mismatch.
set -euo pipefail
cd "$(dirname "$0")/.."

WORK="${1:-}"
if [ -z "$WORK" ]; then
  WORK="$(mktemp -d /tmp/column_store_cli.XXXXXX)"
  WORK="$WORK/demo"
else
  if [ -e "$WORK" ] && [ -n "$(ls -A "$WORK" 2>/dev/null)" ]; then
    echo "refusing to write into non-empty $WORK" >&2
    exit 2
  fi
fi
mkdir -p "$WORK"

PY="python3 -B"
pass() { echo "  ok: $*"; }
fail() { echo "  FAIL: $*" >&2; exit 1; }

echo "work dir: $WORK"

# --- input and schema -------------------------------------------------------
cat > "$WORK/schema.json" <<'JSON'
{"columns": [
  {"name": "event_id", "type": "integer", "nullable": false},
  {"name": "site, region", "type": "string", "nullable": true},
  {"name": "amount_cents", "type": "integer", "nullable": true},
  {"name": "is_refund", "type": "boolean", "nullable": false},
  {"name": "description", "type": "string", "nullable": true}
]}
JSON

python3 -B - "$WORK/events.jsonl" <<'PY'
import json, sys
rows = []
regions = ["Norte", "Sul", "Leste", "Oeste", "Norte", "Norte", "Sul"]
for i in range(96):
    small = i < 32
    amount = ([-(10 ** 6 + i), -1, 0, 9, 10][i % 5] if small
              else [10, 250000, -1, 2 ** 41 + i, 7, 10 ** 9 + i, 0, 9][i % 8])
    rows.append({
        "event_id": 9000 + i,
        "site, region": None if i % 9 == 0 else regions[i % len(regions)],
        "amount_cents": None if i % 6 == 0 else amount,
        "is_refund": i % 5 == 0,
        "description": None if i % 11 == 0 else (
            "settlement %06d %s" % (i, ("detail-%d " % i) * 6)),
    })
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
PY

SRC_SUM_BEFORE="$(sha256sum "$WORK/events.jsonl" | cut -d' ' -f1)"

# --- build ------------------------------------------------------------------
echo "build"
$PY -m column_store build --schema "$WORK/schema.json" \
    --input "$WORK/events.jsonl" --store "$WORK/events.store" \
    --row-group-rows 6 > "$WORK/build.json"
pass "built store ($(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["row_count"])' "$WORK/build.json") rows)"

set +e
$PY -m column_store build --schema "$WORK/schema.json" \
    --input "$WORK/events.jsonl" --store "$WORK/events.store" \
    > "$WORK/rebuild.log" 2>&1
RC=$?
set -e
[ "$RC" -ne 0 ] || fail "rebuild over an existing store must fail"
pass "existing store refused (exit $RC): $(cat "$WORK/rebuild.log")"

# --- a bad input line aborts the build -------------------------------------
printf '%s\n' '{"event_id": 1, "site, region": "X", "amount_cents": 5, "is_refund": "no", "description": "d"}' \
    > "$WORK/bad.jsonl"
set +e
$PY -m column_store build --schema "$WORK/schema.json" \
    --input "$WORK/bad.jsonl" --store "$WORK/bad.store" \
    > "$WORK/bad.log" 2>&1
RC=$?
set -e
[ "$RC" -ne 0 ] || fail "bad input must abort the build"
[ ! -e "$WORK/bad.store" ] || fail "failed build left a store"
pass "bad input rejected (exit $RC): $(cat "$WORK/bad.log")"

# --- narrow scan ------------------------------------------------------------
echo "narrow scan: site, region where amount_cents > 10"
$PY -m column_store scan --store "$WORK/events.store" \
    --columns '["site, region"]' \
    --where 'amount_cents > 10' \
    --output "$WORK/region_above_10.jsonl" \
    --report "$WORK/region_above_10.report.json" > "$WORK/scan.json"
python3 - "$WORK/region_above_10.report.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
assert r["row_groups_skipped_by_stats"] > 0, r
assert r["columns_read"] == ["amount_cents", "site, region"], r
assert "description" not in r["bytes_read_by_column"], r
print("  groups total=%d checked=%d skipped=%d pages=%d bytes=%d" % (
    r["row_groups_total"], r["row_groups_checked"],
    r["row_groups_skipped_by_stats"], r["pages_read"],
    r["bytes_read_from_data_pages"]))
PY
pass "narrow scan used statistics and read only two columns"

# --- naive oracle comparison ------------------------------------------------
python3 -I tests/naive_scan.py --schema "$WORK/schema.json" \
    --input "$WORK/events.jsonl" --columns '["site, region"]' \
    --where 'amount_cents > 10' > "$WORK/naive.jsonl"
cmp -s "$WORK/naive.jsonl" "$WORK/region_above_10.jsonl" \
    || fail "narrow result differs from the naive oracle"
pass "result matches the naive row-at-a-time oracle"

# --- projection must be refused for an unknown column ----------------------
set +e
$PY -m column_store scan --store "$WORK/events.store" \
    --columns "no_such_column" --output "$WORK/nope.jsonl" \
    > "$WORK/nope.log" 2>&1
RC=$?
set -e
[ "$RC" -ne 0 ] || fail "unknown column must be refused"
[ ! -e "$WORK/nope.jsonl" ] || fail "refused scan created an output file"
pass "unknown projection column refused (exit $RC)"

# --- an existing result is never overwritten --------------------------------
cp "$WORK/region_above_10.jsonl" "$WORK/existing.jsonl"
set +e
$PY -m column_store scan --store "$WORK/events.store" \
    --columns "event_id" --output "$WORK/existing.jsonl" \
    > "$WORK/existing.log" 2>&1
RC=$?
set -e
[ "$RC" -ne 0 ] || fail "scan must not overwrite an existing result"
cmp -s "$WORK/existing.jsonl" "$WORK/region_above_10.jsonl" \
    || fail "existing result was modified"
pass "existing output preserved (exit $RC)"

# --- source file and store are untouched by scanning ------------------------
SRC_SUM_AFTER="$(sha256sum "$WORK/events.jsonl" | cut -d' ' -f1)"
[ "$SRC_SUM_BEFORE" = "$SRC_SUM_AFTER" ] || fail "source file changed"
pass "source file byte-identical after scans"

# --- non-projected description page damage: narrow query survives -----------
python3 -B - "$WORK/events.store" <<'PY'
import os, sys
sys.path.insert(0, os.path.abspath("."))
from column_store.catalog import load_store
meta = load_store(sys.argv[1])
page = meta.groups[0].columns[4]
path = os.path.join(sys.argv[1], "data", meta.groups[0].file)
with open(path, "r+b") as fh:
    fh.seek(page.offset + page.length // 2)
    b = fh.read(1)
    fh.seek(page.offset + page.length // 2)
    fh.write(bytes([b[0] ^ 0xFF]))
print("  damaged description page of group 0 at offset", page.offset)
PY
$PY -m column_store scan --store "$WORK/events.store" \
    --columns '["site, region"]' --where 'amount_cents > 10' \
    --output "$WORK/after_damage_narrow.jsonl" > /dev/null
pass "narrow query still succeeds with a damaged description page"

set +e
$PY -m column_store scan --store "$WORK/events.store" \
    --columns "description" --output "$WORK/after_damage_full.jsonl" \
    > "$WORK/damaged.log" 2>&1
RC=$?
set -e
[ "$RC" -ne 0 ] || fail "projecting the damaged column must fail"
[ ! -e "$WORK/after_damage_full.jsonl" ] || fail "failed scan left output"
pass "projecting the damaged column fails with no partial result (exit $RC)"

echo "all CLI checks passed; artifacts under $WORK"
