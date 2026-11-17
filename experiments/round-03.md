# Round 03｜Apsara 评估执行方案

本轮以实验 1–10 和 Intro 为准。先保存本文原文为 `STAGE/plan.md`，从交付的 GitHub 固定链接取 **40 位 commit SHA**，在 controller 和参与客户端的新 checkout 均检出该 SHA。保留旧 checkout、`results/`、`packages/` 和原始日志。先核验可复用的旧实测，再按本矩阵补测；不补零、不重试、不换参数救失败点。一个组失败后停其依赖点；其他组只有确认无残留干扰才继续。

## 固定环境

- 记录源代码、工具、镜像、输入、挂载 profile 和站点 wrapper 的版本/哈希。全节点 `uv sync --locked`，同版 Open MPI 4.1.x 和匹配的 mpi4py；涉及 mdtest 时使用同一未改的 IOR 4.0.0 二进制。每种新路径先做最小真实挂载 smoke，smoke 不进正式摘要。所有正式轴、样本和执行顺序在看结果前冻结。
- 实验 1、2、4 在同一 ECS：Apsara (LakeBase/PFS) FUSE、CephFS FUSE、NAS NFSv4、实际 ext4。Apsara 主臂用 fast 独占配置：attr/entry/dir-entry TTL 300 s、local-lock、keep-page-cache、drop-read-flush-barrier、1 GiB buffer；默认本地及 group cache 开启。实验 3 只比较 Apsara 且只切换指定因素。核实部署版本、有效参数与独占契约；带 `fsync` 的比较要求两臂在同一持久化边界完成（见实验 3）。ext4 不得由 XFS 或未知系统盘代替。
- 实验 1–4 的 **每个 agent 总预算为 1 CPU / 2 GiB**，不是每进程各 1c2g。站点用同一个 cgroup v2 预算子树容纳 workload/其子进程与该 agent 的 FUSE daemon（`cpu.max=100000 100000`，`memory.max=2147483648`）；测量前以 `/proc/<pid>/cgroup` 和实际文件证明并保留脱敏核对记录。NFS/ext4 没有用户态 daemon，不声称 quota 限制了 NFS 内核服务线程。实验 3 的多个角色各有自己的预算，group cache 服务本身固定在预算外。记录 OOM/限流，不临时加额度。
- Harbor 的 Docker 容器**不继承**启动 CLI 的 cgroup。回放固定一个预算父组，FUSE daemon 放在其 `fuse` 子组；录制前把同一父组写入冻结任务的 `environment/docker-compose.yaml`（`services.main.cgroup_parent`，其余参与 workload 的容器同样配置；按 Docker cgroup driver 填值）。父组本身不直接运行进程；核对 task/Compose 没有更小的独立 CPU/内存限额。proxy/controller 在预算外。保留解析后的 Compose 指纹，并通过 Docker PID 在 cgroup v2 的实际成员和限制文件核验容器与 FUSE 同属父组；不能只看 `docker inspect` 配置。capture 和 replay 用同一任务副本、父组、镜像 digest 和容器路径。
- 小工具、Clone、Replay 的 `--target` 是真实测量挂载；`mount-scale` 的 `--target` 是各节点同路径的本地 staging，`admin_root` 是另一个已挂载的同卷管理视图。分开配目录。Apsara 必须每 agent 一个真实客户端；CephFS kernel 扩展用 `noshare`；NAS 记录实际 mount entry 和连接/superblock 共享行为，不把 mount 数当连接数。任何后端的服务端额度未知就写 `unknown`。
- 实验 6/8 固定 Apsara **一 mount 一 metadata 连接**，沿用站点 mount/unmount wrapper，核验实际连接上限和对应的会话/连接，不把旧 `conn4`、bind 或单纯一条 mount entry 当 1:1 证据。不自行调整托管服务或清理历史会话。禁用计时 wrapper 里的人工 stagger、sleep 或重试；不能删掉 wrapper 中确认 mount 已就绪的等待。诊断不用含凭证的完整 argv。

## 1–4｜典型单 Agent 工作负载

### 1. Agent replay：主指标 tool wall time，不称为端到端墙钟时间

冻结 SWE-bench Pro V2 public 的 100 个均匀无放回样本（按公开 task ID 排序，用 Python `random.Random(20261110).sample`）和 Terminal-Bench 2.0 的全部 89 个，两套 harness 用同一 cohort。记录 release、采样实现和全部 378 个计划 task/harness。为每项准备 Harbor 单步任务定义、初始快照和镜像 digest；准备失败保留在计划台账，不造空 task 目录、不抽替代任务。可准备的每项都写入同一份 `cases`，没有 cassette 的项仍列出。使用 [回放入口](../docs/bench-agent-replay.md) 在 ext4 为 Codex `0.169.0`、OpenClaw `2026.11.3` 分别录制 Qwen 3.8 Max（Responses / Chat Completions）。每臂每任务 `tasks=1,repeats=3`，原计划 378 份 capture、4,536 次 replay。先用一公开任务验证实际挂载、预算与工具可比，再运行全计划；不以 smoke 代替。每次恢复相同内容、mode、link 和 `.git`，确认实际访问被测 bind 路径而非 image overlay。不可比仍计缺失/发散，不用 reward 筛选。

四系统跑完同一份 `cases` 后运行 `python -m agent_fs_bench.replay_report <四个完整 run 目录> --reference ext4`。按 benchmark × harness，同一任务四臂所有计划 repeats 均 `ok` 且 ext4 中位 tool time > 0，才取各系统的 `median(tool_s)/median(ext4 tool_s)`。汇总任务比值的中位数和 IQR；覆盖率分母用最初的 100 或 89，补入未进入 `cases` 的 `setup_failure`，对账 capture、发散、错误、超时和排除。不要回传逐任务比值。`harbor_s`、模型推理、verifier 和 setup 不进入主指标。

### 2. 工具：有明确完成边界的安装、SQLite、开发工作流

| 入口 | 单任务矩阵 | 主指标和验收 |
| --- | --- | --- |
| `npm-install` | npm / pnpm 分开，各 3 repeats；固定官方 pnpm benchmark fixture、pinned Node/manager，离线输入在目标外，pnpm `copy` | `installs.csv:durable_s`；同一软件树，整个项目同步包含在内，不混 `install_s` 或 no-fsync |
| `kvtest` | 5 repeats；2,000 个操作、2,048 B、variance=0、DELETE / FULL、`multitrans=true` | 读、写各取 `reads.csv:microseconds_per_read` / `repeats.csv:microseconds_per_write`；核对提交/读写完成数及最后 integrity check |
| `dev-workflow` | 每系统 3 个 fresh-workspace run；固定本地完整 Linux mirror、fixture、base、编译器与 `make -j8` | `workflow.csv:elapsed_ms`，五次 commit tree 校验与 `final-sync`；不使用 mini 或未计最终同步的旧值 |

旧点仅在实际 FS 客户端、1c2g 证据、输入与完成边界均匹配时复用。`complete` 标签不是验收；缺预算/挂载证据的旧点单列，不混入本组。工具 `CONFIG` 支持在站点脚本中先按矩阵设置、再调用 `agent_fs_bench.cli.main([...])`，无需改 tracked source。

例如在已核对的预算组内（站点 `TARGET` 已配置）：

```sh
uv run python - "$TARGET" <<'PY'
import sys
from agent_fs_bench.benchmarks import npm_install
from agent_fs_bench.cli import main

npm_install.CONFIG.update(pm='npm', tasks=1, repeats=3, flush=True)
raise SystemExit(main(['run', 'npm-install', '--mode', 'local', '--target', sys.argv[1]]))
PY
```

捕获主机对象和负载进程的实际 cgroup；不要把从外部启动的 FUSE daemon 误算进 CLI 的继承关系。一个正式实验点从固定配置生成，不在现场修改已跟踪文件。

### 3. 优化消融：同一组 npm 工作区，两条机制对比

**写侧。** 只对比 `exclusive` 与 `exclusive-drop`；两臂均为一个 agent、一个真实独占客户端，固定 metadata TTL、local-lock、page cache、write buffer、group cache、部署和离线输入，**只改变 `drop-read-flush-barrier`**。复用实验 2 的 `npm-install` (`pm=npm,tasks=1,repeats=3,flush=true`)；主指标 `durable_s`，分解保留 `install_s` 和 `flush_s`。先核对该版 flag 的真实语义，不按名称假设其放松了所有同步。必须证明两臂仍在同一个最终确认边界完成：取得 `fsync`/目录发布契约，并在授权的独立 scratch 上确认已同步的内容与目录项经客户端异常退出后的新客户端可见；正常卸载会自行写回，不能单用正常卸载可见性代替。若该部署不能确认边界，不报持久完成对比。

**读侧。** producer 用上述同一 fixture 的真实 `npm ci`（`npm-install` 设置 `pm=npm,tasks=1,repeats=1,flush=true,keep_node_modules=true`），发布同一份已同步的 `node_modules`；消费者用 `tools/npm_workspace_pack.py` 执行一次固定包列表的 `npm pack --offline --ignore-scripts`。这测 npm 工具完成延迟（含遍历和压缩），不是裸读带宽。所有 arm 固定 Node/npm、列表、输入内容、client 独立进程、同一 ECS，使用不同真实 mount，不用 bind 冒充冷客户端。打包输出、npm cache 和日志在另一文件系统。计时前不对消费者工作区做全树读取/哈希，不要求清 provider cache；输出档案摘要核对逻辑输入一致。

| arm | 客户端和 group 读缓存 |
| --- | --- |
| `default` | 本地缓存 + group cache 保留；容量扫只改预先指定的本地额度 |
| `local-off` | `--cache-size=0`，group 路径保持有效 |
| `local-group-off` | 本地额度同为 0；站点验证 `--group-try-one=false --cache-group=<专用空组>` 确实绕开 group 并读 OSS |

先通过 client-local/group/OSS 读字节或请求计数、mount/cache-dir 身份验证三个 arm。表中开关施加在**被计时消费者**，producer、cache owner 的预热及 group 服务配置在三臂保持一致；共享容量两档是单独冻结的容量轴。用 `local-off` 对比 `local-group-off` 解释 group 贡献，用 `default` 对比 `local-off` 解释消费者本地缓存贡献。慢、不可配置或失败都按原 arm 记账；命中路径不可验证则不归因于 P2P。

- **共享容量：** 4 个独立 cache owner 各预读互不重叠的已安装包集合；第 5 个新客户端一次打包其并集。冻结两档每 owner 本地额度：低档容不下各自子集，高档容得下各自子集但小于并集；计时消费者额度不随 owner 变化。以实际 cache 占用/eviction 和 group 请求验证容量确实叠加。两条 off 对照在同一固定高档 owner 条件下各测一次配置，不造零额度的重复容量点。
- **一写多读：** 一个 producer 发布同一份安装树，4 个新只读消费者通过允许的只读挂载访问该版本；若独占契约不允许共存，先释放 writer。把消费者命令放到一个本地启动屏障后执行，不串行预热；主样本取同批最慢消费者的 `elapsed_s`，不是 4 个独立 repeats。核对计划/完成消费者数和相同内容摘要。
- **Handoff：** 前一个 agent 发布工作区并在其挂载上预读固定包，再释放所有权；下一 agent 新 mount，计首次 `pack`。验证 group 在旧客户端离开后实际保有哪些数据，不把发布、卸载或新挂载算进工具延迟；若无 group 保留，报告当轮未命中，不通过双挂独占目录制造命中。

每场景每配置 3 个独立 repeats；每轮按固定策略建立新的缓存初态，使用内容相同的发布版本，验证轮次间的 cache 隔离或准备后相同的有效状态。参考包列表 `@babel/core @mui/material chart.js eslint typescript webpack zod`；容量分区、额度和完整包列表用非计时 fixture 核对后冻结，不按耗时挑选。命令 `python3 tools/npm_workspace_pack.py --workspace "$PROJECT" --scratch "$LOCAL_SCRATCH" --packages <已冻结包列表>`，取 `elapsed_s`，核对 `npm_version`、`package_count`、`packed_bytes`、`tarball_digest_sha256`。不要清生产 group cache，也不把上一轮残留称为本轮 P2P 命中。

### 4. 文件操作延迟

复用 `mdtest-peak`，单 rank、私有目录，`file_size_bytes=0/4096/1048576` 三次独立配置，每点 1 次 warmup + 5 个 measured repeat，沿用每尺寸默认 item 数。0 B 取 `file-create`（create+close，无数据/fsync）与 `file-stat`；4 KiB、1 MiB 分别取 `file-create`（create+write+`-y` 逐文件 fsync+close）与 `file-read`（open+全读+close，跟随创建、不吹冷缓存）。`ranks.csv:elapsed_s / items_per_task × 10^6` 是一个 repeat 的平均 µs/op；汇报中位数/IQR，**不从 phase/rank 均值推逐操作 p99**。空文件 open/close 不冒充读数据。

## Intro、5–8｜扩展性

| 组 | 固定条件与实验轴 | 结果 |
| --- | --- | --- |
| `intro-create` | 优先核验旧 `grp-ms-expansion` 的 `N=320/448/576/640/704` 多节点三系统 create cohort（`run-ref-ms-023..038`；含失败账目）。核对旧 Apsara 的实际配置，不标为 fast。只在同一放置、同一每 agent 文件数/大小、命令、完成边界和有效全批数可核实的点比较；缺失标出。若根本无法建立旧的共同 cohort，则新建独立三系统 `mdtest-create` 0 B cohort，按实验 5 的三台池和共同 N 冻结，绝不拼旧 4 KiB create 点 | x=真实 mount 数；y=mdtest 文件创建完成数 / create phase 时间。只描述实际趋势、饱和、失败，不预设所有 NAS/CephFS 部署的上限 |
| `scale-read` / `scale-create-write` | 原生 `mdtest-io`；固定同一三台客户端池，Apsara fast、NAS v4、CephFS kernel `noshare`；N=1/24/96/384/768，每点 3 repeats；N=1 仅池中第一台。768 常驻+测试 mount 预算未通过，三方该点都 `not_run`。Apsara/CephFS 另在固定同十台测 3,200（10/2,560 门禁，3,200 各 3 次），单独 `scale-capacity-*`，不拼接 | 每 rank 单独真实挂载，1,000 × 4 KiB；create+write 含逐文件 `fsync` (`-y`)，read 同一 job 跟随创建，不称冷读；两阶段分别用全批 `ops_per_s`。不混 Round 02 的 POSIX 批末 syncfs/重挂载或 create-only |
| `mount-scale-latency` | **Apsara only**，固定十台；N=10/100/320/640/1,280/2,560/3,200。每点 `max(5, ceil(100/N))` 个 mount-only 批次，无 workload I/O，独立工作区 | `agents.csv` 内每个 active mount 从 MPI rank 调起 wrapper 到 ready return；排除启动、队列触发、验证、卸载；p50/p99 分两条曲线，卸载延迟单列诊断 |
| `metadata-backend` | **只接入独立旧实测，不安排重跑**。在现场找到实验 7 的数据、原始采集身份和真实自变量，记录请求是否走真实 metadata 后端、部署档位、客户端预算/放置及并发、mdtest 配置与计数 | 回传原曲线的文件创建 QPS、计划/完成数、服务端观测（若有）。固定部署只说明其承载，不写成横向扩容收益；无独立数据或证据不借实验 5 代替 |
| `density-next-mount` | Apsara、单 ECS。先维持 M 个经验证的真实空闲 mount，再逐次测 **1** 个 active mount；100 repeats，不发负载 | x=M（不含当前探针）；y=新增 mount 命令 p50/p99 |
| `density-next-create` | 同一 ECS、同一 M、同一 1:1 profile；固定 `--tasks 1 --workload mdtest-create --idle-mounts M`，100 repeats | x=M；y=这一个 client 的 create 批次 p50/p99；附 `ops_per_s` |
| `density-all-create` | 同一 ECS；`--workload mdtest-create`，M=0，N=1/8/32/64/128/256/512/1,024/1,536/1,920 个 mount 全部活跃，各 `max(5, ceil(100/N))` repeats | x=N；y=每个 rank 的完整 create 批次耗时 p50/p99；另报全批 create QPS。不能把它叫成逐文件尾延迟 |

空闲轴 M=0/64/192/384/768/1,024/1,536/1,920。看结果前记录站点授权的会话/内存额度、admin 余量、进程/文件句柄和 mount-table 限制，冻结可安全纳入的最高 M/N；旧 1,920 不是容量保证。超过上限的点 `not_run`，不以小规模成功替换；失败后不继续堆积 mount。M 个 idle mount 不发 MPI rank、不计入活跃样本；验证挂载与释放。记录客户端内存、CPU、可核实的 metadata 连接数，不从 buffer 配置推 RSS。

典型命令（按冻结矩阵替换目标和规模）：

```sh
uv run python -m agent_fs_bench run mount-scale --mode remote --target apsara-fast -- \
  --workload mdtest-io --tasks 768 --repeats 3 --startup-timeout-s 180 \
  --job-timeout-s 3600 --fixture-timeout-s 7200
uv run python -m agent_fs_bench run mount-scale --mode remote --target apsara-1conn -- \
  --workload mount --tasks 320 --repeats 5
uv run python -m agent_fs_bench run mount-scale --mode local --target apsara-1conn -- \
  --workload mount --tasks 1 --idle-mounts 64 --repeats 100
uv run python -m agent_fs_bench run mount-scale --mode local --target apsara-1conn -- \
  --workload mdtest-create --tasks 1 --idle-mounts 64 --repeats 100
```

`mdtest-create` 只计 0 B create+close，独立删除/清理在计时外。验收每阶段精确 N×1,000 文件、正确的 native 模式和 rank 数、无 mdtest I/O warnings 或截断，无 mount/工作区残留。Exp 5 每个数据阶段的逻辑字节数为 `N×1,000×4,096`；字节数不等于物理存储 I/O。各组报实际 warm/cache 状态，不把结束同步或写可见性外推为崩溃证明。

## 9–10｜Clone 和作用域

**9. Clone。** 单个客户端，保留 ext4。本组不继承实验 1–4 的 1c2g 要求；固定资源并报告。先核验 `run-ref-clone-05..13` 及原始包的轴、文件内容/树形、预读、真实客户端/设备、`cp -r --reflink=never`、目的树+父目录同步、实际原生 API 完成边界、双向写隔离。优先提取可比点（CephFS 客户端形态按已核验 cohort 记录，kernel/FUSE 不混）。同口径缺点才补；旧轴无法建立一致 cohort 时，使用两条新轴：1,024 文件 × 4 KiB/64 KiB/1 MiB/4 MiB；4 KiB × 100/1,000/10,000/100,000 文件。新点每形状 5 次，`clone` 的 Apsara 设 `target_copiers` 为 `polarfs`，基线 `cp-r`；用 `requests.csv:latency_s`，`--smoke` 验输入可访问与写隔离。时间不含源准备，不把 materialized copy 当快照或恢复。

**10. Scope isolation overhead。** 测试机提供两个已经挂好的实际 admin 后端 `scope-on` / `scope-off`。不由 benchmark 编排租户、改变拓扑或修改服务端。取同一 ECS 单 rank 的原生 `mdtest-peak` 0 B 私有文件（1,000 文件、1 warmup、5 repeats）；主指标 create 和 stat 的每 repeat 平均 µs/op，按同一顺序配对报告绝对值及 `on/off - 1`。保持 client options、cache、资源和输入一致，记录这两个后端的实际差异。如果 admin 路径绕过隔离校验或仍有其他后端差异，只能称为两个部署的差值，不能归因于隔离。性能 run 不作为拒绝未授权访问的证明；若另有真实受限凭证，可带一个跨 scope 拒绝 probe（与计时无关）。

## 汇总和回传

- 吞吐每个 repeat 一个全批样本；工具、Clone、mdtest 平均延迟按相同计时边界取样；replay 仅每个已纳入 task 一个配对比值。分位数统一用排序后位置 `(n−1)p` 线性插值，p=.25/.5/.75；独立样本 n=1 时三值相同。mount/密度的 p50/p99 是**同一配置内的真实命令或 rank 批次分布**：汇总全部已验证批次，只在有效样本至少 100 时报告 p99；必须同时回传实际命令/批次数。每一项分布分位数是一个图点统计量（`n=1,q1=median=q3=该分位数`，不作 IQR），在 `formula` 标明 p=.50/.99 和汇总分母。不把各 host p99 平均或把同批关联的客户端当独立重复。
- 用 [交换格式](../docs/markdown-exchange.md) 的 `summary/<group>.csv` / 同名 JSON：一个已声明单位的指标一组。组名前缀用 `replay-`、`tools-`、`ablation-`、`op-latency-`、上表组名、`clone-` 或 `scope-`；后缀区分指标、bench/harness、尺寸轴或分位数。来源全写可对账的原 run 别名及指纹。旧诊断不混新值；有可比旧点即回传实际摘要，不只给 package 路径。
- 在 [执行报告](execution-report.template.md) 逐组/系列/点对账 `planned = valid + failed + not_run + excluded`，标明单位（批次、命令、任务分别计算）、首次失败阶段、观测到的错误，以及是否有其他有效观测。`summary/failures.csv` 只记计数；全批无有效数值时允许只有失败账目。不因失败/超时补零、不重采“容易成功”的任务。只回传核实的汇总、公式、覆盖、失败及来源，不回传原始逐文件、逐 rank、逐 repeat、逐任务表，也不传请求、cassette、日志、配置原文或真实别名映射。
- 未采用的实验按 [归档表](archive.md) 只保留历史材料，不跑、不进入新摘要。测试机不画论文图。

```sh
python3 -m agent_fs_bench.exchange export --batch round-03 \
  --root "$AFB_STAGE" --output "$AFB_OUT/round-03.md" --reviewed
```

唯一回传：一份完整的 `round-03.md` 源文件；不在传输时截断、抽样或手改。
