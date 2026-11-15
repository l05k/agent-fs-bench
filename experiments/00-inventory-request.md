# 第 0 轮：保留测试现场，盘点代码、踩坑经验和已有数据

请在**当前测试机、当前旧版 checkout** 上完成这次盘点，并回传**一份 UTF-8 Markdown 源文档**。文档可以内嵌 CSV、JSON 和小型代码 diff，不要求用 Markdown 表格。目标是让另一端先理解现有工作，再决定怎么迁移和继续实验；本轮不要求采用新版测量结论。

## 0. 本轮边界

- **不更新、不修复、不重跑**：不要 `pull`、`checkout`、`reset`、`clean`，不要更新子模块、依赖或工具，不提交现有改动，不改变配置、不清理数据、不卸载挂载。不要为证明已知错误再运行失败实验。
- 只读已有代码、结果、历史记录和必要的系统信息；可以在工作树外另建回传草稿。不要扫整个磁盘，不递归读取工作区大文件，不中断正在运行的实验。
- 先说明当前是否有实验还在写结果。运行中的目录标为 `running`，只盘点，不把不一致的读出视为已完成快照；无需为盘点停止任务。
- 不知道就写 `unknown`，没记录写 `not_recorded`，确实没有写 `none`。没有原始数据，不从图、终端摘要或记忆反推样本。
- 区分 `observed`（文件或日志证据）、`reported`（操作者经验）、`inference`（推断）。不要把猜测写成事实。
- 凭证、私钥、完整环境、原始日志、模型响应和 replay cassette 均不回传。不执行 `env`、`printenv`、`git remote -v`，不整段粘贴 shell history。日志只摘录解释问题所必需的几行并人工脱敏。
- 使用稳定别名：`machine-01`、`target-apsara`、`path-fixture-01`、`endpoint-01` 等。不同主机/路径/实例/端点不得合成同一个别名；本地保留对应关系，后续批次复用。CPU 型号、资源数量、FS 类型、公开软件版本、语义配置和原始 SHA-256 可保留。
- 附件、压缩包、base64 不适用于这条通道。最终答复的所有内容都在同一文档中。

## 1. 代码与本地实验改动

在现有仓库根目录读取下面的信息；命令不存在或目录不同就说明，不安装、不猜测、不自动修复。输出回传前脱敏。

```sh
GIT_OPTIONAL_LOCKS=0 git rev-parse HEAD
GIT_OPTIONAL_LOCKS=0 git status --short --branch
GIT_OPTIONAL_LOCKS=0 git log -8 --format='%H %cI'
GIT_OPTIONAL_LOCKS=0 git diff --stat
GIT_OPTIONAL_LOCKS=0 git diff --cached --stat
GIT_OPTIONAL_LOCKS=0 git submodule status
```

对子模块单独记录实际 HEAD 和本地改动；主仓登记的 gitlink 与子模块 HEAD 不一致时，两者都保留。不要初始化缺失子模块。若实验脚本在仓外、未跟踪文件或另一个工作树，给它们稳定别名并纳入清单；`git diff` 不含这些文件。

请给出如下 CSV；没有的行不要编造：

```csv
repo_alias,head_commit,branch_alias,dirty,submodule_pinned_commit,submodule_actual_commit,experiment_code_location,evidence_kind
```

再给出**有科学影响的改动清单**，包括已提交和未提交的改动，不只是当前 dirty diff：

```csv
change_id,repo_alias,file_or_script_alias,base_commit,change_commit_or_uncommitted,purpose,changed_measurement_semantics,affected_run_refs,evidence_kind
```

优先说明计时范围、同步/持久化、缓存与挂载、并发/资源、输入/任务选择、代理或工具版本、验收条件、汇总筛选、失败处理的变化。参数扫描与代码逻辑修改分开写。提供必要的小型**已脱敏** patch 或函数片段，带文件别名和起止位置；不要打包整份工作树 diff、`local/`、配置密钥或模型数据。特别有价值的实验性修复，不要只用一句“已修复”替代其行为变化。

当前代码与历史 run 的来源可能不同：每组结果优先使用其生成时记录的 commit、source digest、配置和脚本副本；只有当前 checkout，不能证明它生成了旧结果。

## 2. 机器、存储和输入条件

按机器别名回填：CPU 型号/核数、内存、OS/kernel/架构，实际给 benchmark、FUSE 客户端和服务端的 CPU/内存额度，是否多机，后台负载是否已记录。可以读取 `uname -sr`、`uname -m`、`lscpu`、`free -b`；不要回传真实 hostname/IP。

按 target 回填：文件系统与客户端/服务端版本、存储部署规模、介质和网络资源是否固定、mount 参数中有科学含义的选项、一个挂载是否对应独立客户端、缓存共享是否已查明。挂载源、真实路径、实例 ID 和端点用别名；不要直接粘贴 `mount`/`findmnt` 的完整输出。

```csv
target_alias,filesystem,client_version,server_version,machine_alias,mount_option_semantics,resource_budget,independent_client_evidence,unknowns
```

输入也做清单：fixture、数据规模、task split/修订/选择种子与 IDs、镜像 digest、模型与 harness、cassette schema 和 hash、二进制 hash。仅填写现有记录；本轮不生成新 task sample 或重新录制。模型/任务私有内容不回传。

## 3. 已踩过的错误和有效经验——请优先保留

把已经知道的问题集中成一张 CSV，不用等它再次发生。包括：错误命令/参数，挂载权限或残留，OOM/资源上限，超时/并发死锁，工具版本不匹配，SQL 失败或提交缺失，缓存误判，吞吐分母算错，计时混入准备/清理，Clone API/复制边界，replay 发散或验收过宽，以及“程序成功但测到的不是想测的东西”。没有遇到的项不必逐一回答。

```csv
issue_id,workload,symptom,trigger_conditions,root_cause_or_unknown,evidence_kind,evidence_ref,workaround_or_fix,current_status,affected_run_refs,data_impact,do_not_repeat
```

每条重要问题补充：

1. 最小脱敏证据：错误码/报错片段、相关配置或代码位置、已有验证记录；不要带整份日志。
2. 已确认的根因和仍不确定的部分；区分已实施修复与仅有设想。
3. 哪些 run 作废、哪些只适合作诊断、哪些未受影响，为什么；不删除这些 run。
4. 已知有效的操作顺序或参数约束，以及下一轮必须避开的做法。

多次发生的同类错误可合并，但保留受影响范围。操作者的未落盘经验也有价值，标为 `reported`，不要因缺日志而丢弃。

## 4. 全量现有实验清单：先覆盖，再选数据

先盘点**所有已知结果根目录**，含旧布局、失败/中断结果、临时实验和旧图；仅在明确的结果目录内查找。为每个 run 分配稳定 `run_ref`，建立下面索引。不能从 CSV 存在推断运行成功，不能从目前的代码推断历史测量边界。

```csv
run_ref,workload,recorded_time,code_commit,source_sha256,config_ref,target_set,task_or_fixture_identity,scale,repeats_recorded,status,raw_files,raw_bytes,semantic_group,issue_ids,included_this_reply,omission_reason
```

`status` 可为 `complete`、`partial`、`failed`、`running`、`unknown`。`complete` 需要已有完成记录及预期样本证据；旧版没有这些字段，就保持 `unknown`。别丢掉失败、超时、缺 capture 和未执行的格子。运行失败与 replay 中已分类的不成功样本分开记录。

下面是**可选的只读索引命令**，仅适用于已有 `config.json`/CSV 的结果根目录。在仓库根执行，将 `results` 换成已经确认的本地目录；额外根逐个指定。它只列元数据和大小，不转换数据、不替你判定有效性，输出仍需人工审核。没有 Python 3 就手工填清单，不安装。

```sh
python3 - results <<'PY'
import csv, json, sys
from pathlib import Path

root = Path(sys.argv[1])
if not root.is_dir():
    raise SystemExit('Result root not found; identify the actual existing directory first')
folders = set(p.parent for pattern in ('config.json', '*.csv')
              for p in root.rglob(pattern) if p.is_file() and not p.is_symlink())
writer = csv.writer(sys.stdout, lineterminator='\n')
writer.writerow(['relative_run', 'benchmark', 'run_id', 'git_commit', 'source_sha256',
                 'planned_targets', 'completed_targets', 'config_bytes', 'csv_files', 'csv_bytes', 'note'])
for folder in sorted(folders):
    path = folder / 'config.json'
    config, note = {}, 'completion_not_assessed'
    if path.is_file():
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(config, dict):
                config, note = {}, 'config_is_not_an_object'
        except (OSError, ValueError):
            note = 'config_unreadable_or_invalid'
    else:
        note = 'config_missing'
    tables = sorted(p for p in folder.glob('*.csv') if p.is_file() and not p.is_symlink())
    targets = config.get('targets')
    completed = [t.get('name', 'unknown') for t in targets if isinstance(t, dict)] if isinstance(targets, list) else 'not_recorded'
    writer.writerow([folder.relative_to(root), config.get('benchmark', 'unknown'),
                     config.get('run_id', 'unknown'), config.get('git_commit', 'not_recorded'),
                     config.get('source_sha256', 'not_recorded'),
                     json.dumps(config.get('planned_targets', 'not_recorded'), ensure_ascii=False),
                     json.dumps(completed, ensure_ascii=False), path.stat().st_size if path.is_file() else 0,
                     len(tables), sum(p.stat().st_size for p in tables), note])
PY
```

该索引不覆盖其他格式或仓外脚本，需手工补齐。若目录仍在写入，标注扫描时间与 `running`，不宣称快照一致。

### 按实际测量语义分组

每组写一行；不确定的项不能凭名称补齐：

```csv
semantic_group,workload,timer_start,timer_end,included_setup_cleanup,cache_boundary,durability_boundary,concurrency_scope,completion_validation,aggregation_rule,evidence_ref,unknowns
```

重点辨清：

- 小文件：文件数/大小是每 Agent 还是全局总量？共用一个 mount、bind 还是独立挂载？read 在 write 之后还是独立 fixture？是否 remount？write 是否包含最终 sync？remount 不能证明服务端/持久缓存已清空。
- SQLite：操作数、BLOB 大小、journal/synchronous、每事务多少更新、是否检查 commit/完成数、read 是否单独测量、时钟单位与零值问题。
- 开发工作流和 npm/pnpm：准备、编译、Git、最终同步和清理分别在哪个计时范围？是否 `--offline`？报告的是 install 退出还是 durable completion？
- Clone：原生 API 返回代表什么，是否等待完成，copy 是否可能 reflink，复制和目标持久化分别计了什么，是否做内容/隔离验证？
- Replay：使用哪个原生 harness，是否真实执行工具，报的是工具时间还是 orchestration 总时间，LLM 是否固定回放，验收是一次还是每次，reward/失败如何筛选，是否插入每步同步？

这些是问题清单，不是要求你把旧数据改写为新版语义。发现过去的声明与实现不符时，原声明、实际实现和影响范围都保留。

## 5. 本轮附哪些现有数据

先选**每个相关 workload × 测量语义组中，最近的一组可比较实验**：同一组包含原计划的所有 backend、重复和并发档位，以及对应失败/缺失清单。不得按速度、reward 或“看起来正确”挑行；没有完整组就明确说明，并附最近的部分组用于诊断。未执行/已失败的点不补零、不补造 timing。

对选中 run：

- 内嵌生成时的**已人工脱敏配置**及同层所有原始结果 CSV；保留字段名、单位、逐样本值、repeat/sample/task/target 身份和失败状态，不只给均值或中位数。
- 记录来源文件相对别名、字节数、行数、原文件 SHA-256（能从稳定文件读取时），以及回传副本的 SHA-256。明确哪些字段被别名替换；不要把脱敏副本 hash 伪装成原文件 hash。
- 不改写原始文件。脱敏只改副本中的主机、路径、端点/实例等私有标识；数值、单位、状态、计时边界和来源指纹原样保留。若某个数值确实敏感，本轮不传它，注明限制，不改成虚假值。
- 旧格式原样附在 `legacy/<run_ref>/...` 名下，不添加虚假的新版字段，不默认纳入新版配对报告或论文图。
- 只有摘要/旧图：给出其存在、文件别名与来源，不从图恢复数字；原始图或二进制留在测试机。

单文档太大时，**仍先返回完整清单和全部重要错误经验**。选一个明确且尽量完整的比较组回传；如果连这一组也装不下，本轮只返回准确的文件清单/大小与未包含原因，下一轮按指定 run 分批索取。不要截断 CSV、使用省略号代替样本、压缩成 base64，或把没有传的数据写成“已附”。

## 6. 最终只回一份文档

建议答复结构：

1. **现场摘要**：当前 checkout、是否仍有后台实验、未知/阻塞项。
2. **代码与改动**：版本 CSV、改动 CSV、必要脱敏片段。
3. **机器、target 与输入**：上述条件清单。
4. **错误经验**：问题 CSV 和关键证据，明确不要重复的做法。
5. **全量 run 索引与语义分组**：含失败、缺失、运行中和未传出的条目。
6. **本轮包含的原始文件**：每个文件一个具名代码块，例如下列形式；嵌套代码围栏时外层使用更多反引号。
7. **下一轮建议**：哪些改动值得保留、哪些结论暂不可用、还缺哪些证据；这是建议，不执行升级或复测。

文件块格式示例（这里是格式说明，不是数据）：

````text
### legacy/R01/config.json
```json
完整的已脱敏原配置
```

### legacy/R01/phases.csv
```csv
完整表头及该文件的全部样本行
```
````

**本轮不依赖任何新版本的导入/导出工具。** 先用这一份可读的文档恢复现场知识；等另一端审阅后，再下发下一轮具体测试方案。
