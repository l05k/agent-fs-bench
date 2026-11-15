# 执行与偏离报告模板（`STAGE/execution.md`）

> 本文件记录**实际发生了什么**，与 `plan.md` 的冻结方案并列，随同一份 packet 回传。只写脱敏后的事实/证据/推断，不附原始日志、cassette、模型响应、凭证。导出前完成本文件；导入回执由主机保存在 `exchange.json`/本地记录，不写回已导出的 packet。

## 0. 身份与结论

| 项 | 值 |
| --- | --- |
| 方案 ID / 版本 / 冻结哈希 | `<plan-id>` / `<v>` / `<sha256>` |
| 执行人 / 评审人 | `<alias>` / `<alias>` |
| 执行开始 / 结束（UTC） | `<YYYY-MM-DDTHH:MM:SSZ>` / `<...>` |
| 数据快照时间（UTC） | `<...>`；快照范围：`<哪些 run/文件>` |
| 本包 batch / kind | `<batch>` / `<inventory|results>` |
| 结论 | `<完成/部分完成/失败/未执行；只写状态，不用摘要代替数据>` |

一句话范围：本轮实际执行了 `<哪些 row_id>`，未执行 `<哪些 row_id>`；失败/缺失见第 4 节。不要写“数据见附件”而没有清单。

## 1. 计划 vs 实际身份

| 项 | 计划值 | 实际值 | 一致？ | 证据/说明 |
| --- | --- | --- | --- | --- |
| 主仓 commit / 分支 | `<...>` | `<...>` | `<yes/no>` | `git rev-parse` |
| 工作树 | `<clean/dirty>` | `<clean/dirty>`；改动文件数 `<n>` | `<yes/no>` | `git status --porcelain` |
| 原始 `source_sha256` | `<...>` | `<原 config 记录>` | `<yes/no>` | 不重算；脱敏副本不改变原始身份 |
| 回放仓/子模块 | `<...>` | `<...>` | `<yes/no>` | `git submodule status` |
| task cohort / manifest | `<...>` | `<...>` | `<yes/no>` | 原始哈希/数量 |
| 工具/二进制/镜像 | `<...>` | `<...>` | `<yes/no>` | 版本 + 原始 SHA256/digest |
| provider/model | `<...>` | `<...>` | `<yes/no>` | 不写 key；不写响应 |
| launcher / placement | `<...>` | `<...>` | `<yes/no>` | 别名化 |
| 目标/挂载选项 | `<...>` | `<...>` | `<yes/no>` | fstype/选项/持久化语义 |
| 固定资源 | `<...>` | `<...>` | `<yes/no>` | CPU/内存/cgroup/绑核 |

任何 `no` 都要在第 3 节记录并评估可比性。首轮只读盘点不切换旧 checkout；后续若方案明确指定新代码，可使用独立 checkout，保留旧本地改动。计划外的代码/依赖/工具变化作为偏离报告。

## 2. 实际执行记录

```csv
row_id,group_id,benchmark,planned_run_label,actual_run_id,start_utc,end_utc,status,exit_code,targets_completed,targets_planned,tables_rows,notes
R01,G01,<bench>,<label>,<actual-run-id>,<...>,<...>,<complete|partial|failed|aborted|not_run>,<0/...>,<n>,<n>,"<table: rows; missing: ...>",<...>
```

- `actual_run_id` 必须与 `results/<benchmark>/<run-id>/config.json` 和目录名一致；不要编造。
- `status` 只用可观察证据：`complete`（计划 target/repeat/表齐全且验收通过）、`partial`、`failed`、`aborted`、`not_run`。运行中/仍在写的 run 不能写 `complete`。
- `tables_rows` 写精确文件/行数；`wc -l` 是物理行数，CSV 带换行字段时注明，不要冒充记录数。
- 每个 run 的 CSV 与 `config.json` 必须同批；不要把同一 run 拆开。
- 失败 target 保留 `planned_targets`/`targets` 原状；不要删除 CSV 行或补 target。

实际命令（脱敏；只列关键命令，不粘贴整段 shell 历史）：

```sh
# row R01; cwd=<repo-alias>
<实际执行的命令，路径/端点/凭证已别名化或移除>
# row R02 ...
```

凭证只写“已设置环境变量名/未设置”，不写值；不要贴 `env`/`pip freeze`/完整日志。

## 3. 偏离记录

| ID | 时间（UTC） | row_id | 计划 | 实际 | 原因 | 证据（脱敏） | 数据影响 | 处理 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D01 | `<...>` | R01 | `<...>` | `<...>` | `<...>` | `<1–5 行，标 [事实]/[日志证据]/[推断]>` | `<哪些 run/target 受影响；是否新组/排除>` | `<stop/new run/legacy>` |

- 挂载、凭证、数据准备的灵活调整也要记录；不要只记录代码改动。
- 如果手工改了 source/参数：记录新 commit/diff 指纹、新 config、影响的 row；**旧 run 原样保留，不重写历史**。
- 任何边界变化（计时/缓存/持久化/验收/工具/挂载/placement）都新建 `boundary_id`/组，不能只改备注。

## 4. 失败与缺失清单

| run/target/repeat | 类别 | 症状 | 证据（脱敏） | 根因状态 | 影响的数据 | 处理 |
| --- | --- | --- | --- | --- | --- | --- |
| `<...>` | `<setup/measurement/acceptance/transfer>` | `<...>` | `<1–5 行>` | `<确认/推断/未知>` | `<无效/排除/仅清单>` | `<保留/legacy/待补>` |

必须覆盖：

- 失败/中断的 run、未完成 target、缺 repeat、缺表、缺 `config.json`；
- 运行中仍在写、快照时文件大小/mtime 变化、无法确认完整性的 run；
- 只有聚合摘要或只有图片、没有原始逐样本数据的 run（标 `aggregate-only`/`raw-unavailable`，不从图反推）；
- 传输失败与实验失败分开写；传输失败不触发实验重跑。

不重试、不 fallback、不替换样本；失败数据保留为清单/证据，不混入有效组。

## 5. 快照与运行中数据

| 项 | 值 |
| --- | --- |
| 盘点/导出时是否有后台实验在写 | `<yes/no/unknown；哪些进程/终端；不要终止>` |
| 受影响的结果目录/run | `<别名>` |
| 快照边界 | `<开始/结束 UTC；读取了哪些 run；之后是否变化>` |
| 运行中 run 的处理 | `<标 partial/unknown，不进入完整组；等待停写后再盘>` |
| 可复制范围 | `<本包只包含已停写、已核对的文件；其余未包含>` |

不要为了拿到稳定快照而 kill 进程、卸载/重挂或清理；只记录并等待/跳过。

## 6. 脱敏与别名记录

| 项 | 值 |
| --- | --- |
| 别名账本版本 | `<ledger-vN>`；真实映射仅测试机本地保存 |
| 审核人 / 日期 | `<alias>` / `<...>` |
| 已处理字段类别 | `<node/host/placement/CSV host；target/workload path；launcher env/args；clone instance/endpoint；replay container path；...>` |
| 原始身份哈希 | `<保持原记录；未重算冒充>` |
| 脱敏副本说明 | `<哪些文件内容被改；标注 sanitized copy；原文件哈希不变>` |
| 未包含/删除的文件 | `<launcher 日志/appfile/cassette/模型响应/整个 local/...；原因>` |

要求：逐文件人工审核，不依赖正则保证安全；同值同别名、异值异别名、跨批次复用；不确定就移除。`--reviewed` 只在人工审核后使用。

## 7. 导出记录

| batch | kind | 预定输出文件 | 包含哪些 run / 未包含原因 |
| --- | --- | --- | --- |
| `<...>` | `<...>` | `<file.md>` | `<...>` |

- 导出前冻结本报告。导出后的文件数、`document_bytes` 和 stdout JSON 记在测试机本地批次记录，不写回本包，以免自引用或使已有 manifest 失效。
- 太大时按整 run/整组切批：每批仍有 `plan.md`/`execution.md`，不截断、不丢行、不把 run 拆开。
- 导出失败/重导也要记录原因；不要编辑已生成的 packet。
- 导入回执（`exchange.json`、`packet_sha256`、输出目录）由主机另存；本文件不因导入而改写。

## 8. 交付清单

```text
STAGE/
  plan.md
  execution.md
  results/<benchmark>/<run-id>/config.json
  results/<benchmark>/<run-id>/*.csv        # 全部根层 CSV
  legacy/...                                # 旧/不合规数据（如有）
  inventory/...                             # inventory kind 至少一个文件
```

- [ ] `plan.md` 是冻结原文，未被本报告替代/改写。
- [ ] `execution.md` 覆盖实际命令、配置、失败、偏离、快照边界、脱敏/导出。
- [ ] 每个当前 run 的 `config.json` + 全部根层 CSV 齐全；声明表齐全；失败 target 未补造。
- [ ] 没有 target 子目录日志、appfile、stdout/stderr、cassette、模型响应、整个 `local/`、环境 dump、二进制/归档/base64。
- [ ] 别名/哈希/路径合同符合 `docs/markdown-exchange.md`；`--reviewed` 已确认。
- [ ] 测试机没有画图/生成论文图；本包未运行任何导入文件。

执行人签署：`<alias>`；日期：`<...>`；声明：以上为实际执行与偏离，未编造成功数据，未修改原始结果。
