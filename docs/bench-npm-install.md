# npm-install FS benchmark

Measures how fast a **warm cache/store** can materialise a **cold
`node_modules`** tree on the filesystem under test, for both npm and pnpm.

测量在「缓存/存储已预热」的前提下，向冷 `node_modules` 物化依赖树的速度，
分别覆盖 npm 与 pnpm。

Two placements share one workload, both selected in `CONFIG`. In
**same-filesystem** mode (`CONFIG["prep_dir"] is None`, the default) the caches
sit next to `node_modules`, so the filesystem is free to hardlink or
server-side clone package contents. In **cross-filesystem** mode
(`CONFIG["prep_dir"]` set to an existing directory on another filesystem)
everything the timed install only reads moves there, so every timed write is
real data written to the filesystem under test.

同盘与跨盘两种准备方式共用同一个脚本，由模块顶部的 `CONFIG` 选择：同盘
（`prep_dir` 为 `None`，默认）时文件系统可以用硬链接/服务端 clone 省掉真实
写入；跨盘（把 `prep_dir` 设为另一文件系统上的已存在目录）时被计时的写入全部
落在被测文件系统上。

## Layout and results

Run on Linux with Python 3.12+, Node, npm and/or pnpm installed. Targets are
mounted externally; this workload never mounts or unmounts anything.

| Path | Purpose |
| --- | --- |
| `agent_fs_bench/benchmarks/npm_install.py` | stdlib workload module and its `CONFIG` |
| `data/pnpm-bench/`, `data/pnpm-bench-integrated/` | committed fixture inputs, resolved from repository root |
| `<target>/agent-fs-bench/npm-install/<run-id>/work/*-run-<n>/node_modules` | timed installation |
| `<target>/agent-fs-bench/npm-install/<run-id>/{cache,templates,work}` | same-FS preparation |
| `<prep-dir>/agent-fs-bench/npm-install/<run-id>/<target>/{cache,templates,work}` | cross-FS preparation |
| `results/npm-install/<run-id>/results.jsonl` | one complete measurement per manager/repeat |
| `results/npm-install/<run-id>/<target>/logs/` | diagnostic install logs |

The fixture's committed manifest and lockfiles are **inputs**, not result
tables. Fixture identity, exact resolved manifests, lockfile SHA-256 hashes,
tool versions, workspace settings, cache policy and placement/device
information are embedded in JSONL for offline analysis. There are no
workload-specific CSV results, text manifests, or generated reports. The common
runner derives `summary.csv` only after all selected targets succeed. If a
later target fails, earlier successful measurements remain in JSONL, but no
automatic CSV is generated; these rows are a partial experiment.

## Fixture / fixture

Only committed fixtures are installed. `CONFIG["fixture"]` names one of the
trees pnpm's own install benchmark uses, vendored under `data/`; the default is
`pnpm-bench`:

| Name | Upstream | Shape |
| --- | --- | --- |
| `pnpm-bench` | `pnpm11/benchmarks/fixture` | `bench-project`, 84 deps + 4 devDeps |
| `pnpm-bench-integrated` | `pnpm/tasks/integrated-benchmark/src/fixtures` | `alotta-modules`, 109 deps, ships a `pnpm-workspace.yaml` |

The fixture's own manifest and committed lockfile define the tree; nothing is
re-resolved.

- **pnpm** installs with `--frozen-lockfile` from the committed
  `pnpm-lock.yaml`, so the payload is exactly what pnpm's benchmark measures.
  The fixture's `pnpm-workspace.yaml` is copied into every run project (its
  `storeDir` is overridden by the explicit `--store-dir`); for
  `pnpm-bench-integrated` it also pins `supportedArchitectures` and
  `enableGlobalVirtualStore: false`.
- **npm** cannot read a pnpm lockfile, and these trees have peer ranges npm's
  resolver rejects (e.g. `eslint@9` against `@typescript-eslint/parser@7`), so
  each fixture carries a `package-lock.json` resolved with
  `--legacy-peer-deps`. The timed npm install uses the same flag. A missing
  committed lockfile is an error, never a request to resolve another graph.

Fixtures are installed online exactly once, during the seed phase; the timed
runs are still `--offline`. To replay the other fixture, set
`CONFIG["fixture"] = "pnpm-bench-integrated"` before the run; fixture selection
is code-only.

The two fixtures differ in what they are good for: `pnpm-bench` is the shape
pnpm's CI tracks (fewer packages), `pnpm-bench-integrated` is larger and adds
the cross-platform `supportedArchitectures` payload, which roughly doubles the
installed file count for pnpm. Compare a filesystem against itself, not the two
fixtures against each other.

只复现 pnpm 官方 benchmark 的两个提交 fixture（默认 `pnpm-bench`）：pnpm 侧用
提交的 `pnpm-lock.yaml` 加 `--frozen-lockfile`，npm 侧用同一 fixture 内附带的、
由 `--legacy-peer-deps` 解析出的 `package-lock.json`。fixture 只在 seed 阶段联网
一次，计时阶段依旧 `--offline`；切换 fixture 只能改 `CONFIG["fixture"]`。

## Configuration / 配置

The public workload CLI has no flags: run it with the common `--dirs`/`--target`
selection only, and change this mapping in
`agent_fs_bench/benchmarks/npm_install.py` for a different experiment.

| Key | Default | Meaning |
| --- | --- | --- |
| `fixture` | `"pnpm-bench"` | Committed fixture tree (`pnpm-bench` or `pnpm-bench-integrated`). |
| `pm` | `"both"` | Manager selection: `npm`, `pnpm`, or `both`. |
| `runs` | `5` | Timed runs per selected manager; must be positive. |
| `concurrency` | `4` | Parallelism pin applied to each manager; `0` keeps tool defaults. |
| `prep_dir` | `None` | `None` = same-filesystem placement; a `Path` on another filesystem = cross-filesystem placement. |
| `allow_same_device` | `False` | Permit a configured `prep_dir` on the target's device. |
| `npm_bin` | `"npm"` | npm executable name or path resolved on `PATH`. |
| `pnpm_bin` | `"pnpm"` | pnpm executable name or path resolved on `PATH`. |
| `ignore_scripts` | `True` | Pass `--ignore-scripts` to seeding and timed installs. |
| `no_prewarm` | `False` | Skip pre-reading cache/store before timed runs. |
| `warm_once` | `False` | Prewarm only after seeding, not before every timed run. |
| `keep_node_modules` | `False` | Keep each run's `node_modules` for inspection. |
| `seed_only` | `False` | Prepare cache/store and lockfiles, then exit without timed runs. |

`CONFIG` is experiment setup, not an incidental switch: changing it changes the
measured payload or preparation semantics, so keep it fixed across the
filesystems you compare. By default the caches are warmed once after seeding
and again before every timed run; `warm_once` keeps only the post-seed warm,
and `no_prewarm` skips both.

`prep_dir` must be an existing directory; each target/run gets an exclusive
subdirectory below `<prep_dir>/agent-fs-bench/npm-install/<run-id>/<target>`.
The placement is verified with `st_dev`: a `prep_dir` on the target's device
aborts the run unless `allow_same_device` is true. The preparation root itself
must already exist, and no old templates or caches outside the per-run
subdirectory are removed. Cache reuse is within repeats of the same invocation,
not across separate invocations. There is no per-artifact override:
`prep_dir` moves the npm cache, the pnpm store, the pnpm XDG cache, the
lockfile templates and the seed projects in one step.

## Run / 运行

Default same-filesystem experiment — `pnpm-bench`, both managers, five timed
runs each, concurrency 4, lifecycle scripts ignored, per-run prewarm:

```sh
python -m agent_fs_bench run npm-install --dirs /mnt/fs-a
```

Typical configuration changes (edit `CONFIG`, then rerun the same command):

```python
CONFIG["runs"] = 3
CONFIG["pm"] = "pnpm"
CONFIG["concurrency"] = 8
CONFIG["seed_only"] = True
```

Cross-filesystem placement — cache/store, templates and seed projects on
another filesystem:

```python
from pathlib import Path
CONFIG["prep_dir"] = Path("/var/tmp/npm-prep")
CONFIG["runs"] = 3
```

Minimal real smoke (online seed, followed by one offline install per manager):

```python
CONFIG["runs"] = 1
```

```sh
python -m agent_fs_bench run npm-install --dirs /mnt/fs-a
```

This needs registry access during seeding and working npm/pnpm executables.
Cross-FS coverage additionally needs `prep_dir` on a genuinely different
device; `allow_same_device` does not test that property. Workspace coverage
needs a second run with `CONFIG["fixture"] = "pnpm-bench-integrated"`;
`seed_only` is preparation, not a timed measurement.

## Concurrency / 并发

`CONFIG["concurrency"]` fixes the parallelism of the timed install at `N`; the
default is **4**, which is also npm's threadpool default and pnpm's macOS copy
pool default. The two managers have no shared knob, so `N` is applied to the
ones with a measured effect:

| Manager | Pinned by `CONFIG["concurrency"] = N` | Tool default | Measured effect |
| --- | --- | --- | --- |
| npm | `UV_THREADPOOL_SIZE=N` | 4 | `1` → ~20% slower; `2`/`4` equal; above 4 slower |
| pnpm (Linux/Windows) | `RAYON_NUM_THREADS=N` | `clamp(2×cpu, 4, 16)` | the pool that copies the store into `node_modules` |
| pnpm (macOS) | `PNPM_MACOS_LINK_THREADS=N` | 4 | `1` → 1.98s, `4` → 1.11s, `16` → 0.90s |

`concurrency = 0` disables pinning and leaves every tool at its own default.

**npm has no install-wide concurrency option.** Arborist's
`[_unpackNewModules]` fans every pending package out to pacote at once, and
there is no CLI flag or config key to bound it. What *is* boundable is the
libuv threadpool, because the unpack writes are async `fs.write` calls; that is
the one pin above. It is a real but weak lever on this workload — the store
materialisation is dominated by the fan-out, not by the pool width.

**pnpm's timed install is almost entirely its copy phase**, so the copy pool is
what `N` has to reach. On Linux and Windows that is rayon
(`RAYON_NUM_THREADS`). On macOS the copy runs on a separate, smaller pool whose
default is 4, and `RAYON_NUM_THREADS` alone changes nothing there — both are set
so one `N` means the same thing on either platform.

Deliberately **not** set, because they do not affect a timed offline install:

| Knob | Why not |
| --- | --- |
| `PNPM_MAX_WORKERS` | the pnpm 11 Node worker pool; pnpm 12's binary never reads it |
| `--network-concurrency` | bounds downloads; an offline install fetches nothing (measured effect < 1%) |
| `--child-concurrency` | bounds lifecycle scripts; `ignore_scripts` is the default (measured effect < 1%) |
| `--maxsockets` | registry sockets only, same reason as `--network-concurrency` |

The seed phase uses the same values, so cache/store population and timed runs
use the same parallelism. JSONL records the concurrency mapping, actual timed
command arguments and effective environment pins, alongside tool versions.
Keep concurrency and manager/version fixed when comparing runs.

`CONFIG["concurrency"]` 固定计时安装的并发度，默认 **4**。两个包管理器没有共同的
开关，N 只会写进实测有效的旋钮：npm 是 `UV_THREADPOOL_SIZE`（它没有安装级并发
选项，解包扇出本身无上限），pnpm 是 `RAYON_NUM_THREADS` 与 macOS 上独立的
`PNPM_MACOS_LINK_THREADS`（默认 4）。`PNPM_MAX_WORKERS` 只有 pnpm 11 读，下载/
脚本类限制对离线安装没有影响，所以都不设置；`concurrency = 0` 表示不固定、沿用
各自默认。

## Workload / 负载

1. **Setup (untimed)** — the fixture's manifest and committed lockfiles are
   installed once. npm installs it into a seed project (network) to build the
   npm cache; pnpm seeds its store from the fixture's committed
   `pnpm-lock.yaml` with `--frozen-lockfile`. In cross-filesystem mode the seed
   project lives under `prep_dir`, so the network install never writes to the
   target.
2. **Prewarm (untimed)** — every cache/store file is read once to fill the page
   cache (`no_prewarm` / `warm_once` change this).
3. **Timed install** — a fresh project directory holding only
   `package.json` + lockfile (+ the fixture's `pnpm-workspace.yaml`):
   - npm: `npm install --offline --cache <cache> --legacy-peer-deps`
   - pnpm: `pnpm install --offline --store-dir <store> --frozen-lockfile
     --package-import-method=copy --config.enable-global-virtual-store=false`
4. **Timed flush** — every file and directory under `node_modules` is fsynced
   individually, so the data is on the filesystem before the timer reports.
   This is recorded separately as `sync_s`.
5. **Measured afterwards (untimed)** — the `node_modules` tree is walked for
   file/dir/symlink/hardlink counts, logical bytes, allocated bytes
   (hardlinked inodes counted once) and `du -sk`.

`node_modules` is deleted after each run unless `keep_node_modules` is true.

## pnpm import method and cloning / 导入方式与 clone

pnpm's import method is pinned to `copy` (`PNPM_IMPORT_METHOD` in the script):

- `hardlink` and `auto` create links to the store, so `node_modules` on the
  same filesystem costs almost no write I/O and measures the link path instead
  of the data path;
- `copy` forces real file content into `node_modules`. With
  `CONFIG["prep_dir"]` set, the source is on another filesystem, so the copy
  cannot become a link, a reflink or a server-side clone.

npm always unpacks from its cacache into new files, so it has no equivalent
switch; the `allocated_bytes / logical_bytes` ratio tells you whether anything
was deduplicated anyway.

pnpm 只允许 `copy`：`hardlink`/`auto` 会链接 store 而不真正写数据；跨盘时
`copy` 的源在另一块盘上，无法退化成链接、reflink 或服务端 clone。npm 只会从
cacache 解包，没有对应开关，用 `allocated_bytes / logical_bytes` 比值判断是否被
去重。

## Output / 输出

Each successful manager/repeat emits numeric metrics: `elapsed_s`, `sync_s`,
`sync_errors`, `user_s`, `sys_s`, `files`, `dirs`, `symlinks`, `hardlinks`,
`logical_bytes`, `allocated_bytes`, `du_kib`, `files_per_s`, `sync_files_per_s`,
`logical_mib_per_s`, and `allocated_mib_per_s`.

`elapsed_s` includes subprocess execution and opening/closing its diagnostic
log, but not preparation, preread, tree metrics, or cleanup. `sync_s` sums only
the individual fsync calls; walking and opening/closing descriptors are outside
that sum. It follows and is excluded from `elapsed_s`. CPU times are child
resource-usage deltas. Manager repetitions run serially.

Install failures, missing installed trees, traversal failures, fsync failures,
and failed metrics commands abort the run. No successful row is emitted for a
failed case; `sync_errors` is zero in completed rows. Earlier completed JSONL
lines remain available. Cache prereads fail on unreadable inputs, but still do
not pin pages in memory. An untimed global sync separates seed work and repeats.

The common envelope includes benchmark/run/target/environment and effective
`CONFIG` parameters. Resolved input manifests, lock hashes, versions, fixture
identity, concurrency, workspace settings and import policy live in parameters;
details include actual commands, placement paths/device IDs and cache-warming
policy. Use common offline analysis/plot commands on exported JSONL, grouping
matching dependency identity, manager/version, placement and policies. Logs or
a live target directory are not required to interpret the measurements.

## Notes / 注意事项

- Same-filesystem mode is the default and keeps the historical behaviour, but
  the filesystem under test may then answer with hardlinks or server-side
  clones. Cross-filesystem mode is the one that measures real writes; enable it
  by assigning `CONFIG["prep_dir"]`.
  同盘模式仍可能被硬链接/服务端 clone 加速；要测真实写入请设置
  `CONFIG["prep_dir"]`。
- Cross-filesystem mode reads the caches over the other filesystem's stack, so
  a slow or network-backed `prep_dir` becomes visible in `elapsed_s`. Keep it
  on a fast local filesystem. 跨盘缓存所在文件系统的性能会计入 `elapsed_s`。
- `prep_dir` state is shared by every target that points at it, and caches are
  not cleaned up. Give distinct experiments their own prep directory; do not
  run concurrent preparations in the same directory.
- `allocated_bytes` is `st_blocks × 512`, de-duplicated per inode;
  `logical_bytes` counts every directory entry, so hardlink layouts differ from
  `du_kib` by design.
- The benchmark root stays on the target filesystem after the run; delete it
  yourself. 基准根目录会保留在被测文件系统上。
- Whole-file fsync costs one syscall pair per file and directory. On
  high-latency filesystems `sync_s` can dominate; it is reported separately for
  that reason. 逐文件落盘在高延迟文件系统上可能成为主要开销，因此单独计时。
- The npm and pnpm arms install **different graphs**: pnpm replays the fixture's
  committed `pnpm-lock.yaml`, npm resolves the same `package.json` under
  `--legacy-peer-deps` using its committed `package-lock.json`. The file/byte
  counts therefore differ by design — compare npm-against-npm or
  pnpm-against-pnpm across filesystems, not npm against pnpm within one run.
  两个 manager 装的是**不同的依赖图**（pnpm 用提交的 `pnpm-lock.yaml`，npm 用
  同一个 `package.json` 在 `--legacy-peer-deps` 下的解析结果），文件数/字节数
  不可跨 manager 直接比较。
- The npm timed command is therefore `npm install --offline
  --legacy-peer-deps`; the JSONL `command` field records it verbatim.
