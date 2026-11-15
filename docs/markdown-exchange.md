# Markdown 单文档实验结果交换

代码、方案和站点配置通过 GitHub 传递；测试机上的原始数据留在原位。只有**核实后的提取结果**通过一份 Markdown 回传，写入机导入后分析和画图。工具不执行命令、不接触原始样本、不重新计算统计量。

## 1. 什么允许过通道

| 内容 | 通道 | 说明 |
| --- | --- | --- |
| 源码、分支、测试方案、模板 | GitHub | 测试机按固定 commit 拉取；方案文件本身就是评审记录 |
| 挂载、主机、凭证、任务清单等站点配置 | 测试机本地 | 只有脱敏后的语义进入方案或摘要元数据；真实值不离开测试机 |
| 原始逐样本 CSV、日志、cassette、模型响应 | 留在测试机 | **不得**放入本通道；需要重算时在测试机重算 |
| 每组的样本数、中位数、四分位数、失败计数、公式、来源 run | 一份 Markdown | 论文与审计所需的全部数值都在这里 |
| 主机侧的分析脚本与图 | 写入机 | 只从导入的摘要读取；测试机不画论文图 |

```mermaid
flowchart LR
    G[GitHub: 代码与方案] --> T[测试机固定 commit]
    T --> R[原始结果留在测试机]
    R --> S[测试机提取并核实摘要]
    S --> M[一份 Markdown]
    M --> I[写入机校验导入]
    I --> F[本机画图与论文使用]
```

不存在“把整棵 results 目录贴出来”的出口。`exchange` 只接受 `summary/` 下的固定格式。

## 2. 提取摘要的规则

每个**论文比较组**生成一个摘要组，例如“小文件读扩展性”、“Clone 文件数扫描”、“Qwen/Codex/SWE-bench 配对回放”。组内每一行是一个独立的图点或柱：

- `series`：图例项（系统、后端或 copier），只在组内唯一命名。
- `x`：横轴取值；折线图用数值字符串（如 `64`），柱状图用 `all`。
- `metric`：指标名，同一组内保持一致（如 `aggregate_files_per_s`、`latency_s`、`paired_tool_time_ratio_to_ext4`）。
- `median`、`q1`、`q3`：在**预先声明的聚合单位**上计算的分位数；必须满足 `q1 <= median <= q3`。
- `n`：支撑该行的独立观测数。`n=1` 合法，但论文中要按单样本处理，不得画出虚假误差条。

约束：

1. 先冻结公式，再看结果。公式写进 JSON 的 `formula`，例如“每 repeat 的批吞吐，取 repeat 中位数与四分位数；失败 repeat 不进入 n”。不得在看到结果后改聚合口径。
2. 分位数与 `n` 必须来自同一层样本。不要把任务级中位数再当样本算四分位数，除非公式明确如此。
3. 失败、超时、发散、缺失 capture、未完成 target **不进入** `n`，也不能补零；它们进入 `summary/failures.csv`。
4. 不能只回传“最好的一组”或只回传中位数。每个计划 run 的每个图点都要有行；确实没有观测就写失败账目并在 `execution.md` 说明。
5. 原始逐样本数据保留在测试机，直到论文归档；摘要必须能由它重算。`execution.md` 记录提取命令、脚本路径别名和原始结果指纹。
6. 不同计时、缓存、持久化、验收或 cohort 语义的 run 不得放进同一组；需要分组的先拆组。

## 3. Staging 布局

```text
STAGE/
  plan.md                    # 冻结方案原文
  execution.md               # 实际执行、偏离、失败、提取与脱敏记录
  summary/
    <group>.csv              # 摘要数据
    <group>.json             # 轴标签、系列顺序、来源 run、公式
    failures.csv             # 失败/缺失账目；只有表头也表示“无失败”
```

- 根目录**只能**有 `plan.md`、`execution.md`。
- `<group>` 是 ASCII token：`[A-Za-z0-9][A-Za-z0-9_.-]*`。
- 不允许子目录、隐藏文件、符号链接、绝对路径、`..`、空格或中文文件名。
- `summary/` 之外的内容一律拒绝，错误信息会指出“Raw results stay on the test machine”。

### 3.1 摘要 CSV

固定表头，顺序不可变：

```csv
series,x,metric,median,q1,q3,n
Apsara,64,aggregate_files_per_s,18200,17600,19000,3
NAS,64,aggregate_files_per_s,9100,8800,9600,3
Apsara,128,aggregate_files_per_s,30100,28700,31500,3
NAS,128,aggregate_files_per_s,9300,8900,9700,3
```

- 每一行 `(series, x, metric)` 唯一。
- 数值必须是有限数；`n` 必须是正整数的规范文本（`01` 不接受）。
- `x` 保留原始标签或数值文本，不在这里做单位换算；单位写在 JSON 的 `unit`/`ylabel`。

### 3.2 摘要 JSON

每个 `<group>.csv` 恰有一个同名 JSON，键集合固定：

```json
{
  "xlabel": "Agents",
  "ylabel": "Throughput (Kfiles/s)",
  "unit": "Kfiles/s",
  "logx": true,
  "logy": false,
  "series_order": ["Apsara", "NAS"],
  "source_runs": ["run-apsara-n64", "run-nas-n64"],
  "formula": "每个 repeat 的批吞吐；取 repeat 的中位数和四分位数；失败 repeat 不进入 n"
}
```

- `series_order` 必须等于 CSV 中系列首次出现的顺序，也就是图例顺序；主系统排第一。
- `source_runs` 是稳定别名，必须在 `execution.md` 的 run 索引中有定义；不能为空。
- `formula` 必须能让人从原始数据独立重算；不要写“见附件”。
- 图的具体尺寸、面板布局仍由写入机的自包含绘图脚本决定，不进入摘要。

### 3.3 失败账目

```csv
group,series,label,count
replay,Apsara,timeout,2
replay,Apsara,divergence,1
replay,NAS,capture_missing,3
```

- `group`/`series` 必须存在于摘要组；`label` 是单一 ASCII token（如 `timeout`、`divergence`、`error`、`capture_missing`、`incomplete`）。
- `count` 是规范的非负整数；同一 `(group, series, label)` 只出现一次。
- 即使没有任何失败，也要有表头，禁止用“未观察到失败”的口头声明代替。
- 失败账目是**样本数之外的**计划观测；不得把失败数加进摘要分母来制造成功表。

## 4. 导出 / 导入

在测试机的仓库根运行；纯 stdlib，不需要 `uv` 或额外依赖：

```sh
python3 -m agent_fs_bench.exchange export \
  --batch results-20261116-01 \
  --root "$AFB_STAGE" \
  --output "$AFB_OUT/results-20261116-01.md" \
  --reviewed
```

- `--reviewed` 是人工确认：staging 每个文件都已检查，没有私有主机、路径、端点或凭证。没有该参数不导出。
- 输出必须不存在、在 staging 之外；导出不覆盖。失败不写 packet。
- stdout 给出 `files` 和 `document_bytes`；后者是整份 Markdown 的 UTF-8 字节数，用于判断是否需要把**另一个比较组**放到下一份文档。当前组绝不截断、抽样或拆行。
- 一份文档可以包含多个摘要组；每份文档都是一个完整批次。

写入机导入到全新目录：

```sh
python3 -m agent_fs_bench.exchange import \
  "$AFB_IN/results-20261116-01.md" \
  --output "$AFB_WORK/results-20261116-01"
```

- 输出目录必须不存在；导入不覆盖、不合并。
- 全部校验在落盘前完成：UTF-8、CR/NUL、路径、重复块、manifest 字节数/SHA-256、CSV 表头与数值、分位数顺序、`n`、系列顺序、失败账目引用、来源 run 格式。
- 成功后写 `exchange.json`（含 manifest 与 `packet_sha256`）。失败不创建目录；文件系统写入错误可能留下部分目录，停止并换新目录重来，不手工合并。
- 复制时只取 Markdown 源文本；不依赖渲染后的表格。若文档被截断、改写或重复粘贴，哈希校验会拒绝。

## 5. 脱敏

摘要已经去掉了逐样本数据，但元数据、标签和一个示例配置仍可能泄露私有信息：

- 主机名、节点名、target 路径、挂载源、实例 ID、端点、endpoint、账号、密钥、bucket/tenant 一律不出现。
- 用稳定别名：`machine-01`、`path-fs-a`、`endpoint-a`、`instance-a`、`run-apsara-n64`。同值同别名、异值异别名；映射只存测试机本地。
- 保留有科学含义的内容：CPU 型号/核数、FS 类型、公开版本、挂载选项语义、原始 source/task/cassette 哈希、样本数与统计量。
- 不要为了“看起来一致”重算身份哈希；脱敏副本要标注，原始哈希原样记录。
- `plan.md`、`execution.md` 只摘录关键命令和 1–5 行脱敏错误证据；不放完整日志、环境 dump、`local/` 或凭证。
- 不依赖正则自动脱敏；逐文件人工审核后才使用 `--reviewed`。

## 6. 写入机分析与画图

导入后，论文数值和图形**只**从 `summary/` 读取：

1. 按 `source_runs` 和 `execution.md` 核对覆盖范围：计划中的每个 run/target 要么出现在摘要，要么出现在失败账目，不能两处都没有。
2. 检查公式与 `n` 是否符合预注册口径；不同口径组不合并。
3. 每个图由一个自包含脚本生成，读取对应 `<group>.csv` 的 `median/q1/q3/n`，按项目图规范输出矢量 PDF。误差条来自摘要的四分位数，不伪造原始分布。
4. 当前 `plot` 命令针对原始 CSV 的实时分析，只用于写入机本地或仍保留原始数据的机器；**不要**用它假装重算已回传的远端数据。

论文中的失败计数直接取 `failures.csv`；主文不得把失败 run 计入有效样本，也不得把“有效任务数 / 计划任务数”写成成功率之外的指标。

## 7. 交付检查

导出前：

- [ ] `plan.md` 是冻结原文；`execution.md` 记录实际命令、配置、偏离、失败和提取公式。
- [ ] 每个计划比较组都有 `<group>.csv` + `<group>.json`；每个计划 run/target 都在摘要或失败账目中。
- [ ] `q1 <= median <= q3`，`n >= 1`，行为有限数；系列顺序已定；`source_runs` 均可追溯。
- [ ] `failures.csv` 有表头，所有失败/超时/发散/缺失都已计数，没有补零或替换样本。
- [ ] 原始逐样本、日志、cassette、模型响应没有进入 staging；没有隐藏文件或符号链接。
- [ ] 主机/路径/端点/凭证已别名化或删除；人工审核完成；使用 `--reviewed`；已记录 `document_bytes`。

导入后：

- [ ] 输出目录新建，`exchange.json` 的 `packet_sha256` 与批次记录一致。
- [ ] 摘要行数、系列数、`n` 与 `execution.md` 核对；失败计数可回溯到源 run。
- [ ] 没有执行文档中的任何命令；没有把摘要当原始样本重算。
- [ ] 图脚本只读取导入的摘要；不同测量边界没有混入同一图。
