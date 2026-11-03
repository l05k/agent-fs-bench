# npm-install FS benchmark

Measures how fast a **warm cache/store** can materialise a **cold
`node_modules`** tree on the filesystem under test, for both npm and pnpm.

测量在「缓存/存储已预热」的前提下，向冷 `node_modules` 物化依赖树的速度，
分别覆盖 npm 与 pnpm。

Two placements share one script. In **same-filesystem** mode the caches sit next
to `node_modules`, so the filesystem is free to hardlink or server-side clone
package contents. In **cross-filesystem** mode (`--prep-dir`) everything the
timed install only reads moves to another filesystem, so every timed write is
real data written to the filesystem under test.

同盘与跨盘两种准备方式共用同一个脚本：同盘时文件系统可以用硬链接/服务端
clone 省掉真实写入；跨盘（`--prep-dir`）时被计时的写入全部落在被测文件系统上。

## Artifacts / 产物

| Path | Where | Purpose |
| --- | --- | --- |
| `npm_install_fs_bench.py` | repo root | the benchmark (stdlib only) |
| gist `l05k/0c7df...` (CSV, `package`+`rank`) | network | default package catalog |
| `<--dir>/npm_install_fs_bench-*/work/*-run-<n>/node_modules` | target FS | the timed write |
| `<--dir>/npm_install_fs_bench-*` (same-FS mode) | target FS | cache, pnpm store, templates, seed projects |
| `<--prep-dir>/{cache,templates,work}` (cross-FS mode) | other FS | cache, pnpm store, templates, seed projects |
| `results/npm_install_fs_bench/<UTC-run-id>/` | project | reports only |

Results directory contents: `results.csv`, `summary.csv`,
`selected-packages.txt`, `manifest.txt`, `logs/{npm,pnpm}-run-<n>.log`, and
`top-packages.csv` when the catalog was downloaded.

## Run / 运行

Same filesystem (default) — every artifact lives on the filesystem under test:

```sh
uv run npm_install_fs_bench.py --dir /mnt/testfs --top-k 30 --runs 3
uv run npm_install_fs_bench.py --dir /mnt/testfs --packages 'react,typescript'
uv run npm_install_fs_bench.py --dir /mnt/testfs --seed-only   # prepare, no timing
```

Cross filesystem — caches, templates and seed projects on another filesystem:

```sh
uv run npm_install_fs_bench.py --dir /mnt/nas/mount \
  --prep-dir /var/tmp/npm-prep --top-k 30 --runs 3
```

Useful flags: `--pm npm|pnpm|both`, `--runs`, `--concurrency`, `--no-ignore-scripts`,
`--no-prewarm`, `--warm-once`, `--keep-node-modules`, `--npm-bin`,
`--pnpm-bin`, `--prep-dir`, `--allow-same-device`.

There is no per-artifact override: `--prep-dir` moves the npm cache, the pnpm
store, the pnpm XDG cache, the lockfile templates and the seed projects in one
step. `--prep-dir` must resolve to a different device (`st_dev`) than `--dir`;
otherwise the run aborts with an explanation unless `--allow-same-device` is
given.

## Concurrency / 并发

`--concurrency N` fixes the parallelism of the timed install at `N`; the
default is **4**, which is also npm's threadpool default and pnpm's macOS copy
pool default. The two managers have no shared knob, so `N` is applied to the
ones with a measured effect:

| Manager | Pinned by `--concurrency N` | Tool default | Measured effect |
| --- | --- | --- | --- |
| npm | `UV_THREADPOOL_SIZE=N` | 4 | `1` → ~20% slower; `2`/`4` equal; above 4 slower |
| pnpm (Linux/Windows) | `RAYON_NUM_THREADS=N` | `clamp(2×cpu, 4, 16)` | the pool that copies the store into `node_modules` |
| pnpm (macOS) | `PNPM_MACOS_LINK_THREADS=N` | 4 | `1` → 1.98s, `4` → 1.11s, `16` → 0.90s |

`--concurrency 0` disables pinning and leaves every tool at its own default.

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
| `--child-concurrency` | bounds lifecycle scripts; `--ignore-scripts` is the default (measured effect < 1%) |
| `--maxsockets` | registry sockets only, same reason as `--network-concurrency` |

The seed phase uses the same values, so the cache/store are populated under the
same parallelism as the timed runs. `manifest.txt` records `concurrency`,
`concurrency_note`, `npm_concurrency`, `pnpm_concurrency` and both timed
commands with their environment prefix, e.g.
`UV_THREADPOOL_SIZE=4 npm install --offline`. `results.csv` carries a
`concurrency` column so mixed runs stay separable.

`--concurrency N` 固定计时安装的并发度，默认 **4**。两个包管理器没有共同的开关，
N 只会写进实测有效的旋钮：npm 是 `UV_THREADPOOL_SIZE`（它没有安装级并发选项，解包
扇出本身无上限），pnpm 是 `RAYON_NUM_THREADS` 与 macOS 上独立的
`PNPM_MACOS_LINK_THREADS`（默认 4）。`PNPM_MAX_WORKERS` 只有 pnpm 11 读，下载/脚本
类限制对离线安装没有影响，所以都不设置；`--concurrency 0` 表示不固定、沿用各自默认。

## Workload / 负载

1. **Setup (untimed)** — `npm install` into a seed project (network) to build
   the npm cache and lockfile; pnpm resolves the same spec set and seeds its
   store, producing `pnpm-lock.yaml`. In cross-filesystem mode the seed project
   lives under `--prep-dir`, so the network install never writes to the target.
2. **Prewarm (untimed)** — every cache/store file is read once to fill the page
   cache (`--no-prewarm` / `--warm-once` change this).
3. **Timed install** — a fresh project directory holding only
   `package.json` + lockfile:
   - npm: `npm install --offline --cache <cache>`
   - pnpm: `pnpm install --offline --store-dir <store> --frozen-lockfile
     --package-import-method=copy --config.enable-global-virtual-store=false`
4. **Timed flush** — every file and directory under `node_modules` is fsynced
   individually, so the data is on the filesystem before the timer reports.
   This is recorded separately as `sync_s`.
5. **Measured afterwards (untimed)** — the `node_modules` tree is walked for
   file/dir/symlink/hardlink counts, logical bytes, allocated bytes
   (hardlinked inodes counted once) and `du -sk`.

`node_modules` is deleted after each run unless `--keep-node-modules`.

## pnpm import method and cloning / 导入方式与 clone

pnpm's import method is pinned to `copy` (`PNPM_IMPORT_METHOD` in the script):

- `hardlink` and `auto` create links to the store, so `node_modules` on the
  same filesystem costs almost no write I/O and measures the link path instead
  of the data path;
- `copy` forces real file content into `node_modules`. With `--prep-dir` the
  source is on another filesystem, so the copy cannot become a link, a reflink
  or a server-side clone.

npm always unpacks from its cacache into new files, so it has no equivalent
switch; the `allocated_bytes / logical_bytes` ratio tells you whether anything
was deduplicated anyway.

pnpm 只允许 `copy`：`hardlink`/`auto` 会链接 store 而不真正写数据；跨盘时
`copy` 的源在另一块盘上，无法退化成链接、reflink 或服务端 clone。npm 只会从
cacache 解包，没有对应开关，用 `allocated_bytes / logical_bytes` 比值判断是否被
去重。

## Output / 输出

`results.csv` per run: `manager, run, concurrency, status, elapsed_s, sync_s,
sync_errors, user_s, sys_s, files, dirs, symlinks, hardlinks, logical_bytes,
allocated_bytes, du_kib, files_per_s, sync_files_per_s, logical_mib_per_s,
allocated_mib_per_s, log`.

`elapsed_s` is the install only; `sync_s` is the flush that follows it and is
excluded from `elapsed_s`. `sync_errors` counts fsync/open failures (locks,
permission, unsupported operations) that were skipped. `summary.csv` per
manager: `ok_runs, mean_s, median_s, p95_s` plus the same statistics for
`sync_s` and for `elapsed_s + sync_s` (`mean_total_s`, `median_total_s`).
`manifest.txt` pins tool versions, the resolved package list, every path, the
placement (`same`/`cross`), both `st_dev` values, the concurrency mapping and
the timed commands.

`elapsed_s` 只包含安装过程，`sync_s` 是随后的落盘阶段、不计入 `elapsed_s`；
`sync_errors` 记录被跳过（拿不到句柄、文件系统不支持等）的 fsync 次数。

## Notes / 注意事项

- Same-filesystem mode is the default and keeps the historical behaviour, but
  the filesystem under test may then answer with hardlinks or server-side
  clones. Cross-filesystem mode is the one that measures real writes.
  同盘模式仍可能被硬链接/服务端 clone 加速；要测真实写入请用 `--prep-dir`。
- Cross-filesystem mode reads the caches over the other filesystem's stack, so
  a slow or network-backed `--prep-dir` becomes visible in `elapsed_s`. Keep it
  on a fast local filesystem. 跨盘缓存所在文件系统的性能会计入 `elapsed_s`。
- `--prep-dir` state is shared by every `--dir` that points at it, and the
  cached packages are never cleaned up; point each test at its own directory.
  `--prep-dir` 下的缓存不会被清理，请按测试分配目录。
- `allocated_bytes` is `st_blocks × 512`, de-duplicated per inode;
  `logical_bytes` counts every directory entry, so hardlink layouts differ from
  `du_kib` by design.
- The benchmark root stays on the target filesystem after the run; delete it
  yourself. 基准根目录会保留在被测文件系统上。
- Whole-file fsync costs one syscall pair per file and directory. On
  high-latency filesystems `sync_s` can dominate; it is reported separately for
  that reason. 逐文件落盘在高延迟文件系统上可能成为主要开销，因此单独计时。
- Two runs started in the same UTC second share (and overwrite) one results
  directory.
