# 实验方案模板（冻结后提交 GitHub）

> 本文件是**输入方案**，不是执行器。代码、分支和本方案通过 Git 传递；测试机只把核实后的摘要通过一份 Markdown 回传。原始逐样本数据留在测试机。所有私有主机、路径、端点用稳定别名；凭证只写环境变量名。本文件不填造 task ID、seed、provider、镜像或统计值。

## 1. 问题与判定

- 方案 ID / 版本 / 日期：`<plan-id>` / `<v1>` / `<YYYY-MM-DD>`。
- 科学问题：`<要比较什么，不比较什么>`。
- 可证伪预测：`<方向/差异；没有写 NONE>`。
- 本轮不回答：`<显式排除>`。
- 判定规则（执行前冻结）：`<配对单位、分母、失败处理、图的读法>`。

## 2. 固定身份

| 项 | 值 | 证据 |
| --- | --- | --- |
| 仓库 / commit | `<github-org/repo>` / `<sha>` | `git rev-parse` |
| 实验 checkout | `<path-alias>`；独立于旧版现场 | `git status` |
| `source_sha256` | `<原值，或 UNKNOWN>` | 原记录；不重算 |
| 回放子模块 | `<commit>` | `git submodule status` |
| task cohort / manifest | `<IDs 别名>`；数量 `<n>`；SHA-256 `<sha>` | 原 manifest |
| cassette | 数量 `<n>`；SHA-256 `<sha>` | 内容不传输 |
| 工具 / 镜像 | `<名称>` / `<版本>` / `<digest>` | 原记录 |
| provider / model | `<provider-alias>` / `<model/version>` | key 只写变量名 |
| target / 挂载 | `<target-alias>` / fstype / 语义化选项 / 独立客户端证据 | 站点清单 |
| 固定资源 | CPU/内存/节点/cgroup/placement | 站点清单 |

正式执行所需身份不得为 `UNKNOWN`；诊断 run 可以带已声明未知项，但不进入论文比较组。

## 3. 矩阵

```csv
row_id,group_id,run_label,purpose,benchmark,mode,target_alias,tasks,repeats,shape,config_variant,boundary_id,summary_group,order,notes
R01,G01,<label>,<formal|smoke|exclude>,<benchmark>,<local|overflow|remote>,<fs-a>,<N>,<R>,<shape>,<variant>,<B01>,<group>,<1>,<notes>
```

- 一行一个计划 run；实际 `run_id` 由工具生成，记入执行报告。
- 同组只改计划变量；固定变量写在第 4 节。`pool_with` 不得跨 `boundary_id`。
- `summary_group` 必须是第 6 节声明的组，或 `none`（不进入论文）。
- 失败 run 原样保留；不重试、不替换样本、不为凑齐组而删除。

## 4. 测量边界

| group_id | boundary_id | 计时开始 | 计时结束 | 排除 | 缓存/warmup | 持久化/完成 | 验收 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| G01 | B01 | `<event>` | `<event>` | `<setup/restore/verify>` | `<实际边界>` | `<fsync/syncfs/ack/none>` | `<exit/计数/行检查>` | `<targets×repeats×tables>` |

计时、缓存、持久化、验收任一变化就新建组。小文件读只能用卸载再挂载作为客户端缓存边界，不声称清空服务端缓存。失败默认 fail-fast，保留现场。

## 5. 执行

```sh
git clone <github-url> <path-alias>
git -C <path-alias> checkout <sha>
# 只读预检：commit、工作树、挂载、工具版本
cd <path-alias>
uv run python -m agent_fs_bench run <benchmark> --mode <mode> --dirs <target-alias>
```

旧实验现场不被覆盖。若方案需要保留旧的本地改动，在方案中指定独立 checkout 和改动指纹。实际入口、额外 wrapper、挂载调整必须在执行报告中记录并判断是否改变 `boundary_id`。

## 6. 摘要组（执行前固定）

| summary_group | 文件 | metric | x | series | 聚合单位 | 公式 | 单位 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `<group>` | `summary/<group>.csv` | `<metric>` | `<tasks/files/agents/all>` | `<target/copier>` | `<repeat/task/run>` | `<确切公式>` | `<unit>` |

每组要求：

- 每个有有效观测的图点一行：`median/q1/q3` 在同一聚合层计算，`n` 是独立观测数；`q1 <= median <= q3`，`n=1` 时三者相等。
- `series_order` 固定图例顺序，主系统第一。
- `source_runs` 使用执行报告中的稳定别名。
- 失败/超时/发散/缺失进入 `summary/failures.csv`，不进入 `n`，不补零。没有有效值的组只写失败账目，不造空摘要对。
- 原始样本保留在测试机；执行报告记录提取命令、脚本别名/哈希和源结果指纹。

```csv
series,x,metric,median,q1,q3,n
<series-a>,<x>,<metric>,<median>,<q1>,<q3>,<n>
```

## 7. 交付与冻结

```text
STAGE/
  plan.md
  execution.md
  summary/<group>.csv
  summary/<group>.json
  summary/failures.csv
```

- [ ] 问题、判定、矩阵、边界、摘要组已在执行前固定。
- [ ] commit/task/tool/provider 身份齐全；没有编造；原始哈希未重算冒充。
- [ ] 每个计划 run/target 都有归属：摘要组或失败账目，不能两处都无。
- [ ] 摘要公式能由原始数据重算；`n` 与分位数同层；失败不进入 `n`。
- [ ] 不传输原始样本、日志、cassette、模型响应、凭证或整个 `local/`。
- [ ] 冻结日期 `<...>`；评审人 `<alias>`；方案 SHA-256 记入执行报告和本地批次记录。
