# Round 04｜站点预算、双 workload mount-scale 与上游能力冻结

Round 04 是下一份正式执行协议：它取代 Round 03 的 active 状态，但**不修改**
[Round 03](round-03.md)。Round 03 原文、旧 checkout 与原始结果冻结保留，只作为
参考矩阵和历史材料。本轮实现以 clean `220c066` 为基线，经**上游 PR** 合入可选能力，
不保留长期本地 patch；默认值保持 Round 03 行为。实现完成后只做最小真实挂载 smoke，
smoke 不入正式摘要；本文不声称任何正式结果。

## 0. 固定变更边界

- **M1 create 大小**：`mdtest-create` 可用 `--create-file-size-bytes`（非负整数，默认
  `0`）。`4 KiB` 时语义是 create+write+close，**不加 `-y`**（无逐文件 fsync），
  与 `mdtest-io` 的 create+fsync 不同。
- **M2 可选 stonewall**：`mdtest-create` 可用 `--stonewall-seconds`（正整数，默认
  `None`）。只影响 create-only 轴，`mdtest-io` 不传 `-W`。
- **M3 可选站点预算钩子**：profile 可选 `rank_cgroup` 路径模板，站点创建/删除
  cgroup，harness **不**创建、不删除、不改限额，只校验成员和限制并发射 proof。
  旧 `cgroup = true`、`mount_cgroup`、`{binds}` 等布局继续无效，不提供别名。
- 已实现能力的验收只看：参数真实进入 native argv、真实 backend 挂载、预算组成员/
  `cpu.max`/`memory.max`/proof/事件可核验、正常卸载无残留。若站点账号无 cgroup 写权限
  或无法完成真实挂载检查，相关预算点记 `not_run`，不以本地模拟或旧运行代替。
- 本文件冻结方案，不执行正式大实验；不做 crash injection、自动重试、自动清理失败
  挂载或生产化加固。

## 1. 共同环境与前置

所有参与主机使用**同一** checkout、同一 40 位源码 commit、Python 3.12+、Open MPI
4.1.x 与匹配 mpi4py。mdtest 使用**未修改的 IOR 4.0.0** 二进制，逐机记录 SHA-256；
不同二进制或不同 IOR/Open MPI 版本不得进入同一组。每次运行前记录：

- 源码 commit（含本轮可选能力的合入 commit）、`uv.lock`、Python/MPI/mpi4py 版本；
- mdtest、mpicc、工具、镜像 digest、fixture、Linux mirror HEAD 及编译器指纹；
- 实际 profile/wrapper 文件 SHA-256、实际挂载选项、`rank_cgroup` 模板展开结果与
  站点上限。
- 未修改 IOR 4.0.0 源码 commit 为
  `967a9f65109760db8a3ac14a7fdd007f337d2960`。正式运行选择一个构建并逐机核验同一
  二进制，不同构建或 MPI 版本不合并。

前置检查全部通过后才运行正式点：

1. 每个路径都有真实挂载，`admin_root` 是同卷完整管理视图；multi-mount 的 client
   `admin_root` 必须真实挂载，不允许用 bind 或本机目录冒充。
2. staging 为各节点同路径本地目录，位于 `admin_root` 之外；数据根、sentinel 和
   owner 校验按 [mount-scale 文档](../docs/bench-mount-scale.md) 执行。
3. 预算点先确认 `/sys/fs/cgroup` 为 v2，且 `cpu` 与 `memory` controller 已委派、
   账号可写；`rank_cgroup` 的父级存在，`cpu.max=100000 100000`、
   `memory.max=2147483648` 能由站点 wrapper 建立并逐 daemon PID 核验。只有
   memory/pids 委派时不能形成或证明 1c2g，预算点记 `not_run`。
4. 工具、replay、clone 所需 fixture、镜像、provider 与 ext4 空间已备；缺哪项就把
   依赖组记 `not_run`，不换任务、不缩规模。
5. 先做一次真实的 mount/readiness/cgroup smoke（非正式），确认后立即清理；失败
   保留现场，不自动卸载。Linux bind-mount lifecycle smoke 只能验证 argv、计数与
   清理，不是真实 FUSE/NFS backend，也不证明成功的 1c2g 组；正式数据必须有真实
   backend 与真实站点预算 proof。缺少可用授权时明确记 `not_run`，不声称成功。

## 2. 系统与预算

| 系统 | 正式臂 | 挂载语义 | create/io 缩放轴预算 |
| --- | --- | --- | --- |
| CephFS FUSE | `cephfs-fuse` | shared-directory；每 rank 一个真实 `ceph-fuse` daemon 与私有目录 | 每 rank 1c2g，daemon 与 rank 同组 |
| LakeBase | `lakebase-fast-drop` | shared-directory；每 rank 独立 PFS FUSE client、独占私有子目录 | 每 rank 1c2g，daemon 与 rank 同组 |
| NAS v4 | `nas-v4` | multi-mount；每 rank 一个真实 NFSv4.0 挂载，N ≤ 1000 | 每 rank 1c2g；**组内只有 rank**，不声称限制内核 NFS 线程 |
| ext4 | origin 本地 ext4 | 工具/replay/clone 基线，不参与 mount-scale | 工具单 agent 外部整树 1c2g |

- **create-only 与 mdtest-io 两个缩放轴的所有系统都有同一 1c2g rank 预算**；NFS 的
  内核 NFS 线程不受该组限制，结论只能写客户端预算，不能写服务端/内核线程配额。
- B 组的 `mdtest-peak` 仍按单 agent 外部整树 1c2g；**mount-latency 与所有 density
  子组使用固定宿主机资源、不施加 per-agent 配额**（见 §8），不把缩放轴的预算外推。
- LakeBase 正式配方固定为：`--attr-cache=300s --entry-cache=300s
  --dir-entry-cache=300s --local-lock --keep-page-cache -o writeback_cache
  --drop-read-flush-barrier --drop-close-flush-barrier --dirty-max-ratio=50
  --buffer-size=1024M`，每 rank 独立 client 与独占 `--subdir`，不传空 `--bind`。
  close 不是持久化屏障；仅 `fsync`/`sync` 是持久化边界，不据此声明崩溃保证。
- N ≥ 3200 的 LakeBase 点使用上述配方加
  `--meta-url-query-string max_open_conns=4&max_idle_conns=4`。这是**部署变体**，
  只用于 3200 的容量边界，不是独立缓存因素，也不能和低 N 点拼成单因素消融。
- CephFS kernel `noshare`、`nas-fast`、scope-on/off、metadata-backend 不是本轮正式臂。
- **缓存消融暂缓**：LakeBase 默认 mid-independent 与 fast-drop 的旧 21 格只作为
  “多选项配置对照”的历史参考，不能标成单因素缓存消融或官方消融结论。

## 3. mount-scale 双轴

固定十台客户端；N=1 不进入正式网格。主网格：

| 轴 | N | 每 host rank 数 | repeats | native 参数 |
| --- | --- | --- | --- | --- |
| create-only | 100/200/500/640/1000/1280/2560/3200 | 10/20/50/64/100/128/256/320 | 1 | `--workload mdtest-create --create-file-size-bytes 4096 --stonewall-seconds 600` |
| mdtest-io | 同上 | 同上 | 3 | `--workload mdtest-io`（4 KiB，`-y` create 后同 job read） |

NAS v4 只跑 N=100/200/500/640/1000 这 5 个点；N>1000 不纳入，不用小 N 代替。
10 台客户端、同一放置和同一部署在全部点保持固定。命令模板：

```sh
# create-only 轴：4 KiB create+write+close，无逐文件 fsync，W=600，1 rep
uv run python -m agent_fs_bench run mount-scale --mode remote --target <profile> -- \
  --workload mdtest-create --create-file-size-bytes 4096 --stonewall-seconds 600 \
  --tasks <N> --repeats 1 --startup-timeout-s 180 --job-timeout-s 3600 \
  --fixture-timeout-s 7200

# mdtest-io 轴：4 KiB create+write（-y fsync）+ 同 job read，3 rep，无 -W
uv run python -m agent_fs_bench run mount-scale --mode remote --target <profile> -- \
  --workload mdtest-io --tasks <N> --repeats 3 --startup-timeout-s 180 \
  --job-timeout-s 3600 --fixture-timeout-s 7200
```

- create-only 轴是 **4 KiB create+write+close、无 `-y`**；它不是 0 B create，也不是
  mdtest-io 的带 fsync create。
- mdtest-io 是 4 KiB create+write **逐文件 `-y` fsync**，随后同一 job 读同一文件集；
  不重挂载、不丢缓存，读不称冷读。`-y` 不包含目录项 fsync。
- 密度实验使用上游原生 **0 B** create（不传 M1）；0 B 密度、4 KiB create-only 和
  `mdtest-io` 的 create+read **永不混算**、永不拼图。
- `--workload mount` 只计时站点 mount wrapper，不做文件 I/O，**不能使用
  `rank_cgroup`**；harness 直接拒绝预算 profile 的 timed mount。
- mdtest-io 的 `job_timeout_s` 可能被 NAS fsync 撞上；撞上即失败记账，不缩 N、
  不补零、不换参数重试。

## 4. 站点 wrapper、proof 与预算事件

可选 profile 字段：

```toml
rank_cgroup = "/sys/fs/cgroup/agent-fs-bench/{mount_hash}"
```

- `{mount_hash}` 是 `sha256(str(actual_mountpoint).encode()).hexdigest()[:20]`；
  profile 中必须且只能出现一次该占位符，展开后必须是 `/sys/fs/cgroup` 下的绝对路径。
  profile 的 argv 占位符仍是当前的 `{source}/{mountpoint}/{subdir}/{agent}`，不恢复
  旧 `{binds}` 或 `shared_mount`。
- 组由**站点 mount wrapper 创建**，不由 harness mkdir/rmdir/改限额。FUSE 臂在启动
  daemon 前创建组，记录已知 daemon PID，并读 `/proc/<daemon_pid>/cgroup` 核验该 PID
  确实在组内，同时核验 `cpu.max=100000 100000` 与 `memory.max=2147483648`；rank 随后
  加入同组。NAS 也创建组，但组内只有 rank，内核 NFS 线程不受限，不声称各系统内核/
  系统总预算对称。站点 wrapper 不得按进程/按 daemon 复制额度。
- proof 的 `members` 只记录观察到的 PID，不能单独证明 daemon 身份；daemon 身份由站点
  wrapper 记录 PID + `/proc/<pid>/cgroup` 核验承担，harness 只核验 rank proof。
- 正常 unmount wrapper 先执行真实卸载，再等待组内进程清空并删除组；仍有残留时
  返回非零。**harness 失败时保留挂载和数据，绝不自动卸载**；失败点由人工检查后再
  决定是否清理。
- 预算 rank 的 native 启动方式为
  `python -u -m agent_fs_bench.benchmarks.mount_scale_runner mdtest --cgroup <path> -- <mdtest> ...`。
  runner 将当前 rank 加入组，读取真实限制和成员，stdout 每 rank 恰好一行：

  ```text
  MDBENCH_PLACEMENT {"cgroup": "...", "pid": <int>, "rank": <int>, "cpu_max": "100000 100000", "memory_max": 2147483648, "members": [<int>, ...]}
  ```

  harness 对每个配置了 `rank_cgroup` 的 rank 校验：恰好一条 proof、rank 集合完整且
  逐 rank 匹配（proof 可按任意顺序到达）、cgroup 匹配、limits 正确、pid 在 members
  中；缺/重/错即失败。proof 留在本次
  run 的 diagnostics/native stdout，不随摘要上传。
- 生命周期回传的 `budgets` 列表按 client 顺序写入 `budget_events` 表，每项字段为
  `cgroup, cpu_nr_periods, cpu_nr_throttled, cpu_throttled_usec,
  memory_max_events, memory_oom, memory_oom_kill`。mount 快照在 wrapper 建立组之后，
  unmount 快照在 wrapper 删除组之前；记录 OOM/限流事件，不临时追加额度。
- 没有 `rank_cgroup` 的 profile 不建组、不校验 proof，也不把外部配额写成
  harness 能力。旧 quota 扩展已退役；本钩子只是“声明路径 + 校验/proof”的薄接口。

## 5. 完成、计时与失败

`phases.ops` 不从 mdtest 摘要文字猜：由 IOR CSV 的 rank `rate × time` 重建完成数。
IOR 4.0.0 的 rate/time 为 11 位有效数字，round 后必须落在明确整数容差内；模糊或
非整数即失败。每个 rank 的完成数必须等于 `files`，aggregate 必须等于 rank 之和，
且等于 `N × files`。依据是未修改的 IOR 4.0.0 源码
[`mdtest.c` 的 rate/time 输出](https://github.com/hpc/ior/blob/967a9f65109760db8a3ac14a7fdd007f337d2960/src/mdtest.c#L1118-L1141)
与
[完成项计数写出](https://github.com/hpc/ior/blob/967a9f65109760db8a3ac14a7fdd007f337d2960/src/mdtest.c#L1425-L1463)。

- `phases.elapsed_s` 仍是 native `all` 行时间，含 end barrier；公式不变：
  `ops_per_s = ops / elapsed_s`，`bandwidth = ops × file_size / elapsed_s`，
  rank mean 仍为 `sum(rank times)/ops × 1e6`，不是逐操作 p99。
- create-only 配了 W=600：native stonewall 会把慢 rank 追至最大已完成 rank，完整批次
  的 aggregate/`all` 行可能略大于 600 s（含 end barrier），**wall > W 本身不是截断**；
  若重建操作数 < N×1000，才是截断/失败，不进入正式吞吐，也不补零。
- 原始 `rank-performance.csv` 在解析/校验前保存到该 job 的 diagnostics，截断或失败时
  仍保留在实验机控制器，供审计；不进入回传文档。
- 全量验收：两阶段 `ops == N×1000`、每 rank `==1000`、`measured_mounts == N`、
  `remaining_mounts == 0`、预算臂 proof 数 `== N`、rank 行数 `== N`、无 mdtest I/O
  warning/截断、无挂载或数据残留。mdtest-io 还要求两个 phase 都通过，
  `bytes == N×1000×4096`。
- 失败不重试、不补零、不换参数、不缩 N；失败停其依赖点，其他组确认无残留才继续。
  partial/failed/not_run 只在执行报告和 `failures.csv` 记账。

## 6. 旧数据取用

| 数据 | 处置 |
| --- | --- |
| CephFS FUSE create 旧点 | **有条件候选**：只有原始 run 能给出真实 FUSE 挂载、rank/daemon 同组 1c2g 证据、相同 4 KiB create 语义与全量计数时才可复用；证据不足则按本方案重测 |
| LakeBase create | 新 fast-drop 臂的 **8 点全部新测**；旧 mid/conn4 结果不并入 |
| NAS v4 create | 旧网格没有 per-agent rank 预算，资源合同已改变，5 点降为 **legacy**；不能与新预算点混比，本轮需新测 5 点，缺一则 `not_run` |
| mdtest-io | 21 个点（FUSE 8 + LakeBase 8 + NAS 5）全部新测，每点 3 rep |
| 旧 conn4-cg 640/1280/2560/3200 | 仅参考；3200 正式点用 fast-drop + conn4 变体重测 |
| LakeBase mid-ind 旧 21 格 | 多选项配置历史参考；不是单因素缓存消融，不作为官方消融臂 |
| NAS-fast / kernel / scope / metadata-backend | 不进入本轮；原始观测保留，不删除、不改称合成 |

旧 `complete` 标签不是验收；实际挂载、预算证据、输入和完成边界都匹配才可复用。

## 7. 工具组（四系统含 ext4）

工具组使用**真实测量挂载**（不是 staging），单 agent、`tasks=1`，站点外部把 workload
及其 FUSE daemon/子进程包进同一个 1c2g 整树预算；NAS/ext4 无用户态 daemon 时不声称
daemon 配额。每系统：

| 入口 | 固定配置 | 主指标/验收 |
| --- | --- | --- |
| `npm-install` | npm、pnpm **分开**各 3 repeats，`tasks=1,flush=true`；pinned Node/manager、committed pnpm-bench lockfile、离线输入在目标外；pnpm 用 `copy` | `installs.csv:durable_s`，整树同步含在内；不混 `install_s` 或无 flush 旧点 |
| `kvtest` | 默认正式配置：2,000 ops、2,048 B、variance=0、DELETE、FULL、`multitrans=true`、5 repeats、`tasks=1` | `reads.csv:microseconds_per_read` 与 `repeats.csv:microseconds_per_write` 分开；核对提交/读写完成数与最后 integrity check |
| `dev-workflow` | 每系统 3 个 fresh-workspace run；固定完整 Linux mirror（记录 HEAD）、base、编译器与 `make -j8` | `workflow.csv:elapsed_ms`，五次 commit tree 校验与 `final-sync`；不用 mini 或未计 final sync 的旧值 |

```sh
uv run python - <<'PY'
from agent_fs_bench.benchmarks import npm_install
from agent_fs_bench.cli import main
npm_install.CONFIG.update(pm='npm', tasks=1, repeats=3, flush=True)  # pnpm 臂改 pm='pnpm'
raise SystemExit(main(['run', 'npm-install', '--mode', 'local', '--target', '<真实挂载>']))
PY
uv run python -m agent_fs_bench run kvtest --mode local --target <真实挂载>
uv run python - <<'PY'
from agent_fs_bench.benchmarks import dev_workflow
from agent_fs_bench.cli import main
dev_workflow.CONFIG.update(tasks=1, jobs=8, linux_url='<本地完整 mirror>')
raise SystemExit(main(['run', 'dev-workflow', '--mode', 'local', '--target', '<真实挂载>']))
PY
```

旧工具点只有在实际客户端、1c2g 证据、输入与完成边界都匹配时复用；否则新测。

## 8. 延迟、峰值与密度

- **mdtest-peak（实验 4）**：单 rank、私有目录，每 run 固定一个尺寸：
  `0`、`4096`、`1048576` B；每个尺寸 1 次 warmup + 5 个 measured repeat，沿用该
  尺寸默认 item 数。取 `ranks.csv:elapsed_s / items_per_task × 10^6` 的每 repeat
  平均 µs/op，报中位数/IQR；不把 phase/rank 均值当逐操作 p99。站点外部对
  mdtest 与该 agent 的真实 FUSE daemon 施加同一 1c2g 整树预算。
- **mount-latency（实验 6）**：Apsara 1:1 连接 profile、固定十台；N =
  `10/100/320/640/1280/2560/3200`，每点 `max(5, ceil(100/N))` 个 mount-only 批次，
  独立工作区、无文件 I/O。**不使用 per-agent 预算**（timed mount 拒绝
  `rank_cgroup`），**计时 wrapper 内禁止 stagger/sleep/retry**；保留 mount 就绪等待。
  先冻结站点授权的会话/内存/句柄/mount-table 上限，超限点 `not_run`。p50/p99 用
  `agents.csv` 的实际命令时长；p99 仅在有效样本 ≥100 时报告，并给命令/批次数。
  必须先实证 1 mount : 1 metadata 连接（读会话/连接，不由 conn4、bind 或 mount entry
  推断）。
- **density-next-mount**：单 ECS，先维持 M 个已验证 idle mount，再测 1 个 active
  mount；M=`0/64/192/384/768/1024/1536/1920`，100 repeats，无文件负载。timed mount
  同样不用 rank 预算；指标是新增 mount 命令 p50/p99。
- **density-next-create**：同一 ECS、同一 M 与 profile，`--tasks 1 --workload
  mdtest-create --idle-mounts M`，100 repeats；使用上游原生 **0 B** create（不传 M1、
  不传 W）；指标是该 client create 批次 p50/p99 与 `ops_per_s`。
- **所有 density 子组与 mount-latency 一样使用 `apsara-1conn` 的无 `rank_cgroup`
  profile，固定宿主机资源、不施加 per-agent 配额**；density create 也不加预算组。
- **density-all-create**：M=0，N=`1/8/32/64/128/256/512/1024/1536/1920` 全部 active，
  每点 `max(5, ceil(100/N))` repeats，0 B create；x=N，y 为每个 rank 的完整 create
  批次耗时 p50/p99，另报全批 create QPS；不得称逐文件尾延迟。
- 所有密度/延迟点先冻结可安全纳入的最高 M/N；旧 1920 不是容量保证。超限不缩规模
  替换，失败后不再继续堆 mount。M 个 idle 不发 MPI rank、不计入活跃样本；核对挂载
  与释放、客户端内存/CPU 与实际连接数。

```sh
uv run python - <<'PY'
from agent_fs_bench.benchmarks import mdtest_peak
from agent_fs_bench.cli import main
mdtest_peak.CONFIG.update(tasks=1, file_size_bytes=<0|4096|1048576>,
                          warmup_repeats=1, repeats=5)
raise SystemExit(main(['run', 'mdtest-peak', '--mode', 'local', '--dirs', '<真实挂载>']))
PY
uv run python -m agent_fs_bench run mount-scale --mode remote --target apsara-1conn -- \
  --workload mount --tasks <N> --repeats <max(5,ceil(100/N))>
uv run python -m agent_fs_bench run mount-scale --mode local --target apsara-1conn -- \
  --workload mount --tasks 1 --idle-mounts <M> --repeats 100
uv run python -m agent_fs_bench run mount-scale --mode local --target apsara-1conn -- \
  --workload mdtest-create --tasks 1 --idle-mounts <M> --repeats 100
uv run python -m agent_fs_bench run mount-scale --mode local --target apsara-1conn -- \
  --workload mdtest-create --tasks <N> --repeats <max(5,ceil(100/N))>
```

## 9. Agent replay（378 captures / 4,536 replays）

冻结 SWE-bench Pro V2 public 100（按公开 ID 排序后
`random.Random(20261110).sample`）与 Terminal-Bench 2.0 全部 89；两套 harness 同一
cohort。每任务准备 Harbor 单步定义、初态与镜像 digest；**378 个 capture**
（`189 任务 × 2 harness`）与 **4,536 次 replay**（378 × 3 repeats × 4 臂）为计划数，
准备失败保留在台账。Codex `0.169.0`、OpenClaw `2026.11.3` × Qwen 3.8 Max
（Responses / Chat Completions），在 ext4 录制。

多 host 按冻结 cases 顺序 round-robin 分片，每 host 同时 1 项；`collect` 汇总成功
cassette，`distribute` 复制到各 replay host。四臂为 ext4 / CephFS FUSE / LakeBase
fast-drop / NAS v4；每臂 `tasks=1,repeats=3`。只有同一 benchmark × harness 的四臂
全部计划 repeats 均为 `ok` 且 ext4 中位 tool time > 0 才入
`median(tool_s)/median(ext4 tool_s)`；覆盖率分母仍是 100/89，补记 setup_failure、
capture_missing、divergence、timeout、error。tool_s 是主指标，不含 harbor_s、模型
推理、verifier 或 setup。缓存消融暂缓，scope-on/off 不纳入；不按 reward 筛任务。
原始 cassette、响应、日志不出实验机。

FUSE 任务预算按 Round 03 的 Compose `cgroup_parent` 规则执行：父组不直接跑进程，
FUSE daemon 放 `fuse` 子组，workload 与 daemon 同属 1c2g 父组；controller/proxy
在预算外。详细采集、分片、验证与 `replay_report` 合同**采用 Round 03 §1 原文不变**。

## 10. Clone

先核验旧 `clone-05..13`：轴、文件内容/树形、预读、真实客户端/设备、
`cp -r --reflink=never`、目的树+父目录同步、原生 API 完成边界、双向写隔离。只有
同口径、同真实客户端形态的点才复用；若旧轴无法建立一致 cohort，按**预先计划的新
形状**补测，不把旧点改标签、不用小形状冒充：

- 1,024 文件 × 4 KiB / 64 KiB / 1 MiB / 4 MiB；
- 4 KiB × 100 / 1,000 / 10,000 / 100,000 文件；
- 每形状 5 repeats；单客户端，ext4 保留；Apsara `target_copiers=polarfs`（站点
  `ALIBABA_CLOUD_*` 运行时注入），基线 `cp-r`；
- 主指标 `requests.csv:latency_s`；时间不含源准备，不称快照/恢复。本组不继承
  mount-scale 的 1c2g；metadata-backend 不纳入，详细合同**采用 Round 03 §9 原文**。

## 11. 不纳入与暂缓

- 缓存消融（含 LakeBase 21 格读侧/写侧单因素归因）暂缓；mid-ind 与 fast-drop 只能
  作为多选项配置对照，不是单因素缓存效应。
- scope-on/off、metadata-backend、CephFS kernel 正式臂、`nas-fast` 不纳入；旧结果
  和旧观测保留，不删除、不改称合成。
- Intro 旧 cohort 的网格冲突以用户报告为事实，**不重跑、不再核验、不建立替代
  cohort**。
- 不执行 crash injection、异常退出持久性验收、自动 failed-mount 清理或生产化加固；
  正常卸载可见性不外推为崩溃保证。

## 12. 执行顺序与冻结

在看任何正式结果前冻结以下顺序、N/M 列表、repeats、profile、wrapper、工具版本和
站点上限；之后不因结果调整。

1. 合并精确源码 commit，冻结本文为 `STAGE/plan.md`，记录 profile/wrapper SHA。
2. 完成站点制备与真实挂载/readiness/cgroup smoke；smoke 不入摘要。
3. 审计旧数据证据，冻结复用与 `not_run` 决定。
4. create-only 轴：CephFS FUSE（视证据）、LakeBase 8 点、NAS 5 点。
5. mdtest-io 轴：同一网格 21 点 × 3 rep；3200 LakeBase 用 conn4 变体。
6. 工具组：npm、pnpm、kvtest、dev-workflow。
7. 峰值/延迟/密度：先冻结站点上限，再跑 peak、mount-latency、density。
8. Agent replay 与 clone：先准备/核验，再跑计划点。
9. 生成 summary、failures、execution.md，回传一份 reviewed Markdown。

## 13. 汇总与来源

- 原始逐 rank/repeat/任务表、日志、cassette、配置和凭据全部留在实验机；不进入回传。
  需要重算时在实验机重算。
- `summary/<group>.csv` 固定表头（顺序不可变）：
  `series,x,metric,median,q1,q3,n`；同名 JSON 固定键：
  `xlabel,ylabel,unit,logx,logy,series_order,source_runs,formula`。组名前缀使用
  `scale-`、`density-`、`op-latency-`、`tools-`、`replay-`、`clone-`。分位数继续用
  `(n−1)p` 线性插值，p=.25/.5/.75；n=1 时三值相同；p99 只在有效样本 ≥100 时报告。
- mount/density 的 p50/p99 是同一配置内的一个汇总图点：每点 `n=1` 且
  `q1=median=q3=该分位数`；分母是汇总后的实际命令数或批次数，不把同批关联的 rank
  当独立重复。原始分布留在实验机。
- `execution.md` 逐组/点/rep 对账 `planned = valid + failed + not_run + excluded`；
  `failures.csv` 只记计数；失败/超时/缺失不进入 n、不补零。
- 唯一回传是一份经人工审核的完整 Markdown：

```sh
python3 -m agent_fs_bench.exchange export --batch round-04 \
  --root "$AFB_STAGE" --output "$AFB_OUT/round-04.md" --reviewed
```

  不在传输时截断、抽样或手改；实验机不画论文图。主机、端点、路径、实例 ID 和凭证
  只以稳定别名出现。

## 14. 与 Round 03 的关系

- Round 03 文件**不改**；其 §1（replay）、§2（工具）、§4（峰值）、§9（clone）的
  详细 fixture/采集/回放/计时合同在本轮**明确整段采用**，仅以本文的预算、NAS 5 点、
  3200 变体和排除项覆盖冲突处。
- Round 03 §3（缓存消融）、§10（scope）不采用；metadata-backend 不采用。Round 03
  §5–8 的旧网格与资源合同（所有系统均无 per-agent 预算、固定宿主机资源）由本文
  §2–4、§8 的预算和网格取代；旧 NAS 点因此只是 legacy 资源合同，不与新点混比。
- Round 03 的旧结果、旧 checkout 和旧失败账目保持原样；旧标签不重新解释为
  Round 04 有效点。
