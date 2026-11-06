# agent-fs-bench

面向 agent 工作负载的文件系统科研 benchmark。**常规 workload 输入为已挂载目录；`mount-scale` 另支持配置真实挂载命令。唯一原始结果是 JSONL；每次运行默认生成派生汇总 CSV，图表按需生成。**

正式运行环境为 Linux、Python 3.12+。不包含内部文件系统实现或凭据；本地盘、FUSE、NFS 使用同一个目录接口。除 `mount-scale` 的显式挂载实验外，benchmark 不负责挂载、卸载；不自动安装、重试或迁移旧结果。出错即停止，修正实验环境后重测。KISS：公开 CLI 只选择“测什么、在哪测”，实验默认值集中在各 workload 的 `CONFIG` 常量中。

## 结构

```text
agent_fs_bench/
  cli.py                  # run 入口；目标、运行目录、默认 CSV 汇总
  analysis.py             # DuckDB 汇总、查询与绘图
  results.py              # JSONL 运行上下文与复现信息
  targets.py              # 目录列表与别名
  ssh.py                  # 共享 SSH hosts 配置与 Open MPI remote-shell agent
  benchmarks/             # 9 个 workload + 顶部 CONFIG；SQLite 共用构建机制
experiments/              # 不含内部信息的 TOML 示例
local/                    # 私有目标、mount 与 SSH 配置，不提交
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
| `run` | 在一个或多个目标上顺序测量，随后生成汇总 CSV | `benchmark`、`--dirs`、`--target` |
| `analyze` | 对任意 JSONL 执行 DuckDB 查询并输出 CSV/stdout | `inputs`、`--sql`、`--database`、`--output` |
| `plot` | 从任意 JSONL 按论文图规范绘制分组箱线图或数值横轴曲线，输出 PDF/PNG 及自包含脚本 | `inputs`、`--benchmark`、`--metric`、`--cases`、`--targets`、`--parameters`、`--source-sha256`、`--ylabel`、`--size`、`--output` |
| `tools/gen_mini_source_repo.py` | 确定性重建 mini 的 Git fixture | `--force` |

`run` 的 workload 参数为 `clone --smoke`、`mount-scale --directory-smoke`、`mdtest --smoke`；其余实验设置在各模块顶部 `CONFIG` 中，修改代码属于实验配置变更，不得提交私有值。

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

`mount-scale` 例外：target 是各机相同绝对路径的本地 staging 根目录；`local/mounts.toml` 指定待测卷的 admin 视图与 mount／umount 命令。先填满当前 Linux 控制机的可用逻辑核，再按 `local/hosts.toml` 顺序扩展；使用全局 Open MPI/mdtest 作业，详见 [mount scaling](docs/bench-mount-scale.md)。`--directory-smoke` 仅在本机使用普通目录，不读 hosts/profile，不执行挂载。

```sh
# 默认读取 local/targets.toml，按顺序测量全部目标
uv run python -m agent_fs_bench run clone -- --smoke

# 只测某个已注册别名
uv run python -m agent_fs_bench run clone --target fs-a -- --smoke

# 不使用注册文件，直接用目录列表（别名取末级目录名，必须唯一）
uv run python -m agent_fs_bench run clone --dirs /mnt/fs-a /mnt/fs-b -- --smoke
```

结果固定写入仓库 `results/<bench>/<run-id>/`，没有重定向参数。常规 workload 工作数据只写在 `<target>/agent-fs-bench/<bench>/<run-id>/`；`mount-scale` 的 mount points 在此，本次卷内数据另放在 admin 视图的同名 run 目录。不会清空目标根目录。不同目标顺序运行；bench 内并发 workload 不变。

## Benchmark 命令

| 命令 | 工作负载 | 文档 |
| --- | --- | --- |
| `run dev-workflow` | Linux 内核配置、编译、搜索、修改、提交、树验证；固定 x86 配置输入 | [dev workflow](docs/bench-dev-workflow.md) |
| `run mini-dev-workflow` | 固定小型 C 项目的完整 Git worktree 开发流程 | [dev workflow](docs/bench-dev-workflow.md) |
| `run npm-install` | 外部本地盘上预热源、冷 `node_modules` 的离线 npm/pnpm 物化；按外部并发批次计时，固定 lockfile fixture | [npm install](docs/bench-npm-install.md) |
| `run npm-install-nofsync` | 同一 workload，但 harness 不做任何 fsync/`os.sync()`；只报告 install 计时、吞吐与树统计，无 flush/durable 指标 | [npm install](docs/bench-npm-install.md#no-fsync-variant) |
| `run clone` | 同文件系统持久化目录克隆；完整目录请求并发、数据量/文件数扫描，支持按目标选择原生命令 | [clone](docs/bench-clone.md) |
| `run kvtest` | SQLite 官方单写者随机 BLOB 覆写；逐次提交或整批事务 | [kvtest 与负载校准](docs/bench-kvtest-openclaw.md) |
| `run sqlite-speedtest` | SQLite 官方 `speedtest1 main`，单连接 SQL 阶段及完整进程计时，无 Agent 假设 | [SQLite SQL microbenchmark](docs/bench-sqlite-speedtest.md) |
| `run multi-host` | 多台 SSH 主机同时在共享文件系统执行私有树操作和目录竞争 | [multi-host](docs/bench-multi-host-nas.md) |
| `run mount-scale` | 本机优先、按逻辑核扩至 SSH hosts 的 N 个独立 client；单点 N、全局 Open MPI/mdtest 小文件吞吐、mount lifecycle 与逐机资源 | [mount scaling](docs/bench-mount-scale.md) |
| `run mdtest` | 单客户端 POSIX 元数据吞吐；固定总对象数，private/shared 目录与进程数扫描 | [mdtest](docs/bench-mdtest.md) |

```sh
uv run python -m agent_fs_bench run dev-workflow --dirs /mnt/fs-a
uv run python -m agent_fs_bench run mini-dev-workflow --dirs /mnt/fs-a
uv run python -m agent_fs_bench run npm-install --dirs /mnt/fs-a
uv run python -m agent_fs_bench run npm-install-nofsync --dirs /mnt/fs-a
uv run python -m agent_fs_bench run clone --dirs /mnt/fs-a -- --smoke
uv run python -m agent_fs_bench run kvtest --dirs /mnt/fs-a
uv run python -m agent_fs_bench run sqlite-speedtest --dirs /mnt/fs-a
uv run python -m agent_fs_bench run multi-host --dirs /mnt/fs-a
uv run python -m agent_fs_bench run mount-scale --target lakebase
uv run python -m agent_fs_bench run mdtest --dirs /mnt/fs-a -- --smoke
```

### 运行默认值

`run` 不暴露规模、重复次数、并发、fsync/cache、PRAGMA、fixture/manager、资源路径等调节参数。每个模块顶部有唯一的 `CONFIG` 字典，记录该 workload 的可复现实验设置；具体默认值和计时含义见对应文档。修改 `CONFIG` 并在结果中保留源码指纹，才能说明实验条件。部分 workload 会强制校验实验条件：npm-install 要求 node/npm/pnpm 版本与 `CONFIG["tool_versions"]` 完全一致，并要求外部本地准备盘与 target 不同设备、不同文件系统类型，不满足即中止，不回退。

`multi-host` 与 `mount-scale` 复用 `local/hosts.toml` 的 SSH 配置，格式见 `experiments/hosts.example.toml`。前者在每台 SSH host 跑一个共享写 worker，控制机不跑负载；后者先在 Linux 控制机每个可用逻辑核放一个 client，再按 hosts 顺序填远端，一次只测一个 N（默认全体可用逻辑核总数）。两者的同步和计量协议不同，结果不能直接拼接。

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

每行对应一个 case / repeat；完整开发流程是一条测量，步骤在 `details` 中。多机以 host / phase / round 拆分。npm-install 每行是一个 manager × 外部并发批次大小 × 一个批次（`case` 形如 `npm/c1`、`pnpm/c2`），批次内每个 worker 的原始样本放在 `details.tasks` 中，指标是批次聚合值。公共字段：

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
target,case,metric,samples,mean,median,minimum,maximum,stddev,benchmark,run_ids,parameters,source_sha256
```

一行是 `benchmark × target.name × case × metric × parameters × source_sha256` 的统计。clone 的汇总系列为 `target.name/copier`，区分同一文件系统上的原生 clone 与 `cp-r`；其他 benchmark 仍使用 `target.name`。多次运行的样本合并；`run_ids` 按字典序列出参与该行统计的去重运行标识，以分号分隔。`parameters` 是该行参数对象的规范化 JSON（键递归排序、无空格），`source_sha256` 是源码指纹；两者都是分组键，因此不同实验配置或不同源码不会被静默合并到同一个统计里，而同一实验在不同 run ID 中的重复可以聚合。导出在计时之外执行；导出失败返回非零并保留 JSONL，不重跑 benchmark。

参数键顺序不影响分组，但参数内容会：改变 `CONFIG`、工具版本或固定输入后，结果自然落到另一组。要比较两组，就在分析时显式选择其中一组。

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

# measurements 保留嵌套 JSON；metrics 是数值长表（含 parameters_canonical）
uv run python -m agent_fs_bench analyze 'results/*/*/results.jsonl' \
  --sql 'SELECT benchmark, "case", metric, value FROM metrics'

# 列出每个 case 实际出现的配置身份，供绘图选择器使用
uv run python -m agent_fs_bench analyze 'results/*/*/results.jsonl' \
  --sql 'SELECT DISTINCT benchmark, "case", parameters_canonical, source_sha256 FROM metrics ORDER BY 1,2'

# 写出 results/clone.pdf、clone.png（600 dpi）和内嵌数据的 clone.py
uv run --group analysis python -m agent_fs_bench plot \
  'results/clone/*/results.jsonl' \
  --benchmark clone --metric mib_s --ylabel 'Throughput (MiB/s)' \
  --cases L256M/C1/cp,L1G/C1/cp --targets ours,ext4,nfs --output results/clone

# 只画某一个配置身份（参数取 summary.csv 的 parameters 列原文）
PARAMS='<parameters column from summary.csv>'
uv run --group analysis python -m agent_fs_bench plot \
  'results/npm-install/*/results.jsonl' \
  --benchmark npm-install --metric durable_mean_s \
  --cases npm/c1 --targets fs-a,fs-b \
  --parameters "$PARAMS" --source-sha256 <hash> \
  --ylabel 'Durable install (s)' --output results/npm-latency
```

mdtest 的并发图直接使用原始 JSONL；每个操作和目录布局输出一张中位数/IQR 曲线：

```sh
uv run --group analysis python -m agent_fs_bench plot \
  'results/mdtest/<run-id>/results.jsonl' \
  --benchmark mdtest --metric ops_per_s \
  --targets ours,nfs,ext4 --output results/mdtest-metadata
```

横轴为单客户端进程数，纵轴为 Kops/s，例如输出 `mdtest-metadata-file-create-private.pdf`，并附 PNG 和自包含脚本。绘图要求所选目标覆盖相同进程数，且总对象数、mdtest 版本、缓存及完成口径相同；不把这些吞吐样本解释为逐操作延迟。其余实验条件由人工保证一致。

CSV 是可随时重新生成的派生文件；DuckDB 是派生缓存，不维护数据库版本。默认分类箱线图将所选输入中的跨 run 样本合并到目标 × case，只有 `parameters` 与 `source_sha256` 完全一致的行才会进入同一个箱子；同一 case 内出现多个配置身份时，`plot` 直接报错而不是静默合并，报错会列出找到的身份，并提示用 `--parameters <json>` 和/或 `--source-sha256 <hash>[,<hash>]` 选择。clone 自动按数据量、文件数和两类并发场景生成数值横轴折线图，以 `target/copier` 为系列，延迟直接汇集原始请求样本、显示中位数和 IQR；每条扫描线同样只允许一个 `(parameters, source_sha256)` 身份，混合时报错并列出找到的候选。mdtest 的每个操作/布局曲线同样只接受一组 `(parameters, source_sha256)`，`--parameters` 与 `--source-sha256` 直接过滤其输入行。

- `--parameters '<json object>'`：只保留参数规范化后与该对象**完全相等**的行。键顺序与空白无关，但必须是 workload 记录的完整对象（含 `tool_versions`、`seed_input_sha256` 等）；不支持部分匹配。
- `--source-sha256 <hash>[,<hash>…]`：只保留所列源码指纹的行。
- 两个选择器都可与 `--targets`/`--cases` 组合；先按选择器过滤，再按 case/target 选择，所以被 `--cases`/`--targets` 排除的行不会触发歧义错误。不同 case 可以携带不同配置（例如不同并发批次大小），这不算歧义。
- 选择器过滤后没有剩余行时报错 `no measurements match the plot selection`（clone 为 `no clone measurements match the plot selection`，mdtest 为 `no mdtest measurements match the plot selection`），并附上生效的选择器。

`parameters_canonical` 是 `metrics` 视图中的规范化参数字符串列，持久化的 DuckDB 分析库也保存该列，重新打开时不需要额外函数。

图样式遵循 `.agents/skills/paper-figure-style/SKILL.md`（Arial 9pt、矢量 PDF、图例在上、主系统首位用首色、hatch/marker 区分、四边框、无网格），共享实现在 `agent_fs_bench/figure.py`。`--targets` 决定图例顺序，第一个为主系统；clone 使用 `target/copier` 系列名，其他图直接显示 target 别名。`--cases` 决定分类图的 x 轴顺序并筛选 case，clone 数值扫描只筛选、仍按横轴数值排序。`--ylabel` 默认为 metric 名（clone 常用指标自带易读标签）。`--size` 按实验设定英寸尺寸，默认单栏 `3.5 1.5`。clone 为各扫描输出 `<stem>-bytes`、`<stem>-files`、`<stem>-concurrency-large`、`<stem>-concurrency-small`，只输出输入中存在的组；每组的 Python 脚本内含原始样本与绘图代码，无需 JSONL 即可重新生成 PDF/PNG。图中文字均为英文，缺少 Arial 时直接报错，不替换字体。

## 可复现性与资源边界

- 保留运行所用 checkout、`uv.lock`、私有目标/主机配置及导出的 JSONL。源码指纹识别实际源码和固定输入，包括未提交修改，但不替代源码归档。
- 跨文件系统、跨 run 比较的 workload 参数、工具版本、内核/Python、挂载和构建环境由人工保证一致。默认汇总按 `parameters` 与 `source_sha256` 分组，能防止不同配置或不同源码被静默合并，但目标路径、主机环境与挂载条件不在分组键里。
- 不宣称 cold-cache：page cache、FUSE 客户端缓存和服务端缓存是不同层；保持原 bench 的计时、同步与 cache 语义。个别 workload 的计时边界和持久化协议（例如 npm-install 的全 project flush）以各自文档为准，不能当作被调用工具的默认语义。
- 本次不实现 cgroup，也不放空插件接口。FUSE 客户端、服务端和 benchmark 的资源限制由外部实验脚本设置；限制客户端时应控制真实客户端进程所在 cgroup，并保存这些私有条件。
- smoke 只证明命令、测量和分析链路可运行，不代表真实 FUSE/NFS 性能或一致性验证。

共享边界测试：

```sh
uv run python -m unittest discover -s tests
```

## Fixtures 与来源

`data/dev-workflow/` 与 `data/mini-dev-workflow/` 来自 [YoloFS/perf-eval](https://github.com/YoloFS/perf-eval) 的 `bc32d08`（2026-08-28）；对应最后变更为 `de6f95f` 和 `afe112a`。内核 patchset 是 Amir Goldstein 的 overlayfs `ovl_file` 重构，已合入 Linux v6.13。

例外是 `data/dev-workflow/tinyconfig-overlayfs.config`：它在固定 base commit（`c2c54b5f34f6^`，Linux 6.12.0-rc7）上按 `make ARCH=x86 tinyconfig` + `CONFIG_64BIT=y` + `CONFIG_OVERLAY_FS=y` + `make ARCH=x86 olddefconfig` 重新生成，是 64-bit x86 配置，与上游同名文件不再字节一致。series 与命令文件仍取自上游。

两个开发流程按上游继承构建环境，不强制 `ARCH`、不将环境 `CC` 提升为 make 命令行参数。只记录必要 workload 参数、工具版本和输入指纹，不再采集完整内核配置或 make 环境字典。固定配置文件输入不等于强制相同的最终内核配置，实验条件由人工保证，详见开发流程文档。

mini 的 source repository 从提交的命令确定性重建；bundle 与 `series.json` 必须一起更新，不能只复制最终工作树：

```sh
uv run python tools/gen_mini_source_repo.py --force
```

`data/pnpm-bench*` 来自 [pnpm/pnpm](https://github.com/pnpm/pnpm) 的 `5bd38b19`（2026-11-03）。npm lockfile 由同一 manifest 执行 `npm install --package-lock-only --legacy-peer-deps --ignore-scripts --cache <tmp>` 生成，不是上游 pnpm lockfile 的转换。npm-install 只使用这两个已提交 fixture。

两个 SQLite workload 使用固定版本的官方源代码，构建版本与哈希写入结果，NAS 默认统一为 DELETE + FULL、单连接、5 次重复。`sqlite-speedtest` 运行通用 SQL 测试，不含 Agent 假设；`kvtest` 提供固定 BLOB 的事务粒度对照。OpenClaw 仅作研究动机，不声称重放或推算端到端延迟。计时边界、NAS 限制与论文图建议见 [SQLite SQL microbenchmark](docs/bench-sqlite-speedtest.md)，应用依据见 [kvtest 文档](docs/bench-kvtest-openclaw.md#versioned-openclaw-evidence-and-limits)。

上游 YoloFS/perf-eval 当前未提供 LICENSE，公开分发 fixtures 前仍需确认授权；本次整理不改变授权状态。Linux 历史代码为 GPL-2.0。
