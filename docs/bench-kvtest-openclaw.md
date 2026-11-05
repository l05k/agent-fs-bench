# Transactional SQLite writes via official `kvtest`

Runs SQLite's own [`test/kvtest.c`](https://github.com/sqlite/sqlite/blob/version-3.54.1/test/kvtest.c)
with `--update --random`: overwrite existing BLOB rows. The default
`CONFIG["multitrans"] = True` adds `--multitrans`, one autocommit transaction per
write; False batches all writes in one transaction. This is a single-writer
filesystem microbenchmark, not an OpenClaw replay or application-latency estimate.

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

## Run / 运行

```sh
uv run python -m agent_fs_bench run kvtest --dirs /mnt/fs-a
```

There are no workload flags. Experiment settings are the `CONFIG` mapping at the
top of `agent_fs_bench/benchmarks/kvtest.py`. Change that code for an ablation;
do not commit private settings. Defaults: `count=2000`, `size=2048`, `variance=0`,
`cache_size=1000` KiB, `jmode=delete`, `multitrans=True`, `repeats=5`, `mmap=0`,
`nosync=False`. FULL synchronization is pinned at build time; kvtest does not
print a connection-level readback. `nosync=True` explicitly requests OFF and is
not part of the NAS baseline. `build_dir` and `force_download` manage the build
cache and are not experiment parameters.

For transaction-granularity sensitivity, run fixed sizes such as 1024, 4096 and
16384 bytes with `variance=0`, separately for `multitrans=True` and False. The
case includes journal/sync mode, size, variance, count, cache, mmap and transaction
mode, so these settings do not collapse into the same summary group. This is
still one connection (`C1`), not a same-database concurrency benchmark.

The first run downloads the SQLite 3.54.1 amalgamation and `kvtest.c` into
`build/kvtest/` at the repository root (git-ignored) and compiles `kvtest` with
`-O2` using `CC`, or `cc` then `gcc` when unset. Both SQLite workloads use
`sqlite_tools.py`: `SQLITE_THREADSAFE=1`, loadable extensions omitted, and default
rollback/WAL synchronization fixed to FULL (`2`). The compiler version, source
and binary hashes, exact compile argv and flags are saved in `kvtest-build.json`.

Later runs reuse the binary only when it is newer than `kvtest.c`, `sqlite3.c`
and `sqlite3.h`, and its metadata matches the binary hash, current source hashes
and compile flags. Reuse does not resolve or run any compiler, so a host that only
consumes an existing cache does not need one; rows record the cached compiler's
`--version` line and the source/binary hashes, not the current `CC`. If an
up-to-date binary has missing or incomplete metadata, or metadata that does not
match the binary hash, the run fails and asks for exactly one explicit rebuild
(`CONFIG["force_download"] = True`, or delete the binary and metadata); it never
guesses provenance or treats an unproven binary as current. Changing a source,
header or compile flags rebuilds, as does setting force. A failed compile or version probe leaves
the previous binary and metadata untouched. Set `CC` to choose a compiler and
`CONFIG["build_dir"]` to move the download/compile cache. Paths and the exact
compile argv stay in `build/kvtest/kvtest-build.json`; they are not copied into
JSONL.

`CONFIG["jmode"]` is case-insensitive and must be one of `delete`, `truncate`,
`persist`, `memory`, `wal`, `off`; the requested value in `parameters.jmode` is
lowercase. The mode kvtest's own connection reports is in
`details.observed.journal_mode` and may differ for nonpersistent modes. An
invalid request fails the run before any download, build or database work.

The generic NAS baseline is DELETE + FULL on every target. SQLite warns that
network-filesystem locking and synchronization vary; WAL is not a generic
network-filesystem baseline, including when all connections run on one client.
See [SQLite over a network](https://sqlite.org/useovernet.html) and
[WAL restrictions](https://sqlite.org/wal.html). A successful run or integrity
check does not establish crash durability or safe multi-client access. FULL in
rollback mode is not an unconditional power-loss durability guarantee; see
[synchronous](https://sqlite.org/pragma.html#pragma_synchronous).

## Deployment / 部署

Deploy the checkout with the shared Python package and use Python 3.12+.
Install the project's DuckDB dependency with `uv sync --locked`; the shared
runner derives `summary.csv` beside JSONL after the target loop completes. A
failure raised by a target aborts before export, so earlier JSONL rows remain
but the automatic summary CSV is not written. No single-file helper generation
or separate script deployment is supported.
The test host needs network access for the first source download and a C
compiler (`CC`, `cc` or `gcc`) for the first build; a valid cached build needs
neither. FTS5 is not required. The common runner allocates a fresh target
subdirectory for each run. The first build uses the downloaded SQLite header and
does not require a system SQLite development package.


`CONFIG["count"]` is both the number of rows seeded by `kvtest init` and the
number of writes in the timed phase, because `kvtest run` touches that many
keys.

## What the numbers mean

`kvtest` prints `SQLite version`, `--cache-size ... --jmode ...`,
`Database page size`, `Total elapsed time`, `Microseconds per BLOB write` and
`Content write rate`. The harness requires all of them; a missing field aborts
the run instead of defaulting a value. After all timed repeats finish, it
runs the same official binary's `stat <db>` once and requires
`Integrity-check: ok`.
Each timed run overwrites randomly selected existing initialized rows
(`kvtest --random`). Later repeats retain the rows left by earlier overwrites;
each repeat is a separate JSONL measurement, not an averaged result.

Each JSONL row separates requests from observations and measured timings:

- `parameters` holds the experiment inputs (`size`, `variance`, `count`,
  `cache_size_kib`, `jmode`, `nosync`, `mmap_bytes`, `multitrans`, `repeats`), plus
  `tool_versions` (`sqlite` parsed from kvtest's own stdout, `compiler` from the
  build cache), `source_sha256` for the build inputs and `binary_sha256`.
- `details.observed` holds the values kvtest read back from SQLite:
  `journal_mode`, `cache_size` (raw PRAGMA value; negative means KiB) and
  `page_size` in bytes. `details.integrity_check` is the final official
  `stat` result.
- `metrics` holds only `microseconds_per_write`, `write_rate_mb_s` and
  `total_elapsed_s`; requested settings are not repeated there.

The SQLite version is the one compiled into the measured binary, not the
Python `sqlite3` module version. `--mmap` is only echoed by kvtest and
`synchronous` is never printed, so neither is reported as observed.

The harness passes a negative SQLite `cache_size` so `CONFIG["cache_size"]` is
in KiB, not pages. `details.observed.cache_size` is the raw PRAGMA value
kvtest printed, so `-1000` means 1000 KiB. `details.observed.journal_mode` comes
from kvtest's own connection output (nonpersistent modes can differ on a later
inspection connection). After all timed repeats finish, the harness runs
`kvtest stat <db>` once with the same binary, outside any timing; any
`Integrity-check` result other than `ok` is a hard error before a row is
emitted.

The official SQL update loop does not reliably reject failed commits under lock
contention. Do not point other readers/writers at this invocation's database and
do not launch concurrent kvtest processes against it. A held reader lock can
produce exit status 0, claimed writes and `Integrity-check: ok` with unchanged
database contents. Integrity checks validate structure, not successful commits.

Each JSONL sample is a complete invocation's amortized result. Repeating it
measures run-to-run variation, not a per-transaction latency distribution. Do not
derive p95/p99 transaction latency from these rows. Caches are not evicted.

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
<target>/agent-fs-bench/kvtest/<run-id>/kvtest.sqlite
                             fresh database + rollback journal on the target
build/kvtest/                sources, compiled binary (git-ignored), and
                             kvtest-build.json build provenance
results/kvtest/<run-id>/results.jsonl
                             one complete measurement per repeat
```

Source revision, compiler and workload requests must match when comparing runs;
each row records the source/binary hashes and the observed SQLite version.
`microseconds_per_write` amortizes connection setup and final close/checkpoint;
it is not a distribution of individually sampled write latencies.
