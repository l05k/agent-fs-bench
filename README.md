# agent-fs-bench

面向 agent 工作负载的文件系统科研 benchmark。**输入是已挂载的目录；唯一原始结果是 JSONL；每次运行默认生成派生汇总 CSV，图表按需生成。**

正式运行环境为 Linux、Python 3.12+。不包含内部文件系统实现、挂载命令或凭据；本地盘、FUSE、NFS 使用同一个目录接口。benchmark 不负责挂载、卸载、自动安装、重试或旧结果迁移。出错即停止，修正实验环境后重测。KISS：公开 CLI 只选择“测什么、在哪测”，实验默认值集中在各 workload 的 `CONFIG` 常量中。

## 结构

```text
agent_fs_bench/
  cli.py                  # run 入口；目标、运行目录、默认 CSV 汇总
  analysis.py             # DuckDB 汇总、查询与绘图
  results.py              # JSONL 运行上下文与复现信息
  targets.py              # 目录列表与别名
  benchmarks/             # 6 个 workload + 顶部 CONFIG
experiments/              # 不含内部信息的 TOML 示例
local/                    # 私有目标/SSH 配置，不提交
results/<bench>/<run-id>/ # results.jsonl、summary.csv 和诊断附件
data/                     # 固定输入、lockfile、Git bundle
docs/                     # 各 workload 的 CONFIG 默认值和计时边界
tools/                    # fixture 维护工具
tests/                    # 共享行为边界测试
```

部署整个 checkout（包括 `data/`），不再单独拷贝一个脚本。共享机制直接 import，不再生成或复制代码。

## 安装

```sh
uv sync --locked
uv run python -m agent_fs_bench --help

# 绘图才需要额外安装 Matplotlib；DuckDB 已包含在基础依赖中
uv sync --locked --group analysis
```

`uv.lock` 固定 Python 依赖；`.python-version` 固定解释器系列。`run` 和 `analyze` 需要 DuckDB，`uv sync --locked` 即会安装；绘图另用 `analysis` 依赖组。DuckDB 只在测量完成后读取 JSONL，不进入 workload 计时。多机远端 worker 仍只需 Python 标准库，DuckDB 安装在控制机。外部工具由实验环境预先准备，各 bench 文档列出要求。

## CLI 工具

| 工具 | 用途 | 公开参数 |
| --- | --- | --- |
| `run` | 在一个或多个已挂载目录上顺序测量，随后生成汇总 CSV | `benchmark`、`--dirs`、`--target` |
| `analyze` | 对任意 JSONL 执行 DuckDB 查询并输出 CSV/stdout | `inputs`、`--sql`、`--database`、`--output` |
| `plot` | 从任意 JSONL 按论文图规范绘制分组箱线图，输出 PDF/PNG 及自包含脚本 | `inputs`、`--benchmark`、`--metric`、`--cases`、`--targets`、`--ylabel`、`--size`、`--output` |
| `tools/gen_mini_source_repo.py` | 确定性重建 mini 的 Git fixture | `--force` |

`run` 的 workload 参数只剩 `clone --smoke`；其余实验设置在各模块顶部 `CONFIG` 中，修改代码属于实验配置变更，不得提交私有值。

### 目标目录

在仓库外完成挂载，复制 `experiments/targets.example.toml` 到固定的 `local/targets.toml`：

```toml
[[targets]]
name = "fs-a"
path = "/mnt/fs-a"

[[targets]]
name = "fs-b"
path = "/mnt/fs-b"
```

`name` 是结果和图表标签；简写 `path = "fs-a"` 表示 `/mnt/fs-a`。不会扫描 `/mnt`，不会创建缺失的目标根目录，也不会验证挂载来源；仅有一个本地空目录并不证明挂载成功。

```sh
# 默认读取 local/targets.toml，按顺序测量全部目标
uv run python -m agent_fs_bench run clone -- --smoke

# 只测某个已注册别名
uv run python -m agent_fs_bench run clone --target fs-a -- --smoke

# 不使用注册文件，直接用目录列表（别名取末级目录名，必须唯一）
uv run python -m agent_fs_bench run clone --dirs /mnt/fs-a /mnt/fs-b -- --smoke
```

结果固定写入仓库 `results/<bench>/<run-id>/`，没有重定向参数。目标工作数据只写在 `<target>/agent-fs-bench/<bench>/<run-id>/`，不会清空目标根目录。不同目标顺序运行；bench 内原有并发 workload 不变。

## Benchmark 命令

| 命令 | 工作负载 | 文档 |
| --- | --- | --- |
| `run dev-workflow` | Linux 内核配置、编译、搜索、修改、提交、树验证；固定 x86 配置输入 | [dev workflow](docs/bench-dev-workflow.md) |
| `run mini-dev-workflow` | 固定小型 C 项目的完整 Git worktree 开发流程 | [dev workflow](docs/bench-dev-workflow.md) |
| `run npm-install` | warm cache、cold `node_modules` 的离线 npm/pnpm 安装；固定 lockfile fixture | [npm install](docs/bench-npm-install.md) |
| `run clone` | 同文件系统树复制及并发复制，支持代码配置的外部 copier | [clone](docs/bench-clone.md) |
| `run kvtest` | SQLite 官方 kvtest，每次 BLOB 覆写一次提交 | [kvtest](docs/bench-kvtest-openclaw.md) |
| `run multi-host` | 多台 SSH 主机同时在共享文件系统执行私有树操作和目录竞争 | [multi-host](docs/bench-multi-host-nas.md) |

```sh
uv run python -m agent_fs_bench run dev-workflow --dirs /mnt/fs-a
uv run python -m agent_fs_bench run mini-dev-workflow --dirs /mnt/fs-a
uv run python -m agent_fs_bench run npm-install --dirs /mnt/fs-a
uv run python -m agent_fs_bench run clone --dirs /mnt/fs-a -- --smoke
uv run python -m agent_fs_bench run kvtest --dirs /mnt/fs-a
uv run python -m agent_fs_bench run multi-host --dirs /mnt/fs-a
```

### 运行默认值

`run` 不暴露规模、重复次数、并发、fsync/cache、PRAGMA、fixture/manager、资源路径等调节参数。每个模块顶部有唯一的 `CONFIG` 字典，记录该 workload 的可复现实验设置；具体默认值和计时含义见对应文档。修改 `CONFIG` 并在结果中保留源码指纹，才能说明实验条件。

多机模式固定读取 `local/hosts.toml`，格式见 `experiments/hosts.example.toml`。控制机无需挂载；所有 worker 必须已经以**相同绝对路径访问同一个共享文件系统**。不会尝试修复连接、重试或挂载。

## 结果

每次成功运行写出原始 JSONL，并默认生成同目录的汇总 CSV：

```text
results/
  clone/<run-id>/
    results.jsonl
    summary.csv
    fs-a/                 # 可选诊断附件
    fs-b/
  kvtest/<run-id>/
    results.jsonl
    summary.csv
```

每行对应一个 case / repeat；完整开发流程是一条测量，步骤在 `details` 中。多机以 host / phase / round 拆分。公共字段：

| 字段 | 含义 |
| --- | --- |
| `benchmark`, `run_id` | workload、UTC 微秒运行标识 |
| `target.name`, `target.path` | 稳定别名及实际目标目录 |
| `case`, `repeat` | case 与重复序号 |
| `parameters` | 必要 workload 参数、工具版本及输入/二进制指纹 |
| `source_sha256` | 实际 benchmark 源码与固定输入的指纹 |
| `metrics` | 扁平数值指标，指标名标明单位 |
| `details` | workload 专属实际值、校验结果、步骤或已有原始样本 |

JSONL 是唯一原始数据，可独立搬运；分析不需要原挂载、日志或工作目录。写入发生在对应测量计时之外；失败校验不输出成功测量。没有结果 schema 多版本、迁移层或重复报告。

每项信息只保留一处：数值指标放在 `metrics`，原始延迟、步骤耗时及必要校验放在 `details`。不嵌入日志、临时工作路径、完整 manifest、内核配置、构建环境字典或固定说明文字；不记录重复时间戳和参数哈希。环境和构建条件由实验者统一管理，不在 JSONL 中自动审计或隔离。

### 默认生成 summary.csv

```sh
uv run python -m agent_fs_bench run clone \
  --dirs /mnt/fs-a /mnt/fs-b -- --smoke
```

只有全部目标测量成功且 JSONL 关闭后，才自动生成 `results/<bench>/<run-id>/summary.csv`，无需开关。若后续目标失败，前面已完成的测量行仍保留在 JSONL，但不会生成自动汇总 CSV；这些行不代表完整实验。汇总与 `analyze` 共用 DuckDB 查询及 CSV 写出函数，不是第二套结果机制：

```text
target,case,metric,samples,mean,median,minimum,maximum,stddev,benchmark,run_ids
```

一行是 `benchmark × target.name × case × metric` 的全局统计。多次运行的样本合并；`run_ids` 按字典序列出参与该行统计的去重运行标识，以分号分隔。不同参数、源码、目标路径或机器环境不再自动拆组，实验者必须确保同一目标别名下的输入可比较。导出在计时之外执行；导出失败返回非零并保留 JSONL，不重跑 benchmark。

CSV 的用途是快速比较本次各目标、用表格工具检查统计量，以及直接交给只接受扁平表格的绘图或分析工具。它不是原始数据归档的替代品：保留 JSONL，CSV 丢失或需重新汇总时直接重建，不必重跑 benchmark。重新计算逐操作分位数、检查离群值或分析详细步骤时，应读取 JSONL。

## 分析与绘图

```sh
# 默认汇总输出 CSV 到 stdout；glob 加引号
uv run python -m agent_fs_bench analyze 'results/*/*/results.jsonl'

# 手动导出某个运行的汇总
uv run python -m agent_fs_bench analyze \
  'results/clone/<run-id>/results.jsonl' \
  --output 'results/clone/<run-id>/summary.csv'

# 重新生成可丢弃的 DuckDB 分析库
uv run python -m agent_fs_bench analyze 'results/*/*/results.jsonl' \
  --database results/analysis.duckdb

# measurements 保留嵌套 JSON；metrics 是数值长表
uv run python -m agent_fs_bench analyze 'results/*/*/results.jsonl' \
  --sql 'SELECT benchmark, "case", metric, value FROM metrics'

# 写出 results/clone.pdf、clone.png（600 dpi）和内嵌数据的 clone.py
uv run --group analysis python -m agent_fs_bench plot \
  'results/clone/*/results.jsonl' \
  --benchmark clone --metric mib_s --ylabel 'Throughput (MiB/s)' \
  --cases L256M/C1/cp,L1G/C1/cp --targets ours,ext4,nfs --output results/clone
```

CSV 是可随时重新生成的派生文件；DuckDB 是派生缓存，不维护数据库版本。箱线图与默认汇总采用相同的全局分组口径，将所选输入中的跨 run 样本合并到目标 × case 的箱子。分析前由人工保证运行环境、参数和目标别名一致；要分析某次运行，直接只传入该 run 的 JSONL，或使用自定义 SQL 按 `run_id` 筛选。

图样式遵循 `.agents/skills/paper-figure-style/SKILL.md`（Arial 9pt、矢量 PDF、图例在上、主系统首位用首色、hatch 区分、四边框、无网格），共享实现在 `agent_fs_bench/figure.py`。`--targets` 决定图例顺序，第一个为主系统；`--cases` 决定 x 轴顺序并筛选 case；两者未给出时取全部并按字母序。图例直接显示 target 别名，别名应取论文中的名称。`--ylabel` 默认为 metric 名。`--size` 按实验设定英寸尺寸，默认单栏 `3.5 1.5`。`<stem>.py` 内含原始样本与绘图代码，`python <stem>.py` 无需 JSONL 即可重新生成 PDF/PNG。图中文字均为英文，缺少 Arial 时直接报错，不替换字体。

## 可复现性与资源边界

- 保留运行所用 checkout、`uv.lock`、私有目标/主机配置及导出的 JSONL。源码指纹识别实际源码和固定输入，包括未提交修改，但不替代源码归档。
- 跨文件系统、跨 run 比较的 workload 参数、工具版本、内核/Python、挂载和构建环境由人工保证一致；默认全局汇总不会检查或拆分这些差异。
- 不宣称 cold-cache：page cache、FUSE 客户端缓存和服务端缓存是不同层；保持原 bench 的计时、同步与 cache 语义。
- 本次不实现 cgroup，也不放空插件接口。FUSE 客户端、服务端和 benchmark 的资源限制由外部实验脚本设置；限制客户端时应控制真实客户端进程所在 cgroup，并保存这些私有条件。
- smoke 只证明命令、测量和分析链路可运行，不代表真实 FUSE/NFS 性能或一致性验证。

共享边界测试：

```sh
uv run python -m unittest discover -s tests
```

## Fixtures 与来源

`data/dev-workflow/` 与 `data/mini-dev-workflow/` 来自 [YoloFS/perf-eval](https://github.com/YoloFS/perf-eval) 的 `bc32d08`（2026-08-28）；对应最后变更为 `de6f95f` 和 `afe112a`。内核 patchset 是 Amir Goldstein 的 overlayfs `ovl_file` 重构，已合入 Linux v6.13。

两个开发流程按上游继承构建环境，不强制 `ARCH`、不将环境 `CC` 提升为 make 命令行参数。只记录必要 workload 参数、工具版本和输入指纹，不再采集完整内核配置或 make 环境字典。固定配置文件输入不等于强制相同的最终内核配置，实验条件由人工保证，详见开发流程文档。

mini 的 source repository 从提交的命令确定性重建；bundle 与 `series.json` 必须一起更新，不能只复制最终工作树：

```sh
uv run python tools/gen_mini_source_repo.py --force
```

`data/pnpm-bench*` 来自 [pnpm/pnpm](https://github.com/pnpm/pnpm) 的 `5bd38b19`（2026-11-03）。npm lockfile 由同一 manifest 执行 `npm install --package-lock-only --legacy-peer-deps --ignore-scripts --cache <tmp>` 生成，不是上游 pnpm lockfile 的转换。npm-install 只使用这两个已提交 fixture。

kvtest 使用 SQLite 官方源代码，版本和哈希记录在结果中。

上游 YoloFS/perf-eval 当前未提供 LICENSE，公开分发 fixtures 前仍需确认授权；本次整理不改变授权状态。Linux 历史代码为 GPL-2.0。
