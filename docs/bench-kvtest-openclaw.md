# Transactional SQLite writes via official `kvtest`

Runs SQLite's own [`test/kvtest.c`](https://github.com/sqlite/sqlite/blob/version-3.54.1/test/kvtest.c)
with `--update --random`: overwrite existing BLOB rows. The default
`CONFIG["multitrans"] = True` adds `--multitrans`, one autocommit transaction per
write; False batches all writes in one transaction. Each database is
single-writer; a run may start several independent instances, one per task. It
is not an OpenClaw replay or application-latency estimate.

OpenClaw motivates this workload because its per-agent database stores session
and transcript state. The research question is: **under the same declared SQLite
workload and persistence settings, how does the underlying filesystem affect
transactional write cost?** This complements tree-copy and development-workflow
benchmarks with database pages, journal/WAL writes and synchronization. It does
not measure model inference, tool execution, or complete agent-turn latency.

## Why `--multitrans`

Without `--multitrans`, kvtest wraps the operations in one outer transaction;
with it, each update commits separately. This gives explicit transaction
granularity without inventing an application schema. It does not establish that
OpenClaw commits once per user turn: an application turn, transcript append,
SQL statement and committed transaction are different units. Their relationship
must be observed at the actual writer, not inferred from this benchmark.

## Run

```sh
uv run python -m agent_fs_bench run kvtest --mode local --dirs /mnt/fs-a
# Smoke test: two tasks, two repeats and 200 writes; needs two logical CPUs.
uv run python -m agent_fs_bench run kvtest --mode local --dirs /mnt/fs-a -- --smoke
```

`--mode` selects the machines: `local` is the controller, `overflow` adds the
`local/hosts.toml` machines in file order after the controller, and `remote`
uses only those machines. Workload flags follow `--`. `--smoke` selects a small
real workload; it is not an experiment dimension.

Experiment settings are the `CONFIG` mapping at the top of
`agent_fs_bench/benchmarks/kvtest.py`. Change that code for an ablation; do not
commit private settings. Defaults: `count=2000`, `size=2048`, `variance=0`,
`cache_size=1000` KiB, `jmode=delete`, `multitrans=True`, `repeats=5`, `mmap=0`,
`nosync=False`, `tasks=1`, `cpus_per_task=1`.
FULL synchronization is pinned at build time; kvtest does not print a
connection-level readback. `nosync=True` explicitly requests OFF and is not part
of the NAS baseline. `build_dir` and `force_download` manage the build cache and
are not experiment parameters.

`tasks` is the external concurrency: that many independent kvtest instances,
each with its own database. Each instance is a single connection and the
workload is single-threaded, so `cpus_per_task` stays 1. N is one value per run,
never swept inside a run; obtain a curve by pooling several runs with
`plot --vary tasks`. Tasks above a machine's CPU count share CPUs, which is not
part of the default baseline.

For transaction-granularity sensitivity, run fixed sizes such as 1024, 4096 and
16384 bytes with `variance=0`, separately for `multitrans=True` and False. Each
row carries journal/sync mode, size, variance, count, cache, mmap, transaction
mode, `tasks`, repeat, task `sample` and node `host` as key columns, so these
settings do not collapse into the same summary group. Each task uses its own
database; no database has concurrent users, so this is not a same-database
concurrency benchmark.

On each machine, the node leader (the first task on that machine) downloads the
SQLite 3.54.1 amalgamation and `kvtest.c` into `build/kvtest/` in that machine's
checkout (git-ignored), or into `CONFIG["build_dir"]`, and compiles `kvtest` once
with `-O2` using `CC`, or `cc` then `gcc` when unset, before any task starts. Both
SQLite workloads use `sqlite_tools.py`: `SQLITE_THREADSAFE=1`, loadable extensions
omitted, and default rollback/WAL synchronization fixed to FULL (`2`). The
compiler version, source and binary hashes, exact compile argv and flags are
saved in `kvtest-build.json`; the compiler version, source/binary hashes and
pinned SQLite version are also reported per node under
`targets[].info.hosts.<node>`.

Downloads are checked against the fixed SHA-256 values in `sqlite_tools.py`
before replacing the cache; existing archives and test-source files are checked
before reuse. Those values were derived from the official 3.54.1 release archives
after verifying their [published SHA3-256](https://sqlite.org/download.html), and
the raw test files were matched byte-for-byte to the verified source archive. A
machine without internet access needs `sqlite-amalgamation-3540100.zip`,
`kvtest.c` and the unpacked `sqlite3.c`/`sqlite3.h` pre-placed in its build
directory; the archive and test-source SHA-256 checks still apply before
reuse. `build_dir` must be local to each machine, never a directory shared
between machines.

Later runs reuse the binary only when it is newer than `kvtest.c`, `sqlite3.c`
and `sqlite3.h`, and its metadata matches the binary hash, current source hashes
and compile flags. Reuse does not resolve or run any compiler, so a host that only
consumes an existing cache does not need one; the per-node manifest info records
the cached compiler's `--version` line and the source/binary hashes, not the
current `CC`. If an
up-to-date binary has missing or incomplete metadata, or metadata that does not
match the binary hash, the run fails and asks for exactly one explicit rebuild
(`CONFIG["force_download"] = True`, or delete the binary and metadata); it never
guesses provenance or treats an unproven binary as current. Changing a source,
header or compile flags rebuilds, as does setting force. A failed compile or version probe leaves
the previous binary and metadata untouched. Set `CC` to choose a compiler and
`CONFIG["build_dir"]` to move the download/compile cache. Paths and the exact
compile argv stay in `build/kvtest/kvtest-build.json`; they are not copied into
the results tables.

`CONFIG["jmode"]` is lowercased. The mode kvtest's own connection reports is in
`targets[].info.observed[].journal_mode` and may differ for nonpersistent modes.

The generic NAS baseline is DELETE + FULL on every target. SQLite warns that
network-filesystem locking and synchronization vary; WAL is not a generic
network-filesystem baseline, including when all connections run on one client.
See [SQLite over a network](https://sqlite.org/useovernet.html) and
[WAL restrictions](https://sqlite.org/wal.html). A successful run or integrity
check does not establish crash durability or safe multi-client access. FULL in
rollback mode is not an unconditional power-loss durability guarantee; see
[synchronous](https://sqlite.org/pragma.html#pragma_synchronous).

## Deployment

Deploy the same checkout with the shared Python package and use Python 3.12+ on
every machine. `uv sync --locked` installs the standard library dependencies and
Matplotlib in each machine's environment. Every task reports through task 0 as
the run proceeds; a failure raised by any task stops the run before summaries
and figures are written. No single-file helper generation or separate script
deployment is supported.
Each machine that runs tasks needs network access for the first source download
and a C compiler (`CC`, `cc` or `gcc`) for the first build; a valid cached build
needs neither. A machine without internet access can instead receive the
verified sources in its build directory. FTS5 is not required. The framework
allocates a fresh per-node and per-task directory for each run; each task
initializes its own database there. The first build uses the downloaded SQLite
header and does not require a system SQLite development package.


`CONFIG["count"]` is both the number of rows seeded by `kvtest init` and the
number of writes in the timed phase, because `kvtest run` touches that many
keys.

## What the numbers mean

`kvtest` prints `SQLite version`, `--cache-size ... --jmode ...`,
`Database page size`, `Total elapsed time`, `Microseconds per BLOB write` and
`Content write rate`. The harness requires all of them; a missing field aborts
the run instead of defaulting a value. After its timed repeats, each task runs
the same official binary's `stat <db>` once and requires `Integrity-check: ok`;
the run fails if any task's check is not `ok`.
Each timed run overwrites randomly selected existing initialized rows
(`kvtest --random`). Later repeats retain the rows left by earlier overwrites;
each repeat emits one row per task, not an averaged result.

The run writes one flat table:

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `repeats` | `jmode`, `sync` (`full`/`off`), `transaction` (`autocommit`/`batch`), `payload_bytes`, `variance_bytes`, `count`, `cache_size_kib`, `mmap_bytes`, `tasks`, `repeat`, `sample` (task index), `host` (node) | `microseconds_per_write`, `write_rate_mb_s`, `total_elapsed_s` |

`summary/repeats.csv` pools repeats per target, setting and task host and
reports `n` and each metric's median. The manifest's `targets[].info` holds the
pinned SQLite version, the final `integrity_check` result, and `observed`: per
repeat and task, the values kvtest read back from SQLite (`journal_mode`,
`cache_size` as the raw PRAGMA value, negative meaning KiB, and `page_size` in
bytes). Each node leader's compiler version, source/binary hashes and SQLite
version are under `targets[].info.hosts.<node>`.

The SQLite version is the pinned release compiled into the measured binary, not
the Python `sqlite3` module version; every parsed `SQLite version` line must
match it. `--mmap` is only echoed by kvtest and
`synchronous` is never printed, so neither is reported as observed.

The harness passes a negative SQLite `cache_size` so `CONFIG["cache_size"]` is
in KiB, not pages. `targets[].info.observed[].cache_size` is the raw PRAGMA value
kvtest printed, so `-1000` means 1000 KiB.
`targets[].info.observed[].journal_mode` comes from kvtest's own connection output
(nonpersistent modes can differ on a later inspection connection). After its
timed repeats, each task runs `kvtest stat <db>` once with the same binary,
outside any timing; any `Integrity-check` result other than `ok` is a hard error
and the run stops.

The default figure is a box plot of `microseconds_per_write` with `tasks` on x,
one box per concurrency level and target, split into one figure per transaction
mode. `uv run python -m agent_fs_bench plot <run-dir>...` re-renders the figures
for finished runs; pool runs with different `tasks` using `--vary tasks`, and
runs with different `multitrans` values using `--vary multitrans`.

The official SQL update loop does not reliably reject failed commits under lock
contention. Never point other readers/writers at a task's database. Concurrency
comes only from separate tasks, each with its own database; a held reader lock
can produce exit status 0, claimed writes and `Integrity-check: ok` with
unchanged database contents. Integrity checks validate structure, not successful
commits.

Each row is one instance's amortized result for a complete invocation. Repeating
it measures run-to-run variation, not a per-transaction latency distribution. Do
not derive p95/p99 transaction latency from these rows. Caches are not evicted.

## Versioned OpenClaw evidence and limits

The application reference is
[`openclaw@5f6f15fed0d6ba618cf781c9686a8b93f499564b`](https://github.com/openclaw/openclaw/tree/5f6f15fed0d6ba618cf781c9686a8b93f499564b).
These are version-specific source facts, not measurements of deployed sessions:

- [Database layout](https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/docs/reference/database-schemas/layout.md#L12-L16)
  lists `~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite` as the
  per-agent store for sessions, transcripts and other agent state.
- [Transcript payload storage](https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/docs/reference/database-schemas/layout.md#L56-L66)
  retains event JSON as TEXT or compressed `event_zstd` BLOBs. An export's JSON
  byte length is therefore not necessarily its stored SQLite payload size.
  [Cold archives](https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/docs/reference/database-schemas/layout.md#L214-L234)
  can hold authoritative history outside SQLite; this microbenchmark excludes them.
- [Agent connection setup](https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/src/state/openclaw-agent-db.ts#L465-L476)
  requests `synchronous=NORMAL`, foreign keys and `autoCheckpointPages=0` for
  worker maintenance outside foreground commits.
- The [worker append-message handler](https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/src/agents/sessions/session-manager-metadata.worker.ts#L207-L228)
  calls the [agent write transaction owner](https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/src/state/openclaw-agent-db-admission.ts#L181-L229).
  [Nested calls use savepoints](https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/src/infra/sqlite-transaction.ts#L359-L441),
  and [batch append](https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/src/config/sessions/session-accessor.sqlite-transcript-store.ts#L403-L449)
  can insert several events inside the caller's transaction. Neither a fixed
  event count nor a fixed commits-per-turn ratio follows from these entry points.
- [Filesystem policy](https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/src/infra/sqlite-wal-filesystem.ts#L10-L24)
  and [journal setup](https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/src/infra/sqlite-wal.ts#L300-L324)
  choose rollback journaling for some network/cross-VM storage instead of WAL.
  Do not describe WAL as an unconditional OpenClaw setting on every target.

| Dimension | Reference application | Current kvtest workload |
| --- | --- | --- |
| Data operations | Transcript/state writes and derived projections | Random overwrite of existing BLOBs in one table |
| Transaction granularity | Must be captured at the writer | Autocommit per update (default), or one batch transaction |
| Payload sizes and operation frequencies | No empirical distribution collected here | Deliberately synthetic `CONFIG` inputs |
| Schema/content work | Metadata, indexes, FTS and optional compression | Primary key only; no FTS or transcript compression |
| Journal mode | Path-dependent policy | Explicit `jmode`; actual mode printed in results |
| Synchronization | Agent setup requests NORMAL | SQLite build default (normally FULL), or OFF with `nosync`; not read back |
| WAL maintenance | Worker-managed; timing must be observed | SQLite default autocheckpoint; no application worker |

The retired synthetic session workload is not evidence for OpenClaw's message
size distribution, events per turn, or FTS cost share. No such empirical claims
are made here. Current defaults (`size=2048`, `variance=0`, `count=2000`)
are experiment controls, not fitted application statistics. In SQLite 3.54.1,
kvtest initializes lengths using `size + random() % (variance + 1)`. SQLite
`random()` and its remainder are signed: a nonzero variance spans
`size - variance` through `size + variance`, not only sizes above the requested
size. The default therefore uses exactly 2048-byte BLOBs. For example,
`size=2048, variance=1024` permits 1024–3072 bytes. Content is random, not a
byte-identical application replay. See the [pinned initialization](https://sqlite.org/src/raw/test/kvtest.c?ci=version-3.54.1).

Changing journal mode, synchronization, transaction grouping, schema or checkpoint
policy changes the workload. FULL versus NORMAL is not a calibrated latency
upper bound, and fewer tables is not a calibrated lower bound. Keep each setting
fixed across targets and report the comparison as a SQLite microbenchmark.
For network targets, use the common rollback baseline; a successful single-process
WAL run is not evidence that WAL is supported safely by that filesystem.
One transaction does not imply one fsync: under [WAL with NORMAL synchronization](https://www.sqlite.org/pragma.html#pragma_synchronous),
most transactions do not sync, while checkpointing performs synchronization.
This is why commit grouping and maintenance policy both matter to the comparison.

`Microseconds per BLOB write` is total elapsed time divided by the operation
count, including connection setup and final close/checkpoint. It is not a
sampled BEGIN-to-COMMIT latency distribution.

## Minimal real-workload calibration plan (not yet performed)

The goal is an **empirically calibrated microbenchmark**, not a full OpenClaw
performance evaluation. Static analysis supplies candidate paths and settings;
an instrumented application run supplies transaction and size observations.

1. **Pin a small, explicit workload set.** Start with fixed text-only sessions,
   coding/tool-use sessions, and a separate bounded large-tool-output case. Use
   public task inputs, a fixed tool workspace and model configuration; record
   the actual outputs because model responses need not be deterministic. Begin
   with three independent sessions per case as a pilot, then increase coverage
   if transaction/size profiles differ materially. This is a selected workload
   corpus, not a representative sample of all OpenClaw users. Keep fresh-session
   setup and continued-session appends separate; label the large-output case as
   a sensitivity case rather than assigning it an invented production frequency.
2. **Capture the actual SQLite writer, including worker connections.** After
   serialization/compression, record scenario/session/turn identifiers, connection
   and transaction identifiers, ordered SQL templates, parameter types and byte
   lengths, BEGIN/COMMIT/ROLLBACK outcomes and statement row counts. Count outer
   committed transactions, not SAVEPOINT release operations or transcript events.
   Preserve read statements that participate in the write transaction. Record
   actual connection PRAGMAs, SQLite version, schema identity and initial row/DB
   sizes. Attribute background maintenance separately from transcript writes.
   SQL tracing must cover the native writer and its transaction owner, not only
   RPC submission or UI events. A final DB or JSONL export can help measure
   retained row sizes, but cannot reconstruct deleted events, write ordering,
   past transaction boundaries or previous checkpoint work.
3. **Measure storage work as well as logical messages.** For each transaction,
   retain event UTF-8 bytes, encoded/stored bytes, statements/rows by operation,
   compression use, relevant FTS writes, and checkpoint timing/completion. WAL
   length alone is insufficient because allocated WAL space may be reused.
   A short optional syscall/VFS trace can validate write/sync/locking behavior,
   but does not replace SQL/bind/transaction observations. Keep collection output
   outside the measured storage. Instrumented collection timings are not the
   filesystem performance results.
4. **Freeze and validate the resulting workload.** For metadata-only calibration,
   export transaction grouping and size sequences without private message text.
   Such metadata cannot support a faithful FTS/compression replay by itself:
   equal-length random bytes do not preserve tokenization or compressibility.
   A content-sensitive replay must retain the controlled public corpus (and
   relational ID consistency), the real schema/indexes, statement order and
   stored representation. Publish the corpus hash and provenance. Validate
   completed transactions, resulting rows and DB integrity before timing it.
5. **Separate calibration from comparison.** Generate/record the corpus once,
   then use the same initial DB state and frozen operations on every filesystem;
   do not rerun the model independently on each target. Remove model/tool/network
   think time for a capacity-oriented storage microbenchmark, preserving SQL
   transaction boundaries and dependencies. An arrival-paced experiment is a
   different case and must not be called saturation throughput. Either preserve
   and measure the application's checkpoint owner, or choose and disclose a
   controlled policy; do not silently move all checkpoint work out of the result.

For this study, start with one writer and a fixed prefilled DB state; add readers
only if the captured sessions show relevant overlap. Report completed transactions
per second, individually sampled transaction latency if the replay provides it,
and checkpoint/drain cost under an explicit timing boundary. Use whole independent
runs for uncertainty across targets, not thousands of correlated events as if
they were independent experimental repetitions.

SQLite WAL checkpointing here means transferring committed WAL pages back into
the database; it is not a filesystem snapshot or the removed workflow checkpoint
mechanism. Include the selected SQLite maintenance policy even when filesystem
snapshots are out of scope.

### Smallest practical collection route

First collect only normal hot transcript appends; treat imports, compaction and
cold archival as out of scope unless one is explicitly added as a separate case.
Pin OpenClaw and its Node runtime. Supported recent Node versions expose
[`sqlite.db.query`](https://nodejs.org/docs/latest-v24.x/api/diagnostics_channel.html#event-sqlitedbquery)
through `node:diagnostics_channel` (added in 24.22.0 / 26.8.0). OpenClaw's
[runtime range](https://github.com/openclaw/openclaw/blob/5f6f15fed0d6ba618cf781c9686a8b93f499564b/package.json#L2530-L2532)
also permits older Node versions without this event, so verify coverage rather
than treating an empty trace as no database activity.

Subscribe from a preload in every writer process/thread and confirm it observes
the known append. The event provides the connection, expanded SQL and an internal
statement-duration estimate; it has no structured bind array, transaction ID,
start event or turn ID. Expanded SQL contains the bound message text/BLOBs and
must not be published as harmless metadata. It is useful to discover actual
statement order and transaction shapes on a controlled public corpus, not to
measure end-to-end transaction latency or infer exact bind lengths by string
length. Expansion also makes BLOBs hex text rather than their stored byte count.

For the small calibration run, add a narrow observation at the actual connection
execution and transaction owner when exact bind lengths, turn attribution or
commit outcomes are needed. Record string UTF-8 and BLOB byte lengths before
binding, plus transaction completion; do not build a general tracing platform.
Observe checkpoint completion outside the SQL callback. Inspect the final DB to
cross-check retained sizes, but not as a substitute for the runtime write trace.

The resulting extension, if needed, can be a small hot-append microbenchmark:
reuse the pinned storage path or captured transaction groups and required schema
with the frozen public payloads, while excluding the model, gateway and tool
execution from timing. There is no need to replay the entire agent. Preserve or
explicitly exclude content-dependent FTS/compression work, and state that boundary.

### What can remain in `kvtest`

Keep this official workload as the controlled single-table baseline. Use explicit
payload-size cases (and a rows/DB working set) to test sensitivity; call them
synthetic unless fitted from captured storage-side observations. `kvtest` cannot
express an arbitrary size sequence, transcript append semantics, the real schema,
NORMAL synchronization or the application checkpoint worker. Matching a median
message size does not make it an OpenClaw replay. If those dimensions matter,
evaluate a separate minimal schema/transaction replay instead of adding that
claim to kvtest. No new replay or collector is implemented by this document.

### Paper wording

For the current implementation (no empirical calibration yet):

> Motivated by the use of SQLite for agent session and transcript state in the
> referenced OpenClaw revision, we evaluate a controlled SQLite microbenchmark
> using the official kvtest workload. In the default case, each operation overwrites
> an existing BLOB in its own transaction. We hold workload and persistence settings fixed across
> filesystems. Payload sizes are synthetic experimental controls, not a measured
> OpenClaw distribution; the experiment neither replays OpenClaw nor estimates
> its end-to-end latency.

Only after performing the sampling plan, describe a new workload as calibrated
from the named sessions and report their revision, scenarios, counts, observed
transaction/size profiles and archived corpus. Do not generalize that selected
corpus to all OpenClaw usage or call metadata-only calibration a faithful replay.

## Artifacts

```text
<target>/agent-fs-bench/kvtest/<run-id>/<node>/task-NNNN/kvtest.sqlite
                             per-task database retained on the target
build/kvtest/                per-machine sources, compiled binary (git-ignored),
                             and kvtest-build.json build provenance
results/kvtest/<run-id>/manifest.json
                             config, source hash and per-target info
results/kvtest/<run-id>/repeats.csv
                             one row per repeat and task
results/kvtest/<run-id>/summary/repeats.csv
                             n and metric medians per target, setting and host
results/kvtest/<run-id>/figures/
                             default box plot
```

In DELETE mode the rollback journal may exist during a write transaction and is
deleted by a successful commit; normal completion leaves `kvtest.sqlite`, not a
persistent journal file.

Source revision, compiler and workload requests must match when comparing runs;
the manifest records the source/binary hashes and SQLite version per node, and
the observed values per repeat and task.
`microseconds_per_write` amortizes connection setup and final close/checkpoint;
it is not a distribution of individually sampled write latencies.
