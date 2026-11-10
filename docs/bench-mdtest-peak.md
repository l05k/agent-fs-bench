# mdtest peak

Push a client to its throughput ceiling with the unmodified **IOR mdtest 4.0.0**. This complements the fixed-total-work [`mdtest`](bench-mdtest.md) runs, which cannot show a ceiling: their fixed 65536-object total leaves a few thousand objects per task at high N, so phases are short enough for barrier and launch overhead to dominate. They also move no file data.

`mdtest-peak` uses **weak scaling**. Each task gets a fixed file count, so the total is n×N and phase time grows once the target saturates. That growth makes the plateau visible. Every point reports file ops/s; create and read with non-empty files also report MiB/s.

## Commands

Prerequisites match [`mdtest`](bench-mdtest.md): the same `mdtest` 4.0.0 on every task machine, Open MPI 4.1, and targets mounted externally. Machine placement follows [run modes](run-modes.md).

```sh
# Default: 128 tasks dealt round-robin over the --mode machines.
uv run python -m agent_fs_bench run mdtest-peak --mode local --dirs /mnt/fs-a /mnt/fs-b

# Small real end-to-end check (16 files/task, one repeat); not a result.
uv run python -m agent_fs_bench run mdtest-peak --mode local --dirs /mnt/smoke -- --smoke
```

One run measures one task count and one file size. A peak curve is several runs with explicit `CONFIG["tasks"]` (for example powers of two up to 2× the CPUs, plus the CPU count) pooled with `plot --vary tasks`.

## Settings

`CONFIG` in `agent_fs_bench/benchmarks/mdtest_peak.py`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `mdtest_bin` | `mdtest` | Native IOR 4.0.0 executable, the same on every machine |
| `tasks` | `128` | N, a positive integer; tasks above a machine's CPU count share CPUs, and oversubscribed points are intended |
| `file_size_bytes` | `4096` | Bytes written and read per file (one size per run) |
| `items_per_task` | `None` | Files per task; `None` takes the per-size default (0 B and 4 KiB 8192, 64 KiB 2048, 1 MiB 256), which keeps 128 tasks within a few tens of GiB per launch; an integer overrides it, and another size needs one |
| `repeats` | 3 | Fresh launch and working directories per repeat |
| `layout` | `private` | `private`: one directory per machine with per-task trees (`-u`); `shared`: one common directory |

Each launch is `mdtest -a POSIX -F -i 1 -n <n> -w S -e S -y [-u]`:

| Operation | Timed work |
| --- | --- |
| `file-create` | open(O_CREAT), write S bytes, fsync, close |
| `file-stat` | stat |
| `file-read` | open, read S bytes, close |
| `file-unlink` | unlink |

`-y` makes mdtest fsync after each write. IOR 4.0.0 skips the write for S=0, so the 0 B create has no fsync; it measures pure create/close metadata cost. Reads follow writes in the same launch with **no cache drop** (`cache_state = write-then-read-same-launch-no-drop`), so read MiB/s can come from the client page cache. Do not report it as storage-server read bandwidth.

## Results

Each run writes one flat CSV per record kind plus a `config.json`:

| Table | Key columns | Metric columns |
| --- | --- | --- |
| `phases` | `operation`, `layout`, `tasks`, `repeat`, `file_size_bytes`, `items_per_task` | `ops`, `elapsed_s`, `ops_per_s` |
| `bandwidth` | `operation`, `layout`, `tasks`, `repeat`, `file_size_bytes`, `items_per_task` | `bytes`, `elapsed_s`, `mib_per_s` |
| `ranks` | `operation`, `layout`, `tasks`, `repeat`, `file_size_bytes`, `items_per_task`, `sample`, `host` | `elapsed_s` |

`bandwidth` holds only create and read with S>0. A `ranks` row is one task's phase time before the end barrier, from mdtest's per-rank CSV rows. There is no warm-up launch: every repeat is measured.

| Metric | Definition |
| --- | --- |
| `ops` | n×N, checked against mdtest's own `N tasks, n×N files` report |
| `elapsed_s` | Aggregate `all`-row phase time from the CSV: max over tasks, including the end barrier |
| `ops_per_s` | ops / elapsed_s |
| `bytes` | ops×S, logical create/read bytes |
| `mib_per_s` | bytes / MiB / elapsed_s, for create/read with S>0 |

Timing, validation, the per-machine directories and the IOR 4.0.0 CSV item-count caveat are identical to [`mdtest`](bench-mdtest.md#timing-and-results); launch checks and helpers are shared with that module. `config.json` records each machine's facts (`nodes`) and the task count per machine (`placement`).

## Default figures

`plot` renders one default figure per operation, layout and size for both metrics, plus standalone Python scripts with the raw samples. The default figures are `phases-ops_per_s-...` and `bandwidth-mib_per_s-...`; examples: `figures/phases-ops_per_s-file_read-private-4096.pdf` and `figures/bandwidth-mib_per_s-file_read-private-4096.pdf` (punctuation in split values becomes `_`).
- X is the task count (log scale); Y is Kops/s or MiB/s, with the median/IQR over repeats.
- One run gives one x point; pool runs of different `tasks` to draw the curve.
- The `bandwidth` table only holds create/read with S>0, so its figures contain those rows only.

```sh
# Pool one size and layout at several task counts.
uv run python -m agent_fs_bench plot results/mdtest-peak/<run-n8> results/mdtest-peak/<run-n128> \
  --vary tasks --targets ours,nfs
```

`plot` accepts several run directories of the same benchmark and rejects runs whose config or source fingerprint differ, except in keys given to `--vary`. `--targets` sets legend order and filtering; the first entry is the paper's primary system. Figures split by `file_size_bytes`, so runs of several sizes can be pooled with `--vary tasks,file_size_bytes,items_per_task`.

The ceiling is the plateau of a curve. Points beyond the CPU count show whether more outstanding requests still help; past that plateau the client CPU, not the target, is the bottleneck. Report the CPU count, mount options and server hardware with the numbers. In `overflow` and `remote` modes the tasks of one point can span several machines, so a curve is no longer a single-client ceiling; state the placement of each point.
