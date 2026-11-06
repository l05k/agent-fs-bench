# Results, analysis and plots

## Layout

```text
results/<bench>/<run-id>/
  results.jsonl   # the only raw data
  summary.csv     # derived; regenerate any time
  <target>/       # optional per-target diagnostics
```

`results.jsonl` is self-contained: analysis needs no mounts, logs or work
directories. Results are written outside timed regions. A failed check emits no
successful measurement.

## JSONL rows

Usually one row per case × repeat. Exceptions:

- dev-workflow / mini-dev-workflow: a full workflow is one row; steps are in `details`.
- multi-host: rows split by host / phase / round.
- npm-install: one row per manager × concurrency batch (`case` like `npm/c1`,
  `pnpm/c2`); metrics aggregate the batch, per-worker samples are in `details.tasks`.

| Field | Meaning |
| --- | --- |
| `benchmark`, `run_id` | Workload; UTC run ID with microseconds |
| `target.name`, `target.path` | Target alias and actual directory |
| `case`, `repeat` | Case and repeat index |
| `parameters` | Workload parameters, tool versions, input/binary fingerprints |
| `source_sha256` | Fingerprint of benchmark source and fixed inputs |
| `metrics` | Flat numeric metrics; names carry units |
| `details` | Workload-specific values, checks, steps, raw samples |

## summary.csv

Written automatically once all targets succeed and the JSONL is closed. If a later
target fails, earlier rows stay in JSONL but no summary is written; those rows are
not a complete experiment.

```text
target,case,metric,samples,mean,median,minimum,maximum,stddev,benchmark,run_ids,parameters,source_sha256
```

Each row aggregates `benchmark × target × case × metric × parameters ×
source_sha256`. For clone, `target` is `target.name/copier`, separating native
clone from `cp-r` on one filesystem. `parameters` is canonical JSON (keys sorted
recursively, no whitespace). Because `parameters` and `source_sha256` are grouping
keys, different configs or source versions never merge silently, while repeats of
the same experiment across run IDs do. `run_ids` lists the contributing runs,
sorted and `;`-separated. Use JSONL, not CSV, for per-operation percentiles,
outliers or step analysis.

## analyze

DuckDB query over any JSONL files. Views: `measurements` (nested JSON) and
`metrics` (numeric long table with `parameters_canonical`).

```sh
# Default summary to stdout; quote globs
uv run python -m agent_fs_bench analyze 'results/*/*/results.jsonl'

# Regenerate one run's summary
uv run python -m agent_fs_bench analyze 'results/clone/<run-id>/results.jsonl' \
  --output 'results/clone/<run-id>/summary.csv'

# Rebuild a disposable DuckDB database
uv run python -m agent_fs_bench analyze 'results/*/*/results.jsonl' \
  --database results/analysis.duckdb

# List config identities per case, for plot selectors
uv run python -m agent_fs_bench analyze 'results/*/*/results.jsonl' \
  --sql 'SELECT DISTINCT benchmark, "case", parameters_canonical, source_sha256 FROM metrics ORDER BY 1,2'
```

## plot

Requires `uv sync --locked --group analysis`. Writes `<stem>.pdf`, a 600-dpi
`<stem>.png`, and a self-contained `<stem>.py` with embedded data.

```sh
# Grouped box plot
uv run --group analysis python -m agent_fs_bench plot 'results/clone/*/results.jsonl' \
  --benchmark clone --metric mib_s --ylabel 'Throughput (MiB/s)' \
  --cases L256M/C1/cp,L1G/C1/cp --targets ours,ext4,nfs --output results/clone

# Select one config identity (copy from the parameters column of summary.csv)
uv run --group analysis python -m agent_fs_bench plot 'results/npm-install/*/results.jsonl' \
  --benchmark npm-install --metric durable_mean_s --cases npm/c1 --targets fs-a,fs-b \
  --parameters "$PARAMS" --source-sha256 <hash> \
  --ylabel 'Durable install (s)' --output results/npm-latency

# mdtest: one median/IQR curve per operation and layout, x = process count
uv run --group analysis python -m agent_fs_bench plot 'results/mdtest/<run-id>/results.jsonl' \
  --benchmark mdtest --metric ops_per_s --targets ours,nfs,ext4 --output results/mdtest-metadata
```

Plot kinds:

- **Default:** box plot per target × case, pooling samples across runs.
- **clone:** line plots over a numeric x axis, one series per `target/copier`,
  showing median and IQR of raw request samples. It writes `<stem>-bytes`,
  `<stem>-files`, `<stem>-concurrency-large` and `<stem>-concurrency-small` for
  the sweeps present in the input.
- **mdtest:** one figure per operation and layout, e.g.
  `<stem>-file-create-private.pdf`; y axis in Kops/s. Selected targets must cover
  the same process counts. These are throughput samples, not per-op latency.

Every box, line or curve accepts exactly one `(parameters, source_sha256)`
identity. If a selection mixes identities, `plot` fails and lists the
candidates. Narrow with:

- `--parameters '<json>'`: exact match against the full canonical object (key
  order and whitespace ignored, no partial match).
- `--source-sha256 <hash>[,<hash>…]`.

Selectors apply before `--cases`/`--targets`. Different cases may carry different
configs.

`--targets` sets legend order; the first is the primary system. `--cases` sets
x-axis order and filters (clone sweeps stay sorted numerically). `--size` defaults
to single-column `3.5 1.5` inches. Style follows
`.agents/skills/paper-figure-style/SKILL.md` (Arial 9 pt, vector PDF); the
implementation is in `agent_fs_bench/figure.py`. Plotting fails if Arial is missing.
