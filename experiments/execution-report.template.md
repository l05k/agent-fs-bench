# 执行与提取报告模板（`STAGE/execution.md`）

> 记录实际发生了什么，以及核实后的摘要如何得到。原始逐样本数据留在测试机；本文件随摘要通过一份 Markdown 回传。不附日志、cassette、模型响应或凭证。

## 0. 身份

| 项 | 值 |
| --- | --- |
| 方案 ID / 版本 / 冻结 SHA-256 | `<plan-id>` / `<v>` / `<sha256>` |
| 执行人 / 评审人 | `<alias>` / `<alias>` |
| 执行起止（UTC） | `<...>` / `<...>` |
| 摘要提取时间（UTC） | `<...>` |
| 本包 batch | `<batch>` |
| 结论 | `<完成/部分完成/失败/未执行>` |

范围：实际执行 `<row_id>`，未执行 `<row_id>`；失败与缺失见第 4 节。

## 1. 计划 vs 实际

| 项 | 计划 | 实际 | 一致 | 说明 |
| --- | --- | --- | --- | --- |
| commit / checkout | `<...>` | `<...>` | `<yes/no>` | 独立 checkout，旧现场未动 |
| 工作树改动 | `<clean/dirty>` | `<...>` | `<yes/no>` | 有改动则给指纹 |
| source/task/tool 身份 | `<...>` | `<...>` | `<yes/no>` | 原哈希，不重算 |
| target / 挂载语义 | `<...>` | `<...>` | `<yes/no>` | 别名化 |
| 固定资源 / placement | `<...>` | `<...>` | `<yes/no>` | 资源额度 |
| 测量边界 | `<B01>` | `<...>` | `<yes/no>` | 变化则新建组 |

任何 `no` 都要在第 3 节记录，并判断该 run 是否仍能进入原摘要组。

## 2. 实际 run

```csv
row_id,group_id,summary_group,benchmark,run_alias,start_utc,end_utc,status,exit_code,targets_done,targets_planned,notes
R01,G01,<group>,<benchmark>,<run-alias>,<...>,<...>,<complete|partial|failed|aborted|not_run>,<0/...>,<n>,<n>,<...>
```

`run_alias` 必须与摘要 JSON 的 `source_runs` 一致。`complete` 只用于计划 target/repeat/验收全部满足的 run；运行中或仍在写的 run 标 `partial`/`unknown`。

关键命令（脱敏；1–5 行证据即可）：

```sh
# row R01; cwd=<checkout-alias>
<实际命令，路径/端点/凭证已别名化>
```

凭证只写“已设置/未设置环境变量名”，不写值。

## 3. 偏离

| ID | UTC | row_id | 计划 | 实际 | 原因 | 影响的数据 | 处理 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D01 | `<...>` | R01 | `<...>` | `<...>` | `<...>` | `<无效/排除/新组>` | `<stop/new run>` |

挂载、凭证、数据准备、代码修改都算偏离。改代码必须记录 commit/未提交指纹和影响范围；旧 run 原样保留。

## 4. 失败与缺失

| run_alias | target | 类别 | 症状 | 证据（脱敏） | 根因 | 是否计数 | 处理 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `<...>` | `<...>` | `<setup/measurement/acceptance/timeout/divergence/capture_missing>` | `<...>` | `<1–5 行>` | `<确认/推断/未知>` | `<failures.csv 的 label/count>` | `<保留/排除>` |

必须覆盖未执行、失败、中断、未完成 target、缺 repeat、运行中快照、只有聚合或图片而没有原始数据的 run。失败数写入 `summary/failures.csv`；不得补零或替换成成功样本。传输失败不属于实验失败，不触发重跑。

## 5. 摘要提取

| summary_group | 源 run 别名 | 原始文件名 | 原始 SHA-256 | 提取脚本/命令 | 公式 | 行数 | 失败计数来源 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `<group>` | `<run-aliases>` | `<raw-alias>` | `<sha>` | `<script-alias + hash / 命令>` | `<确切公式>` | `<n>` | `<failures.csv 行>` |

- 每个计划图点都要有摘要行或失败账目；不能两处都无，也不能两处都有而语义冲突。
- `median/q1/q3/n` 的聚合层级一致；`n` 是独立观测数，不是文件数或行数。
- 失败、超时、发散、缺失不进入 `n`；`n=1` 时 `q1 = median = q3`。没有有效值的组只写失败账目，不生成空摘要。
- 原始数据保留位置：`<path-alias>`；不随本包传输。重算所需的信息必须完整。

## 6. 脱敏与别名

| 项 | 值 |
| --- | --- |
| 别名账本版本 | `<ledger-vN>`；真实映射只在测试机 |
| 审核人 / 日期 | `<alias>` / `<...>` |
| 已处理 | 主机/节点、target 路径、端点/实例、run 别名、配置路径 |
| 保留 | CPU/FS/版本/样本数/统计量/原始 hash |
| 未包含 | 原始 CSV、日志、cassette、模型响应、整个 `local/` |

逐文件人工审核，不使用正则保证安全；不确定就删除，而不是保留。

## 7. 导出记录（导出前填写；之后不再修改本文）

| batch | 预定输出 | 包含的 summary_group | 未包含/原因 |
| --- | --- | --- | --- |
| `<...>` | `<file.md>` | `<groups>` | `<...>` |

`document_bytes`、输出文件数和 stdout JSON 记在测试机本地批次记录，不写回本包，以免自引用或使 manifest 失效。导出后不编辑 packet；一份文档只包含完整批次。

## 8. 交付检查

- [ ] `plan.md` 是冻结原文；本报告覆盖实际命令、偏离、失败和提取。
- [ ] 每个计划 run/target 都在摘要或失败账目中；`n` 与 `q1/q3` 同层，公式可重算。
- [ ] `summary/` 仅含 `<group>.csv`、`<group>.json`、`failures.csv`；没有原始样本、日志、cassette。
- [ ] 主机/路径/端点/凭证已别名化或删除；人工审核完成。
- [ ] 测试机没有生成论文图；导入后由写入机从摘要绘图。

执行人：`<alias>`；日期：`<...>`；声明：未编造成功数据，未修改原始结果。
