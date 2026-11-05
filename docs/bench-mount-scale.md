# Mount-scale：单机 agent mount 扩展性

目的：固定每 agent 的小文件工作量，增加 agent 数 N，比较 mount 拓扑的启动成本、挂载后访问性能与主机资源成本。使用 **原版 IOR 4.0.0 mdtest**，不是 Python 仿写，也不是应用端到端性能测试。实现入口为 `agent_fs_bench/benchmarks/mount_scale.py`。

## 实验单元与拓扑

X 是每个 agent 使用的**总目录数**，含 1 个私有 workspace 与 X−1 个所有 agent 共同读取的共享资源；默认 X=3。每个 agent 是一个独立的 mdtest MPI 进程，不启动容器或不同镜像。

| 拓扑 | 每 agent 的 filesystem mount | N 个 agent 的 mount 总数 | 共享资源路径 |
| --- | --- | --- | --- |
| `multi-mount` | 私有 workspace 1 个，X−1 个资源各 1 个 | N×X | 各 agent 通过独立 mount 读相同底层数据 |
| `shared-directory` | 独立 `--subdir` mount 1 个 | N | 在各 agent mount 内用 LakeBase `--bind` 映射 X−1 个资源 |
| `directory-smoke` | 不执行 mount | 不输出 mount 测量 | 普通目录，仅验证 harness／mdtest／结果链路 |

**不是所有 agent 共享一个主机级 mount。** `shared-directory` 仍然是 N 个独立 mount。Linux bind mount 与 LakeBase 内部 `--bind` 不是同一种机制；不能用普通目录或 Linux bind mount 的结果证明 LakeBase 的性能。

此实现不创建容器的 bind mounts，不计镜像下载、容器启动时间；它只比较容器绑定之前的 filesystem 客户端。没有实现多 host SSH；可在某台 Linux host 上运行，也可通过 `colima ssh` 进入单台 VM 运行。已有 `multi-host` 是另外的共享写竞争实验，不能替代这里的扩展性模型。

## 依赖与配置

Linux、Python 3.12+、DuckDB、**mdtest 4.0.0**、与它链接的 MPI 库匹配的本地 MPI launcher（MPICH/Hydra 或 OpenMPI，支持 `:` MPMD 语法）。正式实验由环境预装这些工具；benchmark 不自动下载或编译，也不自动 sudo、重试、降级或安装组件。

上游构建示例（使用同一套 MPI 的 `mpicc` 和 `mpiexec`）：

```sh
# 下载并解压官方 ior-4.0.0.tar.gz 后，在其源码目录中
./configure CC=mpicc
make -j2
# 将 src/mdtest 和对应 MPI launcher 加入 PATH，或修改 CONFIG
```

实验参数集中在 `mount_scale.py` 顶部的 `CONFIG`：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `clients` | `(1, 2, 4, 8, 16)` | N，严格递增并以 1 开始 |
| `directories` | `3` | X，包含私有目录；可为 1 |
| `files` | `1000` | 每目录文件数；每 agent 执行相同数量操作 |
| `file_size_bytes` | `4096` | 小文件写入／读取字节数 |
| `repeats` | `3` | 每个 N 重复次数 |
| `mdtest` | `mdtest` | 原版二进制 |
| `mpi_launcher` | `('mpiexec',)` | 本地 launcher argv；禁止传入多 host 列表 |
| `command_timeout_s` | `300` | 每个 mdtest／launcher 命令的超时，不是 mount 超时 |
| `idle_sample_s` | `1.0` | 基线、挂载后、卸载后主机采样区间 |
| `resource_sample_s` | `0.1` | 整个 workload 期间的内存采样周期 |
| `profiles` | `local/mounts.toml` | 不提交的真实挂载命令 |

可按机器容量增加 N。默认 1000 文件是轻量起点，不保证足以跑满机器；正式实验应增加文件数，使阶段时间明显超过 MPI barrier、定时器和调度开销。不得通过取消独立 mount 或合并 agent 进程来“跑满”。不自动绑核，须统一 CPU affinity、NUMA 和超订阅策略；smoke 的超订阅不用于正式性能比较。

### Mount profile

复制并修改 [`experiments/mounts.example.toml`](../experiments/mounts.example.toml) 到 `local/mounts.toml`。profile 名称与 `local/targets.toml` 的 target 名一致：

```toml
[[targets]]
name = "nfs"
path = "/var/tmp/mount-scale-nfs"

[[targets]]
name = "lakebase"
path = "/var/tmp/mount-scale-lakebase"
```

**这里的 target 是本地 staging 根目录，不是待测 filesystem 的根。** 它必须预先存在，位于 `admin_root` 之外；结果目录也必须在待测 filesystem 之外。mount points 建在 `<target>/agent-fs-bench/mount-scale/<run-id>/N<N>-R<R>/agent-<id>/`。

`admin_root` 是实验前已挂好的完整卷管理视图，用来准备同一卷中的私有目录与共享 fixture；它在整个实验中保持挂载，计入主机资源基线，但不计入每 agent mount 数。准备数据位于 `<admin_root>/agent-fs-bench/mount-scale/<run-id>/`。NFS 的 admin 视图必须对应命令中的 export 根；LakeBase 的 admin 视图必须是完整卷根，而非某个子目录。

mount 命令是 TOML argv 数组，用 `format_map` 展开，不经 shell。支持：

| 占位符 | 含义 |
| --- | --- |
| `{mountpoint}` | 本机实际挂载点 |
| `{source}` | 源路径，相对 admin 卷根转为 `/` 开头的卷内绝对路径 |
| `{subdir}` | 本次私有 mount 的卷内路径；shared-directory 为 agent 根，multi-mount 为 workspace |
| `{binds}` | `共享源:agent/shared-1,...` 的卷内绝对路径映射列表 |
| `{agent}` | 从 0 开始的 agent 编号 |

示例中的 NFS server、export、LakeBase META_URL 必须替换。NFS 示例保留常规 cache 默认值，不强行加 `nosharecache`：**N×X 个 mount entries 不等于 N×X 份内核客户端、连接或 cache**。若研究独立 cache 实例，应单列、报告该选项，不得把它暗中加到基线以放大成本。FUSE 命令也必须保证每次 mount 真正创建所需客户端，不能用单 mount 加 Linux bind 冒充多客户端。

LakeBase 命令须在 mount 建立后返回（daemon 模式），卸载命令须在 mount 释放后返回；计时以命令返回为边界。若实际客户端需要不同参数，修改私有 profile，不添加第二套内置厂商接口。X=1 时没有共享目录，应从 LakeBase profile 中移除空的 `--bind` 参数。

共享目录只执行只读负载；权限由实验环境配置。NFS 示例使用 `ro`；LakeBase 的只读保护依赖非 root agent 与 POSIX 权限。这里不验证恶意写入隔离，不把 workload 不写等价成强制只读权限。正式主机应预配运行用户可执行的 mount／umount 权限；benchmark 本身从不提权。

## 运行

```sh
uv run python -m agent_fs_bench run mount-scale --target nfs
uv run python -m agent_fs_bench run mount-scale --target lakebase

# 只跑真实 mdtest 与普通目录；无需 profiles，不会执行 mount/umount
mkdir -p /tmp/mdtest-directory-smoke
uv run python -m agent_fs_bench run mount-scale \
  --dirs /tmp/mdtest-directory-smoke -- --directory-smoke
```

`--directory-smoke` 固定 N=1,2、每目录 32 文件、1 次重复；保留 CONFIG 的 X 和文件大小。不会读取挂载 profile，也不会输出 mount／unmount rows。不允许把它混入论文挂载性能样本。

## 执行与计时边界

先用一个 mdtest rank 对 X−1 个共享目录各执行 `-F -S -C -n <files> -w <size>`，整次运行只创建一份共享数据，不为每个 agent 拷贝。每次 repeat 顺序扫描所有 N；每个点：

1. 在 admin 视图创建 N 个独立 `agent-<id>/workspace`；shared-directory 的 bind 目标在卷内保持不存在，满足 LakeBase 的约束。
2. 测主机 idle 基线。N 个 agent 并发 mount，每个 agent 内顺序挂私有目录、再挂各共享目录。mount 计时包括本地命令启动、线程调度与命令返回；目录准备不在其中。
3. 在 `/proc/self/mountinfo` 中核对新增 mount **条目**，包括重复叠挂也按条目计数，要求恰好 N×X 或 N，且路径吻合。通过私有／共享 sentinel 校验实际数据别名；这些核验不计入 mount 时间。它不证明厂商、FUSE 进程数、连接数或 mount options。
4. 采集挂载后 idle 主机状态。通过每个 client 的共享目录别名做 stat/read 预热，丢弃该轮 mdtest 指标。
5. 测私有 `create → stat → read → remove`；随后逐个共享资源测 `stat → read`。**各资源及私有／共享阶段是分开的，并非同时交错混合访问**。共享只读不测 create/remove，不引入共享写锁竞争。
6. N 个 agent 并发 unmount，每个 agent 内逆序卸载。要求各 agent 子树不再留有任何 mount。采集 released-idle 主机状态后输出该点结果。
7. 成功且确认卸载后删除本轮私有数据；全部成功后删除自建共享 fixture。出错停止并保留现场，不自动 umount 或重试。中断后可能有 mount／客户端残留，人工检查后再处理本次 run 目录。

每个 agent 对应一个单线程 mdtest rank。MPI MPMD 把每个 rank 的 `-d` 指向自己的 mount alias，所有 rank 在同一个 `MPI_COMM_WORLD` 中使用 mdtest 原有 phase barriers。私有阶段使用 `-u` 与 rank 私有文件名；共享阶段使用 `-S` 与共同文件名。因 mdtest 4.0.0 的 tree 创建逻辑，harness 在私有测量前预建 `test-dir.0-0`，不计其时间；每个 rank 自建、删除自己的 mdtest tree。

采用 warm shared-data 协议，不 drop cache；只保证做过预读，不保证数据始终留在 cache。私有 read 发生在本轮 create 后，不是冷读。没有 `fsync`／`sync`，结果不证明持久化延迟。原版 mdtest 的 POSIX wrapper、循环和 MPI barrier 开销都在阶段计时内；Python、MPI 启动、fixture、预热、结果写入不在阶段计时内。启用 `--warningAsErrors`，操作警告、错误、task 数不匹配、跨 host、版本不匹配或缺失计时即失败，不输出伪造成功值。

## 指标与原始结果

统一输出 `results/mount-scale/<run-id>/results.jsonl`、`summary.csv`。case 含 topology、N 和阶段，例如 `multi-mount/N8/private/create`；repeat 区分完整扫描轮次。参数保留 N、X、文件规模、cache/fsync 协议、mdtest 版本和二进制 SHA256、MPI launcher 版本、源码 SHA256、内核版本、CPU 数和私有 mount profile 文件 SHA256。命令／凭据不写入 JSONL；私有 profile、挂载选项及环境配置仍须实验者自行归档。

| 指标 | 定义／解释 |
| --- | --- |
| `ops` | N×files，每个 agent 执行 files 次当前操作 |
| `elapsed_s` | mdtest 精确 CSV 的 all-row 阶段时间，最大 rank 时间，含 phase-end barrier；不是 rounded stdout，也不是子进程 wall time |
| `ops_per_s` | `N×files / elapsed_s`；不求和各进程独立速率 |
| `scaling_efficiency` | `throughput(N) / [N×throughput(1)]`，使用同一 repeat 中的 N=1 |
| `rank_mean_us_per_op` | `sum(rank_elapsed_s)/(N×files)×1e6`，rank 的阶段均摊成本，**不是逐操作 latency，也不是 p50/p99** |
| `bytes`, `mib_per_s` | create/read 的逻辑字节；共享读取 N 次同一数据，不是物理网络／磁盘字节 |
| `mount_elapsed_s`, `unmount_elapsed_s` | 完成全部 agent lifecycle 命令的 wall time；details 保留逐 agent 时间 |
| `measured_mounts` | 经过核对的本次新增 agent mount 条目；不是推算的进程数 |

`details.rank_elapsed_s` 为 mdtest rank CSV 的 barrier 前阶段时间。mdtest stdout、stderr、原始 rank CSV 仅作诊断附件；计时样本已经转入 JSONL，不依赖这些附件也可分析。

资源单独为 `resources/baseline`、`mounted-idle`、`released-idle`、`workload` rows：

- `/proc/stat` 给出全机 busy CPU 秒、平均 busy cores 与百分比；排除 idle/iowait，不重复计 guest。包含其他进程和内核，不是文件系统归因 CPU。
- `/proc/meminfo` 给出 `MemTotal−MemAvailable`（`used_bytes`）、cache、dirty、slab 及可回收／不可回收 slab。挂载后／卸载后 row 记录相对本点 baseline 的增量，可能为负。
- workload row 周期采样给出观察到的峰值与相对 workload 起点的增量；**包含整个 MPI job 启停、Python harness 与阶段间隙**，不等于纯 mdtest phase 资源用量，也不保证捕获瞬时峰值。
- mount entries 来自当前进程的 mount namespace；daemon 应在同一个 namespace 建立 mount。
- admin mount、Python、MPI、cache 和 slab 都会影响总量。要求专用闲置 Linux host，保持后台负载一致。没有 FUSE RSS 或 FUSE 进程数指标，不能把主机 used-memory delta 标为它们。

## 论文结论边界

这是 weak scaling：每 agent 工作量固定，N 增长时总工作量增长；共享资源内容固定，私有文件数随 N 增长。X 决定共享读阶段数，因此横向比较必须固定 X、文件规模与配置。

跨文件系统的差异还包括服务端、缓存、RPC、权限／一致性策略和实现，不是 mount 数的纯因果效应。正式论文至少增加**同一 LakeBase 后端 multi-mount vs shared-directory 的机制消融**，再比较其他 NAS；保持客户端硬件、后端数据、选项、网络和 cache 协议一致。FUSE 一个 mount 一个 daemon 的假设不能直接套用 Linux NFS。

默认按 N 递增、目标顺序运行，没有随机化；正式测量应在独立 runs 中交错目标顺序，检查温度、cache／slab 历史和后台噪声，绘制重复样本的离散度。接近零的轻量阶段不能据此声称极限吞吐或尾延迟；若单机负载不足，先增大文件数／N，再另行实现多 host 方案，而不是把现有 multi-host 结果直接拼入单机曲线。

本次仅完成 Colima 普通目录 smoke，未执行真实 mount、NFS 或 LakeBase，不能据此写出它们的性能结论。

## 依据

- [IOR/mdtest 官方项目与构建](https://github.com/hpc/ior)，[4.0.0 release](https://github.com/hpc/ior/releases/tag/4.0.0)
- [mdtest 4.0.0 参数、MPI barrier、rank CSV 与 aggregate reduction 源码](https://github.com/hpc/ior/blob/4.0.0/src/mdtest.c)
- [LakeBase shared directory：独立 subdir mount 内的 bind 与目标路径约束](https://help.aliyun.com/zh/polardb/polardb-for-postgresql/polardb-agent-lakebase-shared-directory)
- [Linux NFS mount/cache 语义](https://man7.org/linux/man-pages/man5/nfs.5.html)
