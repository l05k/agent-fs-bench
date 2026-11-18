# 实验归档

[Round 03](round-03.md) 是当前执行矩阵。下列材料只用于追溯；不执行、不进入新论文比较组。旧 checkout、版本、配置、`results/`、`packages/` 和旧失败记录原样保留；已从当前分支移除的 K>1/`directories` 实现与 `mini-dev-workflow`、`npm-install-nofsync`、`sqlite-speedtest`、`mdtest` CLI 入口只存在于历史 commit。冻结的 Round 02 commit `9a10d28` 仍可检出运行。归档不是删除，也不把实测改称合成数据。

| 归档项 | 原材料 | 不进入新版的原因 |
| --- | --- | --- |
| [Round 02](round-02.md) | 固定代码 `9a10d28`（仍可检出运行）；执行方案的发布快照 `dea462b` | 旧版执行方案。批末 `syncfs` + 重挂载读、对称 per-agent `smallfile-quota` 和 replay smoke-only 不再是正式矩阵；当前 replay 是全队列，扩展性使用原生 mdtest。 |
| `mount-scale` 旧 POSIX I/O、directory smoke 与 FUSE cgroup 限额消融 | 历史 `mount_scale_io.py`、`--workload io`、`--directory-smoke`、profile `cgroup=true`/`mount_cgroup` | 当前分支只保留真实挂载上的原生 `mdtest-io`（默认）、`mdtest-create` 和 `mount`；旧 POSIX/重挂载、directory-smoke 和专用 cgroup 扩展已整条删除，不留兼容。旧观测和结果仅作历史材料，不是正式数据删除，也不改称合成数据。 |
| 共享目录/多 share 单挂载、K>1、CephFS `lazyio` | `mount-scale` 历史配置；`grp-ms-k3`；`lazyio` 仅候选，无已交付测量 | 不作为新版实验臂。当前分支已删除 K>1/`directories` 实现与命令，只保留历史 commit、配置和原始结果。实验 3 只切换独占挂载上的 drop barrier 和实际读缓存路径。 |
| 旧安装扩展与进程完成变体 | `grp-npm-conc`、`grp-npm-conc-nf` / `npm-install-nofsync` | c8/c16 与 no-fsync 不是当前 `tasks=1` 的持久完成边界；只保留确有可比性的旧单任务持久点。 |
| mini 开发工作流、旧未同步工作流、`speedtest1` | `grp-mini-dev` / `mini-dev-workflow`、`grp-dev`、`grp-sqlite` / `sqlite-speedtest` | 不代替完整 kernel replay + final sync 或 DELETE/FULL 的 `kvtest`。 |
| 独立固定总量 mdtest 曲线 | `grp-mdtest-early` / `mdtest` | 固定总量和旧语义不代替弱扩展；`mdtest` CLI 命令已从当前分支移除，`mdtest.py` 实现仍供 `mdtest-peak` 复用。 |
| 非计划高并发配置和超时片段 | 旧 `mid`/`conn4`/`big`、FUSE quota 扩展、10k 文件、stonewall 部分完成和临时单机脚本 | 保留诊断及部署差异；不与 fast 主臂、1:1 mount 密度或完整批次合并。满足 Intro 合同的旧三系统 create cohort 仍按新版计划核验。 |

实验 7 的独立旧元数据结果和实验 9 的可比 Clone 组仍在新版范围内，不归档。所有旧 `complete` 都是原索引状态；纳入由新版各组的实际输入、完成边界、挂载和资源证据决定。
