# Round 02｜Apsara 评估：核验旧实测，补齐论文口径

这是 Round 02 的可执行测试机方案。旧仓、`results/` 和 `packages/` 只读保留；新实验使用独立 checkout、独立数据目录。不要修改服务端、清理历史会话、自动重试、换配置救失败样本、填零或以小规模成功代替计划点。预检不过就不启动对应组；运行失败停该组后续依赖点，保留诊断并记账。只有确认互不干扰的其他组才能继续。

## 1. 固定代码与现场

从 `l05k/agent-fs-bench` 的 `bench/round-02-validation` 获取方案。在 controller 的独立 checkout 切换到固定代码前，先把本文原样保存为 `local/round-02-stage/plan.md`。controller 与客户端均固定代码 commit：

```text
9a10d2809c2a9389ee75ea63b25e513c2d54643f
```

所有机器用各自的 `uv sync --locked` 环境和同版 Open MPI 4.1.x，核对 `git rev-parse HEAD`、源码指纹、非交互 SSH 下的 `orted` 和 `mpi4py` 实际链接版本；不要修改测量逻辑。只在新 checkout 写 gitignored `local/`。工具的实验参数用模块 `CONFIG` 的已有约定；可以在 controller 进程内赋值后调用 `agent_fs_bench.cli.main`，无需改 tracked source。记录实际参数和工具指纹，不回传站点配置原文。

- **Apsara = `apsara-fast`**：一 agent 一真实 LakeBase/PFS 客户端、一个私有子树。使用旧 `fast-independent` 的 fast 配方并在测试前核实：attr/entry/dir-entry TTL 300 s、`local-lock`、`keep-page-cache`、`drop-read-flush-barrier`、1 GiB buffer。确认仅对独占子树使用，冻结实际 flag、部署和一致性/持久化契约；验证不通过就记账，不改用 `fast-nodrop`、`mid`、`conn4` 或 `big`。`mid`/`conn4` 只作参考或另行消融。共享 admin mount 加 bind 不是独立客户端。
- **CephFS 主臂 = `cephfs-kernel`**：固定同一部署和内核客户端，确认 `noshare`。CephFS FUSE 另用于资源消融。**NAS = `nas-v4`**：记录真实 NFS mount 选项、是否共用 superblock/cache/连接。NAS 每 agent 有私有数据和单独 mount entry，不据此断言独立内核客户端或冷缓存。**ext4** 只用于工具、Clone、回放，先核对实际文件系统和盘，不把 `sysdisk` 或 XFS 改名当 ext4。
- `mount-scale` 的 targets 指向每个节点同路径的**本地 staging**，持久 `admin_root` 是另一目录；工具、Clone 和回放的 targets 则是**实际测量挂载**。分开配置。确认 staging、admin 卷身份、mount wrapper、CPU/内存、进程/文件句柄额度、网络和存储空间；分别冻结 3 台及 10 台同规格客户端的别名和顺序，controller 不跑 rank。服务端配额查不到便写 `unknown`。
- 预先为大批量 PFS 挂载启用已验证的站点 stagger 和 300 s 内部 mount wait，避免同时建连；固定做法、脱敏 wrapper 指纹和 `dispatch` 范围，stagger 不计入 I/O。不要用数据库连接数、OSS QPS 或限流成因的推断替代证据。预检现有会话和当轮容量只用授权的只读观测；不要自行清理历史会话。日志留本机，不用完整 argv 诊断带凭证进程，不回传环境变量值。

## 2. 先提取旧实测，不全量重跑

以源 run 和实际产物核对，不以旧索引的 `complete` / `paper` 标签判定。对重复粘贴的行按 `run_ref` / 图点键逐项核对：完全相同才去重；冲突回原数据，不取最后一行。旧包的指标定义、形状、失败和来源用实际文件/日志在测试机核验；没有证据写原因。旧 `n=2` 的 min/max 是全距，不是四分位数，按第 5 节从本地观察值重算。旧命令日志不出站。

| 旧实测 | 本轮动作 |
| --- | --- |
| 小文件、mdtest（含 K=3、cgroup、3200） | 保留可核实的 create-only 摘要作诊断；不与新读/带同步写混用。部分完成、不同容量、不同 cgroup 不合并。核对 `run-ref-ms-078` 的真实挂载形态，不能仅凭 `fast` 标签称独立挂载。 |
| npm/pnpm | 优先核验 `c1, flush=true` 的同口径点；回传 `durable_s` 对应值而非 `install_s`。要求相同 lockfile/版本、目标外预热、实际 offline、pnpm copy、计时包含整树同步。`c8/c16` 和 no-fsync 单列诊断；本轮不重跑那两条扩展曲线，不把缺行记为零。 |
| SQLite `speedtest1`、开发工作流 | 回传旧组摘要作诊断。`speedtest1 TOTAL` 不替代 `kvtest`；旧开发工作流若未计最后同步，则不进入持久完成时间。按 §4 补正式可用组。 |
| Clone 9-case 与 100k | 从旧包提取，不默认重跑。按实际 `(files, file_size_bytes)` 和 copier 拆出两条扫描：固定文件数扫单文件大小、固定 4 KiB 扫文件数。先按旧 fixture 配置冻结轴值和计划点，不按性能选点；100k 只在尺寸匹配时并入。核验源逻辑内容/目录分布、预读策略、同一请求计时边界、原生 API 返回即完成及写隔离、基线 `--reflink=never` + 目标树和父目录同步。旧 wall 字段只有在边界相同时才能用。原生 Clone 对比 materialized copy，不是快照恢复。 |

同口径、同输入且来源可核实的旧点保留。`n=1` 不画误差线；缺少可比点才补。若计时、持久性、缓存、fixture 或实现变更影响可比性，旧组留作诊断，新测用统一边界；不要跨组补画成同一条曲线。区别 `absent`、`failed`、`not_comparable`，记录为何没有旧点及本轮执行与否。

## 3. 正式新测｜小文件

`K=1`，每 agent 每阶段 1,000 个 4 KiB 文件，各自私有数据。先分别在三系统上做 N=3、一主机一 rank 的真实 mount smoke；校验 mount/路径身份、所有 rank 起齐、两阶段计数、同步边界和清理。局部目录 smoke 只证实 MPI/程序，不替代真实 mount smoke。

| 组 | 固定资源与系列 | N | 每点 |
| --- | --- | --- | --- |
| `smallfile-main` | 同一份 3 台宿主机池；Apsara fast、NAS v4、CephFS kernel；无 per-agent 限额 | 1、24、96、384、768 | 每点 3 repeats，报告实际放置。N=1 仅用池中第一台，是单机参考，不是三台全参与的同预算点；其余计划点都用这 3 台。NAS 的 768 挂载及常驻挂载预算未确认时，三方共同的 768 点均记 `not_run`，不临时换 N。 |
| `smallfile-capacity` | 同一份 10 台池；Apsara fast、CephFS kernel；无 per-agent 限额 | 3,200（每台 320） | 预先做 N=10、2,560 各一轮门禁，不进入正式点；门禁通过后 3 repeats。单列容量，不接到三系统主曲线。 |
| `smallfile-quota` | 同一份 3 台池；Apsara fast 与 CephFS FUSE 各测无限额和 1 CPU/2 GiB；不混 NAS/kernel | 192 | 3 repeats。`cgroup = true` 仅在限额臂；按 `docs/bench-mount-scale.md` 沿用站点 wrapper，真实核对每个 leaf 的 daemon 与 rank 同组且卸载后消失。CPU quota 不是独占 CPU；fixture 准备不受 rank 限额。 |

同一组的 target 顺序在开始前冻结；同一后端固定实例、flags、主机资源及启动方式。运行前预设 startup 180 s、整 MPI job 3,600 s、fixture/cleanup 7,200 s；这是失败截止，不是 mdtest `-W` stonewall，不计为完成。对计划点逐个执行，例如已配置固定三台池之后：

```sh
uv run python -m agent_fs_bench run mount-scale --mode remote --target apsara-fast -- \
  --tasks 768 --repeats 3 --startup-timeout-s 180 --job-timeout-s 3600 --fixture-timeout-s 7200
```

**验收。** 读使用首挂载时预先写完且同步的独立 fixture，卸载/重挂载后首次逐文件全读；只称「重挂载后」，不称已清除服务端、持久、本机共享缓存。写用独立新目录，从 MPI 起始 barrier 到全员完成文件和目标作用域的 `sync -f`，同步包含在时间内；mount、准备、清理不计时。特别核实 fast 下 `sync -f` 确实覆盖数据和目录项，不能只看 exit 0 或正常卸载后的可见性（卸载自身可能写回）；用实现/供应方契约和独立 scratch 验证，不明即不把该写点判为持久。每 repeat 须两阶段各有恰好 `N×1000` 文件和 `N×1000×4096` 字节、N 个 rank 的启动与结果证据、N 个 mount entry、零残留且清理完成。保留前序已经完整验收的 repeat，失败 repeat 不进统计。丢 rank 记录启动数、缺失主机别名和阶段；遇到 PG `57P03`、OSS `QpsLimitExceeded`、EBUSY 等记录实际类别，不吞掉错误或改变口径。高规模先确认内存余量和 mount/session 预算；320 × 1 GiB 配置上限不是已测 RSS，也不是安全余量。

## 4. 定向补正式缺口与回放 smoke

以下工具/Clone 用同一台客户端、相同 CPU 与固定部署的 Apsara fast / NAS / CephFS / ext4 目标。冻结旧证据判断和新跑点清单后再测，不以结果决定是否保留。

工具/Clone/回放用 `run <benchmark> --mode local --target <alias>`；只有 Clone 的 `--smoke` 和 kvtest 的 `--smoke` 放在 CLI 分隔符 `--` 后。每台主机的真实挂载别名、已冻结参数和执行顺序以本地记录为准，不从旧 run 猜测。

- **npm / pnpm：** 只补 `tasks=1, flush=true` 的缺失或不合规点；新测每个 manager 分开，3 repeats，使用 committed `pnpm-bench` lockfile、外置本地预热源、pin 的 Node/manager、`--offline` 和 pnpm `copy`。取 `installs.csv:durable_s`；进程退出单列诊断。同组不把 legacy c8/c16 的 install mean 混进来。
- **SQLite kvtest：** 当前 `kvtest` 默认正式配置：`tasks=1, repeats=5, count=2000, size=2048, variance=0, jmode=delete, multitrans=true, nosync=false`（FULL）。检查实际 journal/sync、每轮 2,000 更新及提交、独立读的完成数和最终 `Integrity-check: ok`。`repeats.csv:microseconds_per_write`、`reads.csv:microseconds_per_read` 分开摘要。旧 `speedtest1` 保持原名。
- **开发工作流：** `dev-workflow`，同一固定本地完整 Linux mirror、base/fixture/编译器指纹，单 task、`make -j8`。每目标三次独立 fresh-workspace run。取 `workflow.csv:elapsed_ms`，检查五个 commit tree 和 `steps.csv:final-sync`；准备/清理在计时外。不能用 `mini-dev-workflow` 替代。
- **Clone：** 优先保留 §2 验过的 cohort；必要时在确定的轴上补同口径的缺点。先以 `-- --smoke` 点验 Apsara 的 `polarfs`、基线 GNU copy、可访问与双向写隔离。正式 `tasks=1`，新测每形状每系统 5 repeats；匹配 seed/形状/源预读，先核对 source + 目标空间。Apsara 由 `target_copiers` 显式选 `polarfs`（环境里供应 `ALIBABA_CLOUD_*`，站点参数本地提供）；其余 `cp-r`。取 `requests.csv:latency_s`；核对 native `fsync_s=0` 及 copy `latency_s=copy_s+fsync_s`。旧轴无法恢复或有实质边界差异时，按冻结的新组测两条轴：固定 1,024 文件，文件大小 4 KiB/64 KiB/1 MiB/4 MiB；固定 4 KiB，文件数 100/1,000/10,000/100,000。各轴独立；空间不足的点记账，不缩规模、不换 copier。
- **Replay：** 只做 smoke，不发 189 任务全队列、不出论文速度比。开始前固定同一个可运行的公开单步 Harbor task、镜像 digest、初态，按 `docs/bench-agent-replay.md` 初始化固定子模块。在 ext4 给 Codex `0.169.0`、OpenClaw `2026.11.3` 各自录制 Qwen 3.8 Max（分别用 Responses / Chat Completions），再在 ext4、Apsara、NAS、CephFS 各跑一次两 harness 的独立回放，`tasks=1, repeats=1`。验收 `status=ok`、可对比工具行为及正的 `tool_s`；不按 reward 选样本，不用 `harbor_s` 替代。缺 provider、协议或 capture 就记 `not_run` / `capture_missing`；不换模型。只回传覆盖、状态、版本及指纹，无任务内容或 cassette。

## 5. 单 Markdown 回传

小文件每个有效 repeat 的 `aggregate_files_per_s = completed_files / elapsed_s`，聚合单位是整个 repeat，读/写分别成组（如 `smallfile-main-read`、`smallfile-main-write`），绝不把 rank 或文件当独立样本。工具和 Clone 在各自上节指定的指标、相同操作/形状和单位内对有效 repeats 聚合。统一分位数算法：将 n 个有效观察排序，按零基位置 `(n-1)·p` 对相邻值线性插值，`p=0.25/0.5/0.75`；`n=1` 三值相同。没有足够证据则只进诊断/失败账目，不伪造统计值。

- 在 `local/round-02-stage/` 放入原样的 `plan.md`、按[执行报告模板](execution-report.template.md)填写的 `execution.md` 和 `summary/`。run 状态沿用模板；记录实际放置、目标别名、测量边界、偏离及来源指纹，不贴逐文件、逐 rank、逐 repeat 或逐 task 表。
- **图点与 repeat 分开记账。** 每个计划图点按组、系列、x、指标对账。`n≥1` 即为有效点，写一行摘要，即使同一 run 后续 repeat 失败；`n=0` 不写数值，在 `summary/failures.csv` 记 `failed`（已尝试但无有效值）或 `not_run`（未执行）。在 `execution.md` 逐点列计划、有效、失败、未执行及排除的 repeat 合计，后四项之和应等于计划数。失败、未执行及排除的 repeat 另在失败账目用 `repeat_failed`、`repeat_not_run`、`repeat_excluded` 计数，不与点数相加。
- 按[交换约定](../docs/markdown-exchange.md)生成 `summary/<group>.csv`（`series,x,metric,median,q1,q3,n`）和同名 JSON（`xlabel,ylabel,unit,logx,logy,series_order,source_runs,formula`）。Apsara 在已观察系列中排首；旧组用 `legacy-` 前缀；smoke 数值不进入正式组。`summary/failures.csv` 用 `group,series,label,count`，同键计数合并；在 `execution.md` 列各缺失点的 x、原因及涉及的 run。全批无有效值时只写失败账目，不造空摘要对。不提交实验结果，不在测试机画论文图。
- 主机、路径、端点、实例、账户和 bucket 用稳定别名。原始 CSV/JSONL、日志、命令环境、cassette、模型响应、配置和凭证不出站。容量不足先压缩说明，不截断或抽样；本包仍放不下的计划点以 `label=not_run` 纳入 `summary/failures.csv`，每个遗漏点算一次。在 `execution.md` 标出具体点及传输原因，保留真实执行状态，区分未执行与已执行但未回传。

人工审核 staging 后，从基准仓库根目录运行（输出文件必须尚不存在）：

```sh
python3 -m agent_fs_bench.exchange export --batch round-02 \
  --root local/round-02-stage --output local/round-02.md --reviewed
```

只回传 `local/round-02.md` 的完整 UTF-8 源文本，不手改或拼接；其余检查产物、日志和样本留测试机。
