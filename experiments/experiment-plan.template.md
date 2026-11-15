# 实验方案模板（复制为 `STAGE/plan.md` 后冻结）

> 本文件是**输入方案**，不是执行器，也不是结果报告。执行前由主机评审并冻结；执行后不要改写本文件。实际动作、配置变化、失败和偏离写到同目录的 `execution.md`。  
> 本模板中的 `<...>` 必须填成真实身份或用 `UNKNOWN` 标明未知；**不要填造 task ID、seed、provider、镜像或测量值**。所有私有主机/路径/端点用稳定别名；真实映射只留在测试机本地别名账本，不写进本文件。凭证只写环境变量名，不写值。

## 1. 实验问题与判定

- 方案 ID：`<plan-id>`；版本：`<v1>`；日期：`<YYYY-MM-DD>`。
- 科学问题：`<一句话；要比较什么，不比较什么>`。
- 可证伪预测：`<预期方向/差异；若没有预测写 NONE>`。
- 本轮回答不了的问题：`<显式排除，避免事后扩大结论>`。
- 判定规则（先定，执行后不得改）：`<例如“按 run 级中位数比较；失败 run 不进入配对；不因结果改阈值”>`。
- 结论只用原始逐样本数据计算；不用统计摘要替代原始值，不从图片反推。

## 2. 固定身份（pinned identity）

| 项 | 值 | 证据/来源 |
| --- | --- | --- |
| 主仓别名 / 分支 / commit | `<repo-alias>` / `<branch>` / `<sha>` | `git rev-parse HEAD` |
| 主仓工作树 | `clean` 或 `dirty`；改动文件数 `<n>`；原始 diff 指纹 `<sha256>` | 盘点/`git diff --stat`；指纹在脱敏前对原始 diff 计算，之后不重算冒充一致 |
| 记录的 `source_sha256` | `<原 config.json 中的值，或 UNKNOWN>` | 原样记录；脱敏后不重算 |
| 本地改动与科学含义 | `<CONFIG/脚本/commit；影响哪些组>` | 盘点回答；小 patch 可附在 execution.md，秘密/路径已处理 |
| 回放仓 / 子模块 | `<repo-alias>` / `<commit>` / `<submodule path alias>` | `git submodule status` |
| task cohort / manifest | `<公开 ID 或稳定别名>`；数量 `<n>`；manifest SHA256 `<sha>` | 原 manifest；内容不随 packet 传输 |
| cassette 集 | 数量 `<n>`；原始 SHA256 `<sha>` | 只传身份/缺失清单，不传 cassette 内容 |
| 工具 / 二进制 | 名称 `<tool>`；版本 `<version>`；路径别名 `<path>`；原始 SHA256 `<sha>` | 原样记录；不要重算 |
| seed / 输入生成器 | `<seed / generator version>`；没有写 `N/A` | 原配置/代码 |
| provider / model | provider 别名 `<provider-a>`；model/版本/digest `<...>`；key 只写环境变量名 | 不写 key；不写模型响应 |
| 容器 / 镜像 | 镜像 `<ref>`；digest `<sha256>`；运行时 `<docker/podman version>` | `docker image inspect` 只读 |
| launcher | `mpiexec`、`mpiexec_args`（别名化）、`env`（仅变量名/公开值）、MPI 版本 | 实际配置 |
| 目标与挂载 | target 别名、fstype、影响语义的挂载选项、持久化/缓存语义 | 盘点回答；真实路径/服务器别名化 |

规则：

- 正式执行所需身份不能为 `UNKNOWN`；先补齐。明确标记的诊断 run 可以带已声明未知项，不进入正式比较组。
- 不要为了“看起来和旧 run 一致”而重算 source/binary/task/cassette 哈希；身份以原始记录为准。
- 如果执行中手工改了 source/参数，不要覆盖历史 run：新建 run/组，记录新的 source/参数/失败/遗漏，旧 run 原样保留。
- 源码 patch 只附与本实验相关的改动，逐段人工脱敏；不要用 `git diff` 批量打包，也不要把 `local/`、秘密或无关工作区改动混入 packet。

## 3. 实验矩阵（一行一个计划 run）

```csv
row_id,group_id,run_label,purpose,benchmark,mode,target_alias,tasks,repeats,shape,config_variant,boundary_id,required_tables,pool_with,order,notes
R01,G01,<label>,<question|smoke|exclude>,<benchmark>,<local|overflow|remote>,<fs-a>,<N>,<R>,<files/size/params>,<variant>,<B01>,<phases;ranks>,<G01>,<1>,<...>
```

- `run_label` 是计划标签，不是实际 `run_id`；实际 `run_id` 由执行工具生成，写入 `execution.md`。
- 同组只改变计划内变量；固定变量见本节固定变量表。缺正式 task ID/seed/provider 就先保留未知，**不要编造**。
- `purpose=smoke` 的行不进入论文数据；`exclude` 要写原因。
- `pool_with` 只能连同一 `boundary_id` 且身份/配置可比的 run；不同测量边界不 pool。
- 计划顺序/交错：`<说明；为什么这样排；target 顺序是否轮换>`。
- 总样本量/重复理由：`<按盘点与资源确定；不要写未经验证的数字>`。

固定变量表：

| 变量 | 固定值/别名 | 说明 |
| --- | --- | --- |
| CPU/内存/节点 | `<...>` | 所有可比 run 相同 |
| 挂载/文件系统选项 | `<...>` | 同组不变 |
| 工具/二进制/镜像 | `<...>` | 同组不变 |
| task/cassette cohort | `<...>` | 同组不变 |
| 缓存/warmup | `<...>` | 同组不变 |
| 并发/placement | `<...>` | 除计划变化外不变 |

## 4. 测量边界与完成检查

| group_id | boundary_id | benchmark | 计时开始 | 计时结束 | 排除的工作 | 缓存/warmup | 持久化/完成 | 验收 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G01 | B01 | `<bench>` | `<event>` | `<event>` | `<setup/restore/verify/...>` | `<实际边界；小文件只允许卸载再挂载，不承诺服务端冷缓存>` | `<fsync/syncfs/close/server ack/none>` | `<exit/version/task count/row checks>` | `<all targets/repeats/tables; no retry>` |

要求：

- 计时边界、缓存、持久化、验收任何一个变化，就新建 `boundary_id`/组；不要在同一组里混。
- “完成”必须可观察：退出码、目标完成、表齐全、行数/columns、重复次数、失败清单；不要用“看起来正常”。
- 失败处理：`<fail-fast：哪个失败停止整组/哪些独立行可继续；不重试、不 fallback、不替换>`。
- 已知 caveat：`<旧代码 bug、mdtest CSV 已知问题、FUSE 缓存、NFS 语义、replay divergence 等；引用盘点证据>`。
- 运行中/后台写：执行前确认没有其他实验在写目标目录或结果目录；无法确认则暂停并记录，不能把运行中数据当完整结果。

## 5. 实际执行命令

所有路径/目标用本地环境变量别名；真实值只在测试机 shell 中设置，不写入本文件。凭证只用环境变量名。

```sh
# 变量由测试机本地别名账本提供；不要把真实值写进 plan/execution
export AFB_REPO="<repo-alias>"
export AFB_TARGET_A="<fs-a>"
# 例：clone 的云凭证只存在于进程环境，变量名如下，不写值
# export ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET / ALIBABA_CLOUD_SECURITY_TOKEN

cd "$AFB_REPO"

# 执行前只读核对：source/tool/mount 与 plan 一致
git rev-parse HEAD
git status --porcelain=v1 -b
# <按需：版本/哈希/挂载检查命令；不要改现场>

# R01（按盘点确认并在方案中固定的入口执行，不自动切换入口）
uv run python -m agent_fs_bench run <benchmark> --mode <mode> --dirs "$AFB_TARGET_A"

# 执行后只读检查：run 目录、config 字段、CSV 表头/行数；不编辑结果
find results/<benchmark>/<run-id> -maxdepth 1 -type f -printf '%f\t%s\n'
```

如果测试机使用不同的入口、不同的 `--mode`、额外 wrapper 或手工挂载，**不要静默替换**：在该行注明 `row_id`，把实际命令和原因写入 `execution.md`，并判断是否改变 `boundary_id`/可比性。

## 6. 结果文件要求

执行后，staging 中必须能形成：

```text
STAGE/
  plan.md                    # 本文件冻结原文
  execution.md               # 实际执行 + 偏离 + 失败 + 脱敏/导出记录
  results/<benchmark>/<run-id>/
    config.json              # 当前 schema，required 字段齐全
    <table>.csv              # 该 run 根层全部 CSV；config.tables 声明的表必须齐全
```

- 每个 `run` 保留 `config.json` 和全部根层 `*.csv`；不要只挑声明表，不要附带 `<target>/` 日志/appfile/stdout/stderr。
- 失败 target：保留 `planned_targets` 与 `targets[]` 的差异，CSV 可保留失败行；不要补 target、不要删行、不要把未完成写成完成。
- 预期表/行数（计划值，可留待执行后核对）：`<table -> expected columns/rows>`。
- 旧格式/字段不全的 run 放 `legacy/`，不补造新字段；不默认进入论文。
- `execution.md` 的失败/缺失清单必须覆盖：失败 run/target/repeat、缺表、缺 config、被中断、只有摘要/图片、运行中快照。

## 7. 偏离与失败报告

执行中任何与冻结方案不同的地方都必须记录，不能只口头说明：

| ID | 时间 | row_id | 计划 | 实际 | 原因 | 证据（脱敏） | 数据影响 | 处理 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D01 | `<UTC>` | R01 | `<planned>` | `<actual>` | `<why>` | `<1–5 行，标 [日志证据]/[事实]/[推断]>` | `<哪些 run/target 无效/排除/需新组>` | `<stop/new run/legacy>` |

失败清单：

| run/target/repeat | 类别（setup/measurement/acceptance/transfer） | 症状 | 证据（脱敏） | 根因状态 | 影响的数据 | 是否重跑 |
| --- | --- | --- | --- | --- | --- | --- |
| `<...>` | `<...>` | `<...>` | `<...>` | `<确认/推断/未知>` | `<...>` | `否（默认）` |

规则：失败即停/记录；不重试、不 fallback、不换样本；不把失败 run 混入有效组；不因为传输失败而重跑实验。

## 8. 执行前检查与冻结

- [ ] 盘点已审查；`UNKNOWN` 已补齐或明确接受；旧代码/本地改动/历史错误已纳入边界。
- [ ] 首轮盘点不改旧 checkout；若本轮需要新代码，已在矩阵中指定 commit/本地改动和独立 checkout，旧代码及结果保留。旧格式数据走 `legacy/`，不补造字段。
- [ ] 来源/工具/task/cassette/provider 身份已固定；没有编造 ID/seed/provider，没有重算身份以掩盖实际变化。
- [ ] 矩阵覆盖所有计划 run；每个组只有一种测量边界；`pool_with` 没有跨边界。
- [ ] 挂载、固定资源、launcher、placement、缓存/持久化语义已记录并与组一致。
- [ ] 实际命令可执行；凭证只在环境变量；本文件无真实主机/路径/端点/秘密。
- [ ] 结果要求、失败/偏离表、`execution.md` 负责人已明确；测试机不画图。
- [ ] 执行前确认没有并发写同一目标/结果目录；运行中数据不当完整结果。
- [ ] 方案评审通过；冻结日期 `<...>`；评审人 `<alias>`。冻结后计算文档 SHA-256，登记在 `execution.md` 和本地批次记录，不把文档自己的哈希写回本文。

冻结后如需手工改方案：新建 plan 版本/组或在该行明确记录新 source/参数，保留旧 run 原样，不重写历史。执行人：`<alias>`；日期：`<...>`。
