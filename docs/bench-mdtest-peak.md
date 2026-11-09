# Single-client mdtest peak

Push one Linux client to its throughput ceiling with the unmodified **IOR mdtest 4.0.0**. This complements the fixed-total-work [`mdtest`](bench-mdtest.md) sweep, which cannot show a ceiling for two reasons. First, it stops at 16 processes. Second, its fixed 65536-object total leaves a few thousand objects per process at high P, so phases are short enough for barrier and launch overhead to dominate. It also moves no file data.

`mdtest-peak` uses **weak scaling**. Each process gets a fixed file count, so the total is n×P and phase time grows once the target saturates. That growth makes the plateau visible. Every point reports file ops/s; create and read with non-empty files also report MiB/s.

## Commands

Prerequisites match [`mdtest`](bench-mdtest.md): `mdtest` 4.0.0 and its MPI launcher are on PATH, and targets are mounted externally.

```sh
uv run python -m agent_fs_bench run mdtest-peak --dirs /mnt/fs-a /mnt/fs-b

# Small real end-to-end check (16 files/process, P=1,2, one repeat); not a result.
uv run python -m agent_fs_bench run mdtest-peak --dirs /mnt/smoke -- --smoke
```

## Settings

`CONFIG` in `agent_fs_bench/benchmarks/mdtest_peak.py`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `mpi_command` | `("mpiexec", "--oversubscribe", "-n")` | Launcher prefix plus process-count flag. Points above the CPU count oversubscribe, which Open MPI rejects without `--oversubscribe`. Drop that flag for MPICH. |
| `items_per_proc` | `{0: 8192, 4096: 8192, 65536: 2048, 1048576: 256}` | File size in bytes → files per process. At 128 processes the 1 MiB point writes 32 GiB. |
| `conc_levels` | `None` | `None` means powers of two up to 2× available logical CPUs (`sched_getaffinity`), plus the CPU count and 2× it. With 64 CPUs: 1…64, 128. |
| `repeats` | 3 | Fresh launch and working directory per repeat |
| `layouts` | `("private", "shared")` | `-u` per-process directories versus one common directory |

Each launch is `mdtest -a POSIX -F -i 1 -n <n> -w S -e S -y [-u]`:

| Operation | Timed work |
| --- | --- |
| `file-create` | open(O_CREAT), write S bytes, fsync, close |
| `file-stat` | stat |
| `file-read` | open, read S bytes, close |
| `file-unlink` | unlink |

`-y` makes mdtest fsync after each write. IOR 4.0.0 skips the write for S=0, so the 0 B create has no fsync; it measures pure create/close metadata cost. Reads follow writes in the same launch with **no cache drop** (`cache_state = write-then-read-same-launch-no-drop`), so read MiB/s can come from the client page cache. Do not report it as storage-server read bandwidth.

## Results

Case names look like `file-read/shared/S65536/n2048/P64`. Metrics:

| Metric | Definition |
| --- | --- |
| `ops` | n×P, checked against mdtest's own `P tasks, n×P files` report |
| `elapsed_s` | Aggregate `all`-row phase time from the CSV: max over ranks, including the end barrier |
| `ops_per_s` | ops / elapsed_s |
| `bytes`, `mib_per_s` | create/read with S>0 only: ops×S and ops×S / MiB / elapsed_s (logical bytes) |

Timing, validation and the IOR 4.0.0 CSV item-count caveat are identical to [`mdtest`](bench-mdtest.md#timing-and-results). Launch checks and helpers are shared with that module. `parameters.client_cpus` records the CPU count the sweep was derived from.

## Figures

```sh
uv run --group analysis python -m agent_fs_bench plot 'results/mdtest-peak/<run-id>/results.jsonl' \
  --benchmark mdtest-peak --metric mib_per_s --targets ours,nfs --output results/mdtest-peak-bw
uv run --group analysis python -m agent_fs_bench plot 'results/mdtest-peak/<run-id>/results.jsonl' \
  --benchmark mdtest-peak --metric ops_per_s --targets ours,nfs --output results/mdtest-peak-ops
```

There is one figure per operation, layout and size, e.g. `mdtest-peak-bw-file-read-private-S1048576.pdf`. X is the process count (log scale). Y is MiB/s or Kops/s, with the median/IQR over repeats. `mib_per_s` figures exist only for create/read with S>0. Series must share the mdtest version and the cache/completion policy. Each size's files-per-process value is part of the case and of the parameters, so changing `items_per_proc` produces a separate configuration.

The ceiling is the plateau of a curve. Points beyond the CPU count show whether more outstanding requests still help; past that plateau the client CPU, not the target, is the bottleneck. Report the client CPU count, mount options and server hardware with the numbers.
