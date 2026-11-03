# dev-workflow FS benchmark

Replays a real kernel dev cycle (config → build → search → read → edit →
incremental build → commit) inside one disposable Linux checkout, so the
filesystem under test sees the metadata/write pattern of an agent editing a
large source tree.

在同一个一次性 Linux checkout 内重放真实的内核开发循环（配置 → 构建 → 搜索 →
读取 → 编辑 → 增量构建 → 提交），让被测文件系统承受「智能体编辑大型源码树」的
元数据与写入模式。

## Artifacts / 产物

| Path | Where | Purpose |
| --- | --- | --- |
| `dev_workflow_fs.py` | repo root | the benchmark (stdlib only / 仅标准库) |
| `data/dev-workflow/overlayfs-ovl-file.json` | fixture | base commit + 5 replayed commits |
| `data/dev-workflow/commit-0N-{search,read,edit}.cmds` | fixture | per-commit shell commands |
| `data/dev-workflow/tinyconfig-overlayfs.config` | fixture | kernel config copied over `.config` |
| `results/dev_workflow_fs/<UTC-run-id>/result.json` | project | timings, steps, assertions |

## Run / 运行

```sh
uv run dev_workflow_fs.py --repo /mnt/testfs/linux
uv run dev_workflow_fs.py --repo /mnt/testfs/linux --require-fstype ext4 --jobs 16
uv run dev_workflow_fs.py --repo /mnt/testfs/linux --install-deps --verbose
```

Linux only. `--repo` must be disposable: it is cloned from `--linux-url`
(`torvalds/linux` by default) when absent, and afterwards reset with
`git reset --hard` + `git clean -fdx`.

仅支持 Linux。`--repo` 必须是一次性目录：不存在时从 `--linux-url` 克隆，运行
结束后用 `git reset --hard` 与 `git clean -fdx` 还原。

## Workload / 负载

1. **Preparation (untimed)** — clone/reuse checkout, fetch the fixture commits,
   `checkout --detach <base>`, clean. 准备阶段不计时。
2. **Timed section** — `make tinyconfig` → install fixture config →
   `make olddefconfig` → full `make -jN` → per commit: 3 `grep`, 1–3 `sed -n`
   reads, 2–20 `%%`-separated edit blocks (each `patch`/`python3`/`sed`), then
   `make -jN`, `git status`, `git diff --stat`, `git add`, `git commit`.
3. **Assertion** — after each commit the `HEAD^{tree}` must equal the fixture
   commit's tree; any mismatch fails the run. 每个 commit 后校验树一致性。

Totals from the shipped fixture: 5 commits, 15 search + 10 read + 41 edit
commands, 53 zero-cost checkpoint markers, 1 full build, 5 incremental builds.

## Output / 输出

`result.json` (schema 3) contains `success`, `error`, `preparation_ms`,
`elapsed_ms` (timed section only), `cleanup_ms`, `steps[]` with per-command
`ms`, `checkpoint_count`, `tree_assertions[]`, `filesystem`,
`required_filesystem`, `jobs`, `base_commit`.

`elapsed_ms` 只覆盖计时区间；`preparation_ms`（克隆/检出）与 `cleanup_ms`
（reset + clean）单独记录，不应混入对比。

## Notes / 注意事项

- Checkpoint markers are zero-cost; timings are **not** comparable with
  backend-checkpoint runs. 检查点仅作分界标记，不可与后端检查点测量对比。
- A reused checkout keeps build cache state, so repeated runs are not equal to
  fresh-worktree runs. 复用 checkout 会保留构建缓存。
- The checkout must already contain (or be able to fetch) the pinned upstream
  commits; a partial/shallow/sparse clone is rejected.
- `git clean -fdx` is destructive — never point `--repo` at a working tree you
  care about. `git clean -fdx` 具有破坏性。
- Fixture pipelines run under plain `bash -c` without `pipefail`.
- Ownership marker lives in `.git/`; concurrent runs on one checkout are not
  supported. 并发运行不受支持。
