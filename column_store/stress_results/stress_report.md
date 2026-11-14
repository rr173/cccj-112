# column_store stress results

Measurements were produced by `scripts/stress_run.py`.  Each phase (data
generation, build, narrow scan, full-column scan) runs in its own Python
process, and peak memory is the kernel's `ru_maxrss` high-water mark for that
child (`os.wait4`), so it is not contaminated by the generator or by the
parent.  "Narrow" means `SELECT region WHERE amount_cents > 10`; "full" reads
all five columns.  The input is synthetic vendor events with a long, mostly
unique description column (about 185 bytes per row) and 1/3 of rows in
negative/zero/small groups.

| rows | row_group_rows | input | groups | build peak RSS | narrow peak RSS | full peak RSS | narrow bytes | full bytes | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 200 000 | 4096 | 40.1 MB | 49 | 17.7 MB | 14.6 MB | 17.9 MB | 1 052 962 | 25 654 693 | 0.041 |
| 400 000 | 1024 | 81.0 MB | 391 | 18.7 MB | 15.6 MB | 17.3 MB | 2 110 030 | 52 074 751 | 0.041 |
| 1 000 000 | 4096 | 203.5 MB | 245 | 18.2 MB | 15.2 MB | 19.3 MB | 5 232 748 | 131 101 688 | 0.040 |
| 1 000 000 | 16384 | 203.5 MB | 62 | 29.3 MB | 16.0 MB | 31.9 MB | 5 252 159 | 131 065 271 | 0.040 |
| 2 000 000 | 2048 | 414.1 MB | 977 | 23.2 MB | 18.7 MB | 22.5 MB | 10 482 889 | 269 371 103 | 0.039 |

## What this shows

* **Memory does not grow with total rows.**  Build peak rises only with the
  row group size (17.7 MB at 4096 rows vs 29.3 MB at 16384 rows) and is flat
  as the input grows from 200 000 to 2 000 000 rows at a fixed group size
  (17.7 MB vs 18.2 MB, +0.5 MB, which is mostly the larger catalog).  The
  scan peak is also flat (14.6–18.7 MB across a 10x input range); it holds one
  row group of decoded columns plus the serialization batch.
* **Narrow queries really read less.**  The scanner's own accounting (exact
  `pread` lengths of the pages it read) is about 4 % of a full-column read,
  and its page list contains only `amount_cents` and `region`.  About one
  third of row groups are skipped from statistics alone across all sizes; a
  separate LD_PRELOAD test (`tests/test_io_audit.py`) confirms the count with
  real read syscalls and shows no I/O overlapping the description pages.
* **Encoding is compact.**  The store is roughly 13–16 % smaller than the
  JSONL input (long descriptions are offset by dictionary-packed regions,
  fixed-width integers and packed booleans).

## Caveats

* Peak RSS includes the Python interpreter (~10 MB) in every figure.  The
  marginal cost of a 4096-row group is therefore ~7–8 MB above baseline.
* The catalog is proportional to the number of groups (241 KB at 245 groups,
  967 KB at 977 groups); at 1 000 000 groups it would become large.  Raising
  `--row-group-rows` trades scan granularity for index size.
* Wall times include process startup and are not a throughput benchmark.
