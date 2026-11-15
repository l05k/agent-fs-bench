# Markdown 单文档实验交换

本文件定义主机与测试机之间的单文档交换：人工方案传入，已审核原始数据传出，主机导入后分析和画图。工具只封装/恢复文本，不自动脱敏、不执行命令、不跑实验。首轮用 [`00-inventory-request.md`](../experiments/00-inventory-request.md) 在旧 checkout 只读盘点，不依赖新工具；后续是否切换代码由审阅后的方案明确指定，不能覆盖旧机器的本地改动。

相关模板：

- 初轮盘点请求：[`experiments/00-inventory-request.md`](../experiments/00-inventory-request.md)
- 方案模板：[`experiments/experiment-plan.template.md`](../experiments/experiment-plan.template.md)
- 执行/偏离报告模板：[`experiments/execution-report.template.md`](../experiments/execution-report.template.md)

## 1. 闭环

| 阶段 | 在哪 | 输入 | 输出与门禁 |
| --- | --- | --- | --- |
| 0 盘点 | 测试机 | 盘点请求（一份 Markdown） | 一份 Markdown：版本、改动、机器/挂载、语义组、全部 run、已有原始数据、历史错误。只读，不改现场，不升级，不重跑。记录后台实验是否仍在写、可复制快照范围；运行中数据不当完整结果。 |
| 1 审查 | 主机 | 盘点回答 | 标出缺失/矛盾/`unknown`；必要时按组/按 run 要下一批。**不猜、不用图反推。** |
| 2 冻结方案 | 主机 | 盘点结论 + 方案模板 | `plan.md`：来源/工具/task 身份、矩阵、测量边界、完成检查、实际命令、结果文件要求。评审后冻结并记哈希。 |
| 3 执行 | 测试机 | 冻结的 `plan.md` | 实际运行结果 + `execution.md`：实际命令、配置、失败、偏离原因、影响的数据。失败即记录，不重试、不 fallback。 |
| 4 人工脱敏 | 测试机 | 原始结果 + `plan.md` + `execution.md` | `STAGE/` 副本：只放已审核文本；真实结果留在原位不动。建立/复用别名账本，逐字段人工检查。 |
| 5 导出 | 测试机 | `STAGE/` | 一份 Markdown；stdout 报 `document_bytes`。太大就按整 run/整组分批，不截断、不丢行。 |
| 6 导入验证 | 主机 | 单文档 | `exchange.json` 回执；全量校验通过才落盘；不执行任何文件。 |
| 7 分析与出图 | 主机 | 导入目录 | 现有 `replay_report` / `plot` 原样复用；只在主机画图；先确认可比性，再配对/绘图。 |
| 8 论文使用 | 主机 | 审核后的结果 | 旧数据不伪装新格式，不默认进入论文；本变更不运行正式实验、不更新论文或图。 |

测试机由操作者/助手灵活准备挂载、凭证和数据，不开发方案执行器；实际配置及偏离写进 `execution.md`。如后续方案需要新代码，另建固定 commit 的 checkout，保留旧 checkout 与结果。交换工具也可以在独立的新版 checkout 中运行，通过 `--root` 读取已审核副本，不必升级旧实验环境。

## 2. 单文档类型与 staging 目录

导出工具接受两种 `--kind`：

| kind | 必须包含 | 禁止 / 说明 |
| --- | --- | --- |
| `inventory` | 根 `plan.md`、`execution.md`；至少一个 `inventory/` 下的文本文件 | 不能有 `results/`。旧/历史数据放 `legacy/`。适合盘点、索引、脱敏后的旧数据。 |
| `results` | 根 `plan.md`、`execution.md`；至少一个当前格式 run：`results/<benchmark>/<run-id>/config.json` + 同层全部 `*.csv` | 旧格式或字段不全的数据必须放 `legacy/`；不要补字段、不要改 schema 伪装成当前 run。 |

`STAGE/` 只允许以下布局（根目录**只能**有 `plan.md`、`execution.md` 两个文件）：

```text
STAGE/
  plan.md
  execution.md
  results/<benchmark>/<run-id>/
    config.json
    <table>.csv
    ...                       # 该 run 根层的全部 CSV
  inventory/                  # kind=inventory 至少一个文件；允许嵌套
    <reviewed>.md|csv|json|txt|patch
  legacy/                     # 旧格式/不可比数据；允许嵌套
    <reviewed>.md|csv|json|txt|patch
```

- `results/` 下的 run 目录**不能再有子目录**（不要带 `<target>/` 日志、stdout/stderr、appfile、诊断文件）。结果只导出 `config.json` 和该 run 根层的 `*.csv`。
- `config.tables` 声明的每张表都必须存在；每个 run 根层的**所有** CSV 都要导出，不能只挑声明表，也不能删除多余 CSV。`config.json` 与全部 CSV 必须同批，不能把一个 run 拆开。
- 失败 target 可以保留：`planned_targets` 包含它、`targets[]` 可能没有它，CSV 里可以有它的行。保持现状；不要为了让校验通过而删除行、补 target 或改状态。
- 如果某个当前 run 的字段/表不符合当前 schema：**不要补造字段或重命名旧表**。把该 run 留在测试机，标为 blocked，或在确认它属于历史格式后放 `legacy/`；两种处理都写进 `execution.md`。
- `legacy/` 不参与当前 schema 校验，不默认进入论文分析。历史 `.csv`/`.json` 可原样保留；其他纯文本格式可用 `.txt` 文件名承载，内容不转换，并在索引登记原文件名。不要冒充 `results/` 的新表。
- `inventory/` 放本轮盘点产物（如 `runs.csv`、`groups.csv`、`aliases.md`），至少一个文件；不能用 `results/` 代替。
- 导入生成的 `exchange.json` 是回执，不属于 staging；不要把它再放进下一次导出的目录（根目录只允许两个文件）。
- 不要放隐藏文件、符号链接、空名字、绝对路径、`..`、反斜杠、空格、中文文件名或归档文件。路径组件必须是 ASCII token：`[A-Za-z0-9][A-Za-z0-9_.-]*`。run-id 原样使用；若它含私有信息，优先不要用 `results` 包，改走 `legacy` 并说明。

## 3. 文本与字节合同

- 只接受 UTF-8 文本，建议无 BOM；LF 或 CRLF。含 CR（未被 CRLF 消掉）或 NUL 的文件会被拒绝；二进制、base64、压缩包、图片都不行。
- 导出时 CRLF 归一为 LF，并为非空且缺少末尾换行的文件补一个换行；已有空行不删除。manifest 的 `bytes`/`sha256` 针对归一化文本，不是原始文件字节。
- 允许的扩展名只有 `.md`、`.csv`、`.json`、`.txt`、`.patch`；`plan.md`/`execution.md` 固定为根文件名。
- `plan.md` 必须是冻结的输入方案原文（不是摘要，不要在执行后改写）；`execution.md` 必须是非空的实际执行、偏离、失败、脱敏/导出记录。二者对所有 kind 都是必需的。
- 工具不做自动脱敏、不做格式化、不做 schema 转换；不要靠正则替换来保证安全。

## 4. 导出 / 导入 API

以下命令在**已有 `agent_fs_bench.exchange` 的 checkout** 根运行；它可独立于旧实验 checkout，纯 stdlib、不要求 `uv` 或新依赖。首轮旧机器仍按盘点请求直接回答，不需要这些命令。

```sh
cd "$AFB_REPO"

# 盘点/旧数据：至少一个 inventory/ 文件，不能有 results/
python3 -m agent_fs_bench.exchange export \
  --kind inventory \
  --batch inv-20261115-01 \
  --root "$AFB_STAGE" \
  --output "$AFB_OUT/inv-20261115-01.md" \
  --reviewed

# 当前格式结果：至少一个 run（可以部分失败），保持真实完成状态
python3 -m agent_fs_bench.exchange export \
  --kind results \
  --batch results-20261115-01 \
  --root "$AFB_STAGE" \
  --output "$AFB_OUT/results-20261115-01.md" \
  --reviewed
```

- `--reviewed` 是必需的人工确认：staging 中每个文件都已检查过，没有私有主机/路径/端点/凭证。没有这个参数不会导出。
- `--batch` 只能由 ASCII 字母/数字/`.`/`_`/`-` 组成，长度 1–80；建议日期+序号。内容改变后换新的 batch ID。
- `--output` 必须不存在，且必须在 `--root` 之外；工具不会覆盖。输出父目录要先建好。
- 导出成功时 stdout 是 JSON，例如：
  `{"batch": "results-20261115-01", "kind": "results", "files": 7, "document_bytes": 123456}`
  `document_bytes` 是整份 Markdown 的字节数（含 manifest/fence），用于判断是否要缩小下一批。
- 校验失败不会写 packet。文件系统写入出错会非零退出，可能留下不完整输出；不要手工修补，修正原因后用新的输出文件重导。

主机侧导入到**新目录**：

```sh
python3 -m agent_fs_bench.exchange import \
  "$AFB_IN/results-20261115-01.md" \
  --output "$AFB_WORK/results-20261115-01"
```

- `NEW_DEST` 必须不存在；导入不会覆盖、合并或修改已有目录。
- 成功时 stdout 是 JSON：`{"batch": "...", "kind": "...", "files": N, "output": "..."}`；目录内会写恢复的文件和 `exchange.json`（含 `packet_sha256`）。
- 文本、指纹或结果合同校验失败时不创建输出目录。文件系统写入错误可能留下部分目录；停止并报告，不复用该目录或手工合并。复制内容不完整时重新取得完整源文档，不为传输失败重跑实验。

## 5. 单文档内部格式与校验

导出器生成的内容只有三类块，文件路径均相对 staging：

1. 标题和说明文字（导入忽略块外内容，但不要手改生成结果）。
2. 一个 `afb-manifest` JSON fence：
   ```json
   {
     "kind": "results",
     "batch": "results-20261115-01",
     "files": [
       {"path": "plan.md", "bytes": 1234, "sha256": "..."}
     ]
   }
   ```
3. 每个文件一个 `afb-file <path>` fence，内容为该文件的归一化文本。fence 长度由导出器按内容中最长反引号串加一决定（至少 3 个反引号），所以内容里的反引号不会截断文件。

导入校验顺序（全部通过才落盘）：

- 路径：只允许 `plan.md`、`execution.md`、`inventory/…`、`legacy/…`、`results/<benchmark>/<run-id>/config.json` 或同层 `.csv`；拒绝绝对路径、`..`、隐藏组件、非法 token、重复文件、未闭合 fence、重复 manifest、非 UTF-8/CR/NUL。
- manifest：文件列表、每个 `bytes`、每个 `sha256` 必须与实际块内容完全一致；多一个/少一个都不行。
- kind：`inventory` 至少一个 `inventory/` 文件且不能有 `results/`；`results` 至少一个 run。
- 当前结果：每个 run 都必须有 `config.json`，且字段包含 `benchmark`、`run_id`、`git_commit`、`source_sha256`、`config`、`nodes`、`planned_targets`、`targets`、`tables`；`benchmark`/`run_id` 与目录名一致；`planned_targets` 唯一；完成的 `targets[]` 唯一且是 planned 的子集；每张声明表的 CSV 表头必须等于 `config.tables[table].keys + .metrics`；每行 `target` 必须属于 `planned_targets`。
- `exchange.json` 的 `packet_sha256` 是收到的原始 Markdown 字节哈希，可用于记录批次；它不是 source/tool/task 身份。

工具不会执行导入文件，也不会自动运行 `plan.md`/`execution.md` 中的命令。

## 6. 人工脱敏与别名

### 6.1 原则

- **先复制，后修改**。原始 `results/`、`local/`、日志、cassette 留在测试机原位，绝不原地编辑；只在 `STAGE/` 的副本上改。
- **人工逐字段审核**。正则搜索只能用来找候选，不能作为安全保证；每个文件至少人工读一遍，重点看嵌套 JSON、CSV 自由文本、命令和日志摘录。
- **稳定别名**。同一真实值在所有批次用同一别名；不同真实值用不同别名。别名不要由真实值截断/哈希/前缀生成，也不要暴露真实结构。真实映射只存在测试机本地的别名账本中，不随 packet 发送。
- **保留逻辑含义**。别名要让主机看懂“这是哪个角色/哪个 target/哪个端点”，例如 `node-a`、`path-fs-a`、`endpoint-a`、`instance-a`、`container-data-a`；父子路径的关系可用 `path-fs-a/sub` 表达。
- **不重算身份哈希**。`source_sha256`、binary/tool/task/cassette 的原始哈希保持原记录；脱敏副本改过内容就标注“sanitized copy”，不要把重新计算的哈希冒充原始身份。传输完整性由 manifest 的 `sha256` 保证，和实验身份是两回事。

### 6.2 逐字段处理

| 私有字段 | 常见位置 | 处理 | 必须保留 |
| --- | --- | --- | --- |
| 主机名 / node name | `nodes[].name`、`nodes[].hostname`、`hosts.toml` 的 key、placement key、CSV `host`、日志/命令 | 全部换成 `node-a`/`node-b`；同一机器的所有出现用同一别名 | 角色、CPU/内存、任务数、机器数量 |
| host / placement key | `hosts`、`placement`、per-node 统计、CSV `host` | 与 node name 同一别名体系；placement 的数值和分布保留 | 任务到机器的分配、每机器任务数 |
| target 路径 | `targets[].path`、`--dirs`、命令、错误文本、`volume_root` | `path-fs-a` 或 `target:fs-a`；保留相对子路径和读写角色 | fstype、挂载选项、target 逻辑名 |
| target / backend 名 | `targets[].name`、CSV `target`、图例/配置 | 私有名换 `fs-a`；公开通用名（如 `ext4`/`nfs`）可保留；所有字段一致替换 | 比较关系、copier/后端语义 |
| workload 配置路径 | `CONFIG` 里的 `mdtest_bin`、脚本路径、数据目录、源码目录、`root`/`python` | `tool-mdtest`、`path-work-a`、`path-repo-a`；保留版本/角色 | 工具版本、二进制哈希、参数 |
| launcher env/args | `config.launcher`、`mpiexec_args`、`env`、hosts 的 `ssh_args` | 选项名保留；接口/CIDR 换 `net-a`；env 值换 `env-a`；凭证值直接删除（不留别名） | 选项数量、网卡选择语义、MPI 版本 |
| SSH 用户/端口/密钥 | `hosts.toml`、命令、错误 | 用户/端口/密钥路径别名或删除；绝不发私钥 | 连接角色、端口类别（若影响测量） |
| clone instance/endpoint | `config.polarfs` 的 `instance_id`、`region_id`、`endpoint`、`volume_root`；命令 | `instance-a`、`region-a`、`endpoint-a`、`volume-a`；保留协议/端口类别 | API 名称、SDK 版本、超时/重试语义 |
| replay container path | `local/agent-replay.toml` 的 mount、容器内路径、命令 | `container-data-a:/work`；保留 source→target 关系、只读/可写、镜像 digest | task/cassette 身份、镜像 digest、挂载语义 |
| task / cassette | manifest、run 配置、路径 | 公开 task ID 原样保留；私有 ID 用 `task-a`/`cassette-a`；cassette 内容、模型响应绝不上传 | 原始哈希、数量、配对关系、缺失清单 |
| provider / model | 配置、命令、环境变量名 | provider/endpoint 用 `provider-a`/`endpoint-a`；model 版本/digest 可保留；key 删除 | 协议、模型版本、采样参数 |
| Git remote / 用户名 | `git remote -v`、日志、路径 | `remote-a`、`user-a`；去掉 URL 凭证和主机名 | commit/branch、仓库角色 |
| 日志/错误文本 | 失败报告、execution.md | 不传原始日志；最多 1–5 行脱敏摘录，标 `[日志证据]` | 错误类别、退出码、时间、影响的数据 |
| 设备标识 | 序列号、MAC、磁盘 ID、许可证 | `device-a`；能删就删 | 设备类别、数量、是否影响测量 |

补充：

- `config` 是任意嵌套结构，必须逐层检查；legacy 的 `parameters` JSON 字符串里常有二进制路径、挂载路径、端点，不能只看列名。
- CSV `target`/`host` 与 `config.planned_targets`/`targets`/`nodes` 的别名必须一致；改了别名就要全链路一致，否则当前结果的完整性校验会失败。
- `source_sha256`、`git_commit`、`mpi_version`、工具版本、镜像 digest 是身份证据，通常不是秘密，保留原值。
- 时间戳和 run-id 通常保留；不要把 `config.run_id` 改成别名而不改目录名。
- 不确定某个值是否敏感时：**不发**。缺少一个字段比泄露一个凭证好。

### 6.3 明确禁止

- 密码、token、cookie、私钥、云 AK/SK、STS token、`.env` 值、SSH 配置内容。
- 原始 launcher 日志、stdout/stderr、appfile、cassette、模型请求/响应、prompt、Docker 全量日志。
- 整个 `local/` 目录、环境变量 dump、`/proc` dump、完整 `git diff`（未审核的）。源码 patch 只附与本实验相关的改动，逐段人工脱敏；不要用 `git diff` 批量打包，也不要把 `local/` 或秘密文件混进去。
- 二进制、图片、压缩包、base64、SQLite 数据库、cassette 文件本身。
- 真实主机名、用户名、绝对路径、IP/域名、实例 ID、endpoint、NFS server/export、bucket/tenant。

## 7. 可比性与数据选择

- 盘点阶段选每个语义组**最近的完整可对比组**，回传该组所有 target、repeat、逐样本数据及失败/缺失清单；不要挑最快/最好的一次，不要只给中位数/均值，不要用图反推。
- 结果包必须保留 `config.json` 和所有根层 CSV；`summary.csv`、聚合表、图片不能代替逐样本数据。
- 盘点/导出时仍在写、大小或 mtime 还在变化的结果，标 `partial`/`unknown`，不进入“完整可对比组”；先记录快照边界，等停写后再取。不要为了稳定快照而 kill 进程、卸载/重挂或清理。
- 可比性至少检查：`benchmark`、`source_sha256`、实际 `config`（除计划变化的键）、工具/二进制身份、task/cassette/cohort、计时边界、缓存策略、持久化/验收语义、挂载/文件系统语义、机器类别与 placement。任何一项不同就是不同组；不要混池。
- `plot` 的 config/source 检查是必要条件，不是充分条件；语义边界不同时，即使 `plot` 允许 `--vary` 也要人工拆开。
- 保留部分 run 与失败样本的清单及原始数据。一般 `plot` 按各自 run 的已完成 target 过滤；正式 replay 配对报告要求完整 cohort。不能把失败目标残留行当有效样本，也不能把有成功 target 的部分 run 一律伪装成全失败。
- 旧数据放 `legacy/`，默认不进入论文；需要用时先人工确认其边界和字段含义。

主机侧的现有命令原样复用，不新增绘图路径：

```sh
# 回放配对报告（示例；按导入后的实际目录替换）
python3 -m agent_fs_bench.replay_report \
  "$AFB_WORK/results-.../results/agent-replay/<run-a>" \
  "$AFB_WORK/results-.../results/agent-replay/<run-b>" \
  --reference ext4

# 论文图（示例；按实际 run 和维度替换）
uv run python -m agent_fs_bench plot \
  "$AFB_WORK/results-.../results/<benchmark>/<run-id>" \
  --targets fs-a,fs-b
```

测试机不画图、不生成论文图、不用统计摘要替代原始数值；图只在主机导入并验收后生成。

## 8. 批次、大小与失败处理

- 每份 packet 是一份 Markdown；`export` 打印 `document_bytes`。上限未知时先按整 run/整组切批，保证每个 run 的 `config.json` + 全部 CSV 在同一份文档里。
- 太大时缩小**下一批**：先保留全量清单和精确字节数，再按语义组/run 分批；不要截断文件、删行、抽样、用汇总替代，或把一个 run 拆到两个 packet。
- 单份文档必须是完整文本；不要用归档/base64/附件绕过大小限制。若单个 run 本身就超过可用上限，停下来报告精确大小，不要切行。
- 导出失败：修 staging（补文件、改路径、重做脱敏），用新的输出文件（必要时新 batch）重导；不要编辑已生成的 packet。
- 导入校验失败：不落盘，取得完整源文档或重新导出；不绕过校验。写入错误：停止并报告已有部分目录，下一次使用全新目标目录。
- 实验执行失败：fail-fast，记录失败/偏离和影响的数据；不重试、不 fallback、不替换样本。传输失败不等于实验失败，二者分开记录。

## 9. 导出前 / 导入后检查清单

导出前：

- [ ] `STAGE` 根目录只有 `plan.md`、`execution.md`；`plan.md` 是冻结原文，`execution.md` 有实际执行/偏离/失败。
- [ ] `results/` 每个 run 有 `config.json` + 全部根层 CSV；没有 target 子目录、日志、appfile；声明表齐全；失败 target 的 `planned_targets`/`targets` 原样保留。
- [ ] 旧数据在 `legacy/`，没有补字段冒充新版；kind 为 inventory 时至少有一个 `inventory/` 文件。
- [ ] 每个文件是 UTF-8/LF（或已由工具归一化）、无 CR/NUL、无隐藏/符号链接/非法路径。
- [ ] 主机名、node/placement 键、CSV `host`、target 路径、workload 路径、launcher env/args、clone instance/endpoint、replay container path 已按第 6 节处理；同值同别名、异值异别名、跨批次稳定。
- [ ] 原始 source/binary/task/cassette 哈希未被重算冒充；脱敏副本已标注。
- [ ] 没有秘密、原始日志、cassette、模型响应、整个 `local/`、环境 dump、二进制/归档/base64。
- [ ] 人工审核人/日期/别名账本版本已写进 `execution.md`；使用 `--reviewed`；输出在 staging 之外且不存在。导出后将 `document_bytes` 记在测试机本地批次记录，不能写回本包的 `execution.md` 再冒用原 manifest。

导入后：

- [ ] 输出目录是新建的，`exchange.json` 存在，`packet_sha256` 与本批记录一致。
- [ ] stdout 的 `batch`/`kind`/`files`/`output` 符合预期；文件树与 manifest 一致。
- [ ] 抽查 `config.json` 的 required 字段、表头、target 集合、行数；失败/缺失清单与 `execution.md` 一致。
- [ ] 没有执行 packet 内任何文件；没有把 `legacy/` 默认送入论文分析。
- [ ] 确认可比性后再跑 `replay_report`/`plot`；不跨测量边界混池。
