# 实验归档

[Round 04](round-04.md) 是当前执行矩阵；[Round 03](round-03.md) 已冻结为上一版参考协议，
文件不改。下列材料只用于追溯；不执行、不进入新论文比较组。旧 checkout、版本、配置、
`results/`、`packages/` 和旧失败记录原样保留；已从当前分支移除的 K>1/`directories`
实现与 `mini-dev-workflow`、`npm-install-nofsync`、`sqlite-speedtest`、`mdtest` CLI
入口只存在于历史 commit。冻结的 Round 03 commit `220c066`、Round 02 commit `9a10d28`
仍可检出运行。归档不是删除，也不把实测改称合成数据。

当前分支也已删除 `npm_install_nofsync.py`、`sqlite_speedtest.py`、
`mini_dev_workflow.py` 及其专用测试；不为退役实验保留实现或 mini 对照 oracle。对应
历史结果和 checkout 不变。

| 归档项 | 原材料 | 不进入新版的原因 |
| --- | --- | --- |
| [Round 03](round-03.md) | 冻结 commit `220c066`；文件本身不修改 | 上一版正式协议，保留为参考矩阵和详细合同来源；Round 04 只在明确声明的范围内采用其 replay/工具/峰值/clone 合同，不把旧网格或旧资源规则当新正式点。 |
| [Round 02](round-02.md) | 固定代码 `9a10d28`（仍可检出运行）；执行方案的发布快照 `dea462b` | 旧版执行方案。批末 `syncfs` + 重挂载读、对称 per-agent `smallfile-quota` 和 replay smoke-only 不再是正式矩阵；当前 replay 是全队列，扩展性使用原生 mdtest。 |
| `mount-scale` 旧 POSIX I/O、directory smoke 与**已退役**的 harness cgroup 限额扩展 | 历史 `mount_scale_io.py`、`--workload io`、`--directory-smoke`、profile `cgroup=true`/`mount_cgroup`、harness 建组/配额实现 | 当前分支只保留真实挂载上的原生 `mdtest-io`（默认）、`mdtest-create` 和 `mount`。旧 POSIX/重挂载、directory-smoke 和 harness 自建/自删配额的扩展已整条删除，不留兼容。当前可选的 `rank_cgroup` 只是站点自建组的**路径声明与 proof 校验**薄钩子；harness 不建组、不删组、不改额度，旧 `cgroup=true` 仍是无效未知键。两者不是同一实现。 |
| 旧 FUSE cgroup create 数据与旧 NAS create 网格 | `fuse-cg`/`nas-v4` 旧 create run、旧检查点与原始结果 | 新 create/io 轴要求每 rank 1c2g 真实站点 proof。CephFS FUSE 旧点只有能核验真实 FUSE 挂载、daemon/rank 同组和全量计数时才是有条件候选；旧 NAS 网格没有 per-agent rank 预算，资源合同已改变，整组降为 legacy，不与新预算点混比。旧观测保留，不删除、不改称合成。 |
| LakeBase 缓存消融与旧 21 格 | `mid-ind` / conn4-cg 历史 21 个配置点 | 缓存消融本轮暂缓但能力保留；旧 `mid-ind` 与 fast-drop 是多选项配置对照，不能写成单因素缓存消融或官方消融结论。 |
| 共享目录/多 share 单挂载、K>1、CephFS `lazyio` | `mount-scale` 历史配置；`grp-ms-k3`；`lazyio` 仅候选，无已交付测量 | 不作为新版实验臂。当前分支已删除 K>1/`directories` 实现与命令，只保留历史 commit、配置和原始结果。 |
| 旧安装扩展与进程完成变体 | `grp-npm-conc`、`grp-npm-conc-nf` / `npm-install-nofsync` | c8/c16 与 no-fsync 不是当前 `tasks=1` 的持久完成边界；只保留确有可比性的旧单任务持久点。 |
| mini 开发工作流、旧未同步工作流、`speedtest1` | `grp-mini-dev` / `mini-dev-workflow`、`grp-dev`、`grp-sqlite` / `sqlite-speedtest` | 不代替完整 kernel replay + final sync 或 DELETE/FULL 的 `kvtest`。 |
| 独立固定总量 mdtest 曲线 | `grp-mdtest-early` / `mdtest` | 固定总量和旧语义不代替弱扩展；`mdtest` CLI 命令已从当前分支移除，`mdtest.py` 实现仍供 `mdtest-peak` 复用。 |
| Experiment 7 元数据后端、scope-on/off、kernel 正式臂、`nas-fast` | 旧元数据观测、scope 配置、kernel `noshare`/`nas-fast` run | Experiment 7 本轮不纳入，但旧实际观测与原始记录保留、不删除、不重跑；scope-on/off、kernel 正式臂和 `nas-fast` 也不进入本轮正式矩阵。kernel/nas-fast 只可留在历史或示例 profile 中。 |
| 非计划高并发配置和超时片段 | 旧 `mid`/`conn4`/`big`、10k 文件、stonewall 部分完成、临时单机脚本、旧 Intro cohort | 保留诊断及部署差异；不与 fast 主臂、1:1 mount 密度或完整批次合并。Intro 旧 cohort 的网格冲突以用户报告为事实，不再核验、不重跑、不建立替代 cohort。 |

Experiment 9 的可比 Clone 组仍在新版范围内：先核验旧 `clone-05..13`；同口径才复用，
否则按预先计划的新形状补测，不把旧点改标签。所有旧 `complete` 都是原索引状态；纳入
由新版各组的实际输入、完成边界、挂载和资源证据决定。
