# Mount-scale：本机优先的跨机 client 扩展性

目的：固定每 client 的小文件工作量，在一个指定并发数 N 下，比较独立 mount 拓扑的文件操作吞吐、lifecycle 与逐机资源成本。使用**原版 IOR 4.0.0 mdtest + Open MPI 4.1.x**；它是 agent 文件操作的代理负载，不是应用端到端 agent benchmark。

入口：`agent_fs_bench/benchmarks/mount_scale.py`。`mount_scale_cluster.py` 负责 CPU 分配与本地/SSH lifecycle workers；`mount_scale_mdtest.py` 负责一个全局 MPI 作业的原生计时。已有 `multi-host` 的独立 Python worker/共享写竞争协议不参与这里的吞吐计算。

## 实验单元与放置

每 client = 一个单线程 mdtest rank。K 为每 client 的**总目录数**：1 个独立私有 workspace + K−1 个共享只读资源，默认 K=3。每个共享资源只有一份底层 fixture，所有 client 通过自己的别名访问。

| 拓扑 | 每 client 的实际 filesystem mount | N 个 client 的新增 mount 条目 |
| --- | --- | --- |
| `multi-mount` | 私有 workspace 1 个、共享资源各 1 个 | N×K |
| `shared-directory` | 独立 subdir mount 1 个，内部 LakeBase `--bind` 映射共享资源 | N |
| `directory-smoke` | 普通目录，不 mount | 不输出 mount 测量 |

`shared-directory` 不是所有 client 共用一个主机级 mount。LakeBase 内部 `--bind` 也不是 Linux bind mount。K=3 表示三目录；只有 `multi-mount` 可以写成“each client performs three filesystem mounts”。

**一次运行只测一个 N，没有默认扫描列表，不自动追加 N=1。** `CONFIG['clients']`：

- `None`（默认）：N 等于当前 Linux 控制机和 hosts 列表全部机器的可用逻辑 CPU 总数。
- 正整数：只测该 N。优先填满当前 Linux 控制机，再按 hosts 文件顺序填后续机器；达到 N 后不连接剩余机器。N 超过所探测总容量即报错，不超订阅、不缩小实验。
- “可用逻辑 CPU”取各 worker 的 `os.sched_getaffinity(0)`，不是物理核数。保留真实 CPU ID，按升序分配。rank wrapper 在执行 mdtest 前将 affinity 固定为一个 CPU，验证实际 hostname、global rank 和 affinity；实际放置与计划不符即失败。

默认“占满核”是每个可用逻辑 CPU 一个 rank，并不保证 CPU 利用率 100%；文件系统等待可使 CPU 闲置。不额外预留控制核，Python、MPI、内核和 filesystem daemon 仍与 ranks 共享主机 CPU。正式实验应统一 cpuset、SMT、NUMA、机型和后台负载。

例如本机可用 64 个逻辑 CPU、两台远端各 64 个：默认 N=192；指定 N=100 时分配为 local=64、第一台远端=36，第二台不参与。要画扩展曲线，实验者在独立 runs 中显式选择 N；本实现不决定论文扫描点。

## 依赖与网络

所有参与机器预装 Linux、Python 3.12+、相同架构下**相同 mdtest 二进制和 Open MPI 4.1.x 版本**。控制机另需仓库与 DuckDB。mdtest 必须链接这一套 Open MPI；其他 MPI 实现不回退兼容。benchmark 不自动安装工具、不 sudo、不重试。

原版 IOR 构建示例：

```sh
# 解压官方 ior-4.0.0.tar.gz，在源码目录中
./configure CC=mpicc
make -j
# 将 src/mdtest 和 Open MPI mpiexec 放进各机非交互 PATH
```

同一云内网的集群需要：

1. 控制机到 worker 的免交互 SSH，预先信任 host key。自定义 user、port、identity、ProxyJump 通过 hosts 配置提供。
2. 参与机器间双向 MPI TCP 可达，并能解析 MPI 使用的 hostname/地址。最简环境配置是只对实验机器的安全组成员或私有 IP 放通组内 TCP，同时检查 OS 防火墙；不开放公网。
3. Open MPI 的启动、barrier 和 rank 通信不经 benchmark 自建 SSH 隧道。只开放 TCP 22 不够。强制使用普通 TCP/同机共享内存，不要求 RDMA；不把 launcher 端口范围误当成所有 MPI 流量范围。
4. 多网卡机器需在 `CONFIG['mpi_launcher']` 中显式选择相互可达的 MPI 数据/控制接口，并归档实验配置。MPI 流量可与存储流量竞争，必须报告网络布局。

Open MPI 通过控制机上的 inventory-aware SSH agent 启动远端 daemon，关闭树状 SSH 启动；无需各 worker 保存其他 worker 的私钥。agent 使用已有 `agent_fs_bench/ssh.py` 的 hosts/SSH 规则。Open MPI 的 host token 必须是可达 hostname 或 IPv4 地址；不能只使用 SSH-only alias 冒充可解析的 MPI 地址。控制机 Python、checkout、inventory 路径不能含空白或冒号（Open MPI 4 对 `plm_rsh_agent` 的解析限制）。

## 配置与运行

实验参数集中在 `mount_scale.py` 顶部 CONFIG，不增加规模/容错 CLI：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `clients` | `None` | 单个 N；默认所有列出主机的可用逻辑 CPU 总数 |
| `hosts` | `local/hosts.toml` | 有序远端 SSH inventory；不含当前控制机 |
| `profiles` | `local/mounts.toml` | 目标挂载 profile |
| `directories` | `3` | K，包含私有 workspace；可为 1 |
| `files` | `1000` | 每 client 每目录的文件操作数 |
| `file_size_bytes` | `4096` | 每文件 create/read 字节数 |
| `repeats` | `3` | 同一 N 的完整重复次数 |
| `mdtest` | `mdtest` | 各机非交互 PATH 中的上游二进制，或统一绝对路径 |
| `mpi_launcher` | `('mpiexec',)` | Open MPI launcher；host/slot/appfile 由 harness 生成 |
| `connect_timeout_s` | `15` | SSH 连接超时 |
| `command_timeout_s` | `300` | mount/unmount 每条命令、MPI job 与 worker 控制操作的超时 |
| `idle_sample_s` | `1.0` | baseline/mounted/released 主机 idle 采样时长 |
| `resource_sample_s` | `0.1` | workload 主机资源采样周期 |

1000 个 4 KiB 文件只是轻量默认值。正式实验需使阶段时长明显超过 MPI barrier、定时器和调度开销，不得用极短 smoke 声称极限吞吐。

### SSH hosts

与 `multi-host` 共用 `experiments/hosts.example.toml` 的格式，但这里**只列远端机器，顺序就是填充顺序**：

```toml
[[hosts]]
name = "node-a"
address = "10.0.0.11"
user = "bench"
port = 22
python = "python3"
ssh_args = ["-i", "/home/operator/.ssh/bench_ed25519"]

[[hosts]]
name = "node-b"
address = "10.0.0.12"
user = "bench"
```

`name='local'` 保留给控制机。不得重复列出同一机器/地址；preflight 也会拒绝重复 execution hostname。`clients=None` 但只想占满本机时，显式提供 `hosts = []`。当显式 N 不超过本机容量，或运行 directory smoke 时，不读 hosts 文件；其他情况缺文件报错，不静默退回单机。

### Target 与 mount profile

target 是各机相同绝对路径的**本地 staging 根**，必须预先存在，且位于 admin 卷之外。结果仍写控制机仓库 `results/mount-scale/<run-id>/`。复制并修改 `experiments/mounts.example.toml` 到 `local/mounts.toml`；所有参与机器使用同一个目标 profile。控制机分发本次 Python 包和私有 profile 到远端 staging，无需远端 checkout；外部工具、mount 权限、admin mount 由实验环境预配。

`admin_root` 必须在每台机器同一路径预挂**同一卷的完整管理视图**。准备数据位于 `<admin_root>/agent-fs-bench/mount-scale/<run-id>/`；worker 读取本次随机 owner sentinel，拒绝 disconnected admin namespace。每次 repeat 的私有数据再按逻辑 host 名分开，避免跨机重名；各 host 上 agent 编号从 0 开始。

mount profile 的 argv 用 `format_map` 展开，不经 shell，保留既有占位符：

| 占位符 | 内容 |
| --- | --- |
| `{source}` | 相对 admin 卷根的 `/` 开头卷内路径 |
| `{mountpoint}` | 当前机器实际挂载点 |
| `{subdir}` | 私有卷内路径；multi-mount 为 workspace，shared-directory 为 agent 根 |
| `{binds}` | `共享源:agent/shared-1,...` 的卷内绝对路径映射 |
| `{agent}` | 当前 host 内从 0 开始的 client 编号 |

NFS profile 保留常规 cache 默认值，不暗加 `nosharecache`。N×K 个 mount entries 不等于同等数量的内核客户端/连接/cache。FUSE profile 必须确保每次 mount 创建真实 filesystem 客户端，不能用一个 mount 加 Linux bind 冒充多客户端。

mount 命令必须在挂载建立后返回，unmount 在释放后返回。共享负载只读，不证明强制权限隔离。K=1 时移除 shared-directory profile 中的空 `--bind` 参数。

```sh
uv run python -m agent_fs_bench run mount-scale --target nfs
uv run python -m agent_fs_bench run mount-scale --target lakebase

# 本机两 client、普通目录、32 文件、一次重复；不读 hosts/profile，不 mount
mkdir -p /tmp/mdtest-directory-smoke
uv run python -m agent_fs_bench run mount-scale \
  --dirs /tmp/mdtest-directory-smoke -- --directory-smoke
```

`--directory-smoke` 要求当前 affinity 集合至少有两个可用逻辑 CPU；不足时在启动 worker/MPI 前明确报错，不自动缩为 N=1。这里检查的是 `sched_getaffinity`，不是容器 CPU 时间配额。

## 执行与计时边界

1. 在参与机器启动 persistent lifecycle workers，探测 affinity CPU IDs、内核、架构、Python、mdtest SHA256、MPI 版本；生成本机优先的单点 N 放置。
2. 控制机一次性建立共享 fixture，用一个 mdtest rank 在 admin 视图 create。每次 repeat 各 host 建立自己的私有 workspace、sentinel 与挂载点，采样 baseline；这些准备不进 mount 命令计时。
3. 控制机并发通知各 host mount；每 host 并发启动本机 clients，每 client 顺序挂私有、共享目录。各 host 验证新增 mount entries 恰好符合计划、sentinel 别名正确。mounted-idle 在共享预热之前采样。
4. 对每个共享资源启动一个**全局 MPI 作业**做 stat/read 预热，丢弃该轮性能值。
5. 启动各机资源采样，然后启动全局私有 mdtest 作业 `create → stat → read → remove`；随后每共享资源各一个作业 `stat → read`。所有 ranks 在同一个 `MPI_COMM_WORLD` 中使用 mdtest 原有 phase barriers；不是每机独立跑完后相加吞吐。私有/共享、各共享资源仍分阶段，不是交错混合负载。
6. 每个 rank 通过 Open MPI appfile 明确指定 host，wrapper 验证 global rank、实际 hostname、单 CPU affinity，再 exec 上游 mdtest。控制机另外核对 proof 的实测 `affinity` 必须恰好等于请求的单 CPU，不只比较回显的 `cpu`。私有 `test-dir.0-0` 在所属 host 上预建，排除在计时外；只由 rank 0 把全局 native CSV 写到控制机结果目录，远端不要求共享结果盘。
7. 停止资源采样，各 host 并发逆序 unmount，校验无残留，采样 released-idle。成功后输出整次 repeat 的结果、删除已卸载私有数据；全部成功后收集 lifecycle 日志并删除本次共享 fixture 和远端部署。

采用 warm shared-data 协议，不 drop cache；预读不保证后续始终驻留 cache。私有 read 在本轮 create 后，没有 fsync/sync，不证明持久化延迟。mdtest wrapper/循环/barrier 在 upstream 阶段计时内；Python wrapper、MPI 启动、部署、fixture、预热、结果输出不在其中。

启用 `--warningAsErrors`。操作错误、rank/node 数或实际放置不符、版本/二进制不一致、缺失/nonfinite 计时都失败，不输出该 repeat 的成功吞吐。失败即停止并保留现场，不自动 unmount、retry、failover，也不把剩余 hosts 自动缩成较小 N。人工检查所有机器后再处理该 run。

## 结果与解释

唯一原始结果仍是 `results.jsonl`，默认生成派生 `summary.csv`。全局 workload case 例如 `multi-mount/N1024/private/create`；逐 host 生命周期/资源 case 在 `.../hosts/<name>/` 下。repeat 仅区分同一 N 的重复。

| 指标 | 定义 |
| --- | --- |
| `ops` | N×files |
| `elapsed_s` | upstream rank CSV all-row 阶段时间，含 phase-end barrier，不是子进程 wall time |
| `ops_per_s` | N×files / elapsed_s，不求和各机独立速率 |
| `ops_per_s_per_client` | 聚合吞吐/N，不是每 client 独立样本 |
| `rank_mean_us_per_op` | sum(rank_elapsed_s)/(N×files)×1e6；阶段均摊，不是逐操作 latency/p99 |
| `bytes`, `mib_per_s` | create/read 逻辑字节；共享数据被 N 次读取，不是物理网络/磁盘字节 |
| 逐 host `mount_elapsed_s`, `unmount_elapsed_s` | 本机全部 client lifecycle 命令的 wall time；details 保留逐 client 时间，不含别名/mountinfo 核验 |
| 全局 `controller_dispatch_elapsed_s` | 通知全部 workers 到收到全部回复，**包括控制通道、调度与 mount 核验**；不是纯 mount 时间，不是各机时间之和 |
| `measured_mounts`, `remaining_mounts` | 逐机核对后的新增/残留条目；全局行求和，不推算连接或 daemon 数 |

不再输出 `scaling_efficiency`：单点运行没有同轮 N=1 基线。论文若需要效率，在独立 runs 中选定可比基线再计算，不隐式增加实验点。

JSONL 保留解析后的 N、requested_clients、K、工作量、cache/fsync 协议、各机可用/分配 CPU、hostname/内核/架构/工具版本、mdtest 与 profile SHA256，以及公共 source SHA256。`parameters.planned_rank_placement` 是计划的 rank→逻辑 host→CPU 映射；`parameters.rank_placement` 按 `private`、`shared-1` 等测量作业分别保存通过校验的实测 rank、逻辑 host、hostname、CPU 和 affinity 列表。每轮重新汇总全部测量作业的 proofs，不能用最后一个作业覆盖前面的记录；K=1 仅有 `private`。这些值来自 wrapper 在 exec mdtest 前的观测，不代表整个阶段持续采样的 affinity。SSH transport 配置和 mount 凭据不写入 JSONL；实验者私下归档 inventory/profile。原始 placement proof、mdtest stdout/stderr、CSV/appfile 仍作为诊断附件，分析实际放置无需依赖这些附件或原机器。

资源来自**逐 host 全机计数**：CPU busy、MemTotal−MemAvailable、cache、dirty、slab 等。baseline/mounted-idle/released-idle 的 used/slab/unreclaimable/mount_entries 记录相对 baseline 的 delta；workload 记录观察到的峰值。包含 Python、MPI、daemon、内核及其他进程，不做 filesystem CPU/RSS 归因。采样窗口包含 MPI 启停及阶段间隙，各机开始/停止的控制时间有偏差；不把各机峰值相加声称同时全局峰值。mount entries 属于 worker 当前 namespace，daemon 必须在同一 namespace 建 mount。

## 论文结论边界

- 保持每 client 工作量不变，跨 runs 增加 N 才构成 weak scaling。增加机器同时增加客户端 CPU、内存和网络资源；必须报告放置、硬件及本机到跨机转折点，不能把差异完全归因为 mount 数。
- 同后端 `multi-mount` vs `shared-directory` 消融与跨 NAS 对比应分清；跨系统还混合服务端、cache、RPC、权限/一致性策略差异。
- 部分被测系统可能有 1000 mounts 上限；不硬编码成所有系统的容量规则。K=3 时 333/334 agents 对应 999/1002 个**新增实验 mount 条目**，admin/其他会话也可能占供应商配额。本轮只测成功点性能，不实现拒绝边界探测/恢复；不能凭未运行的高 N 写“已验证在此处拒绝”。
- 默认按 target 顺序运行，不做随机化；正式 runs 交错 target 顺序、保留重复样本离散度、检查 cache/slab 历史与后台噪声。短 directory/bind smoke 不是论文性能数据。
- 不启动容器、模型或真实 agent；没有逐操作尾延迟、durability 或恶意写入隔离结论。

## 已执行的验证

Colima 非 root、Linux aarch64、Python 3.12.3、Open MPI 4.1.6、官方 IOR 4.0.0 release 构建的 mdtest：

| 场景 | 已观测结果 |
| --- | --- |
| `--directory-smoke`，N=2、K=3、32 文件 | 12 条 JSONL 测量，生成 summary.csv |
| placement 修正后的 K=3 / K=1 directory runs | K=3 的 JSONL 包含 private/shared-1/shared-2 各自的 hostname、CPU、单核 affinity；K=1 连续两轮仅包含 private，无共享 proofs 残留 |
| `taskset` 仅允许单个逻辑 CPU 的 `--directory-smoke` | 明确报错需要至少 2 个可用逻辑 CPU，不启动 MPI，不隐式缩容 |
| 默认 N、本机 6 个可用逻辑 CPU、K=3 | ranks 实际绑定 CPU 0–5；18 个专属 Linux bind mounts 建立、mdtest 完成、卸载后残留 0 |
| 显式 N=2、hosts 文件不存在 | 只用本机，6 mounts 的完整负载/lifecycle 成功 |
| K=1、N=2、两次重复 | 私有 create/stat/read/remove 与两轮 mount/unmount 成功 |
| `taskset -c 1,3`、默认 N | 仅两个 ranks，实际 affinity 分别为 CPU 1、3；指定 N=3 则因容量不足拒绝，不超订阅 |
| macOS→Colima 的实际 SSH worker | 分发包/profile、preflight、6 mounts、采样、日志回收、卸载零残留、部署删除成功；inventory-aware MPI SSH agent 的真实 SSH 命令执行成功 |
| 仓库 unittest suite（本轮修正后） | `python -m unittest discover -s tests -v`：112 项通过，含本轮新增的 affinity 不符/缺失拒绝与单核 smoke 前置检查两项回归 |

Linux bind mounts **只验证 harness/lifecycle**，不代表独立 NAS 客户端或存储性能。这些临时 smoke 结果不作为论文数据。尚未运行两台独立 Linux 主机组成的全局 MPI 作业、真实 NFS/LakeBase profile 或千 client 实验，不能据此声称它们已通过性能/容量验证。

## 依据

- [IOR 4.0.0 release](https://github.com/hpc/ior/releases/tag/4.0.0) 与 [mdtest 原生计时/CSV 源码](https://github.com/hpc/ior/blob/4.0.0/src/mdtest.c)
- [Open MPI v4.1 mpirun](https://www.open-mpi.org/doc/v4.1/man1/mpirun.1.php) 与 [TCP transport](https://docs.open-mpi.org/en/main/tuning-apps/networking/tcp.html)
- [LakeBase shared directory](https://help.aliyun.com/zh/polardb/polardb-for-postgresql/polardb-agent-lakebase-shared-directory)
- [Linux NFS mount/cache 语义](https://man7.org/linux/man-pages/man5/nfs.5.html)
