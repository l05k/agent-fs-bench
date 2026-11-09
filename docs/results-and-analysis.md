# Results, summaries and figures

## Layout

```text
results/<bench>/<run-id>/
  manifest.json        # run-level facts
  <table>.csv          # one row per measurement or raw sample
  summary/<table>.csv  # derived: n and medians per target × dimensions
  figures/             # derived: default figures (.pdf, .png, .py)
  <target>/            # optional per-target diagnostics and logs
```

Each workload doc lists its tables and default figures. Rows are appended and
flushed one at a time outside timed regions, so a crash keeps every completed
measurement. A failed check emits no row.

## manifest.json

| Field | Meaning |
| --- | --- |
| `benchmark`, `run_id` | Workload; UTC run ID with microseconds |
| `source_sha256` | Fingerprint of benchmark sources and fixed inputs, including uncommitted edits |
| `config` | The workload's settings: `CONFIG` plus workload flags |
| `targets[]` | `name`, `path` and `info` (tool versions, binary hashes, fstype, ...) of each finished target |
| `tables` | `keys` and `metrics` columns of each table |

The manifest is rewritten after each target. A failed target is absent; its
partial rows stay in the CSV.

## Tables

Columns are `target`, then key columns (dimensions such as `operation`,
`procs`, `repeat`), then metric columns (numbers whose names carry units). An
empty cell means the metric does not apply to that row. Booleans are `0`/`1`.

`repeat` (1-based repetition) and `sample` (index of one raw sample within a
measurement, e.g. a request or a worker) are pooled keys; every other key is a
dimension.

## summary/

Written once all targets succeed. For each table with a `repeat` or `sample`
column: one row per target × dimensions with `n` (pooled rows) and the median of
each metric (6 significant digits). Compute other statistics from the tables.

## plot

`run` renders the workload's default figures (`FIGURES` in its module) into
`figures/` after the last target. Re-render them, pool runs, or draw a custom
figure:

```sh
# Default figures again, primary system first in the legend
uv run python -m agent_fs_bench plot results/mdtest/<run-id> --targets ours,nfs,ext4

# Pool repeated runs; their configs must match
uv run python -m agent_fs_bench plot results/mdtest/<run-a> results/mdtest/<run-b> \
  --output figures/mdtest

# One mount-scale run per client count: let `clients` differ to draw the curve
uv run python -m agent_fs_bench plot results/mount-scale/<run-n64> results/mount-scale/<run-n256> \
  --vary clients --output figures/mount-scale

# Custom figure: one per operation, private layout only, in Kops/s
uv run python -m agent_fs_bench plot results/mdtest/<run-id> --table phases \
  --x procs --y ops_per_s --split operation --where layout=private --yscale 1000 \
  --logx --xlabel 'Client processes' --ylabel 'Throughput (Kops/s)' --name private
```

- `--kind line` (default): median with IQR error bars over all pooled rows at
  each numeric x. `--kind box`: one box per x category, in first-appearance order.
- `--series` (default `target`): columns joined with `/` as legend labels, at
  most 4. Legend order follows `--targets` (which also filters), else run order.
- `--split`: one figure per value combination, titled in the upper-left corner,
  file `<name>-<values>`.
- `--where COLUMN=VALUE` (repeatable): exact match on the CSV text.
- `--xscale`/`--yscale` divide values (`1048576` for MiB); `--logx`/`--logy`;
  `--size W H` in inches (default single column `3.5 1.5`).

Runs pool only when their `benchmark` and `config` match, apart from keys given
to `--vary`; otherwise `plot` names the differing keys. Vary a key only when the
figure keeps those runs apart as x, series or split. Each figure writes
`<stem>.pdf`, a 600-dpi
`<stem>.png` and a self-contained `<stem>.py` with embedded data; `python
<stem>.py` regenerates both images. Style follows
`.agents/skills/paper-figure-style/SKILL.md` (Arial 9 pt, vector PDF),
implemented in `agent_fs_bench/figure.py`. Plotting fails if Arial is missing.

## Queries

There is no built-in query layer; DuckDB, pandas or a spreadsheet read the CSVs
directly:

```sh
duckdb -c "SELECT target, procs, median(ops_per_s) FROM 'results/mdtest/*/phases.csv' GROUP BY ALL"
```
