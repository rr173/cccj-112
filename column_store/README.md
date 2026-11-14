# column_store — a small columnar event store for offline collected records

Offline capture tools write one JSON object per line.  Analysts usually need
only two of the fields but have to read and parse the whole (often large)
file.  `column_store` imports a UTF-8 JSONL file into a compact columnar
store split into row groups, and scans it by predicate and projection while
reading only the pages a query actually needs.

Everything here is hand-written: the record scanner, the page encodings, the
catalog/index, the reader and the I/O accounting.  No Parquet, Arrow,
DataFrame, SQLite or other columnar/analytics library is involved, and only
the Python standard library is used.

The existing tools in this repository (`handover_join/`, `verif_pipeline/`,
`pcap_recover/`, `archive_stripe/`) are untouched.  Capture recovery
(`pcap_recover/`) is not modified or re-run.

## Quick start

```bash
cd column_store

# 1. declare the schema (order matters; names may contain spaces/punctuation)
cat > schema.json <<'JSON'
{"columns": [
  {"name": "event_id",     "type": "integer", "nullable": false},
  {"name": "region",       "type": "string",  "nullable": true},
  {"name": "amount_cents", "type": "integer", "nullable": true},
  {"name": "is_refund",    "type": "boolean", "nullable": false},
  {"name": "description",  "type": "string",  "nullable": true}
]}
JSON

# 2. build a store from UTF-8 JSONL (source is never modified)
python3 -B -m column_store build \
    --schema schema.json --input events.jsonl --store events.store \
    --row-group-rows 4096

# 3. filter and project into a NEW JSONL file
python3 -B -m column_store scan \
    --store events.store \
    --columns "region" \
    --where "amount_cents > 10" \
    --output region_above_10.jsonl \
    --report region_above_10.report.json
```

Both commands exit `0` on success and `2` on a rejected request or a
format/integrity failure; errors go to stderr.  `python3 -B -m column_store
info --store events.store` prints the schema, per-group encodings, null
counts and statistics.

The two Python entry points are `column_store.builder.build(source_path,
store_path, schema, row_group_max=...)` and `column_store.scanner.scan(store,
projection, conditions)` / `scan_to_file(...)`; the CLI is a thin wrapper.

## Schema

A schema is an ordered list of columns.  Each column states exactly:

* `name` — a non-empty string; spaces, punctuation and non-ASCII are fine.
  Names are never used as file names (the store maps them to column indexes
  and loads pages by index from the catalog).
* `type` — `integer` (signed 64-bit), `string` or `boolean`.
* `nullable` — `true` or `false`, and must be stated.

Source records must contain exactly the declared fields: every field must be
declared, every declared field must be present exactly once, and a null is
only allowed where `nullable` is true.  Violations name the physical input
line, the field and the reason.  Booleans are never accepted for integers
(and vice versa), JSON numbers with a fraction or exponent are rejected, and
integers are range-checked numerically against int64 bounds.  Strings are
kept exactly as their Unicode code points: empty strings, NUL bytes and
non-ASCII text are neither normalized nor altered.  Invalid UTF-8, lone
surrogates, duplicate keys, extra keys, missing keys, non-objects and empty
lines all abort the build with a line number and cause; no store is left
behind.  The builder holds only the current row group in memory, and both a
single line and a single string have configurable size bounds.

## scan

`scan` takes an ordered list of projection columns and zero or more
conditions combined with AND.  Operators: `eq`, `lt`, `le`, `gt`, `ge`,
`is_null`, `is_not_null`.  Value comparisons exist only for integer and
string columns; literals are type-checked (an integer column wants an
integer, a string column wants a string, booleans reject value comparisons).
A null never satisfies a value comparison.  Strings compare by Unicode code
point order (not dictionary index and not UTF-8 byte tricks), and integers
compare numerically (not by their decimal text).

Results are rows of projected values in the original input order and the
projection order.  A predicate column does not need to be projected.  An
empty result is a normal success.  Unknown columns, type-incompatible
literals and unsupported format versions are rejected before any output
file is created.

## What a scan reads

The catalog carries per-group, per-column `null_count`, `min` and `max`.  A
group is skipped entirely when its statistics prove no row can match (e.g.
`amount > 10` when the group's maximum is 10, or `is_null` when it has no
nulls).  For the remaining groups only the pages of columns used by a
predicate or the projection are read, and each page is one exact `pread`
verified by CRC.  Columns that are neither filtered on nor projected are
never opened, and a group skipped by statistics is never opened at all.

Each scan writes side-band statistics (with `--report`): groups total,
checked and skipped-by-statistics (with the reason per skipped group), pages
read with their group/column/offset/length, bytes read from those pages,
bytes read for the index (header and catalog), rows matched and emitted.
These byte counts are measured I/O, not estimates.

The index (header + catalog) is fully validated before any query — a
damaged index is always rejected.  Each page read is validated for
truncation, CRC, header CRC, row count and null-bitmap size; a damaged page
fails the query that reads it and leaves no output file, while damage in
pages a query does not read does not affect it.  A failed scan never
overwrites an existing output or an existing store; the source file and the
store's bytes are unchanged by scanning.

## Layout and format

```
events.store/
  HEADER               80-byte root header + CRC-32 + SHA-256 of the catalog
  data/g000000.bin     pages of row group 0 (one per column, contiguous)
  data/g000001.bin     ...
```

All details — field tables, the null bitmap, the four page encodings, the
catalog schema, statistics rules and the query-stopping rules — are in
[docs/FORMAT.md](docs/FORMAT.md), which is sufficient to write a second
reader.  `tests/test_decoder_independence.py` does exactly that on every
test run.

## Tests, demo and stress

```bash
bash scripts/run_tests.sh          # unittest, 87 tests
python3 -B scripts/demo.py --work-dir /tmp/column_store_demo
python3 -B scripts/stress_run.py --rows 1000000 --row-group-rows 4096 \
    --work-dir /tmp/column_store_stress --json /tmp/stress.json
```

* `tests/naive_scan.py` is an independent row-at-a-time filterer (built-in
  `json.loads` per line, no store code).  Comparisons of values, order and
  nulls are made against it, both in-process and by subprocess, including a
  randomized fuzz over schemas, encodings, nulls and predicates.
* `tests/test_io_audit.py` compiles a small `LD_PRELOAD` shim
  (`scripts/readwatch.c`) that logs every read a child process performs and
  proves that a narrow query touches no description-column page and no
  skipped-group page, matching the report byte for byte.
* `scripts/stress_run.py` records row-group configuration, input size,
  per-process peak RSS (kernel high-water mark from `os.wait4`) and the
  measured data bytes for a narrow vs full scan.  Results are in
  `stress_results/` and summarized in `stress_results/stress_report.md`:
  200 000 → 2 000 000 rows at fixed group size keeps build peak RSS at
  ~18–23 MB while narrow scans read ~4 % of the full-column bytes.

## Limits and non-goals

* One store is immutable: no appends or updates.  A new import writes a new
  directory.
* The catalog is per-store JSON; its size grows with the group count (about
  1 KB per group here), so very small row groups on huge inputs make the
  index large.
* `scan_to_file` streams rows to disk; `scan()` (the API used by tests and
  small queries) returns the matched rows in memory.
* Supported format version is 1; a store whose header declares anything
  else is rejected as unsupported, not ignored.
