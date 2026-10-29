# Carbide 需求理解与修复验收：改进及复验

日期：2026-10-26。接续 `COMPLETION_STUDY_20261025.md`，本轮关注正常结束但补丁未通过独立验收的情况。

后续进展：同日按用户要求改用 `gpt-6-luna` 后，新的 16 次对照已全部完成，严格通过 4 次。详见 [gpt-6-luna 真实任务对照](VERIFICATION_STUDY_6LUNA_20261026.md)。下文保留本阶段旧模型端点不可用时的结果，不覆盖原始记录。

## 1. 当前结论

完成主 Agent 与 Worker 共用的代码修改验证指引，并补齐区分输入因素的对照实验运行器。当前源码的默认离线测试 **626 passed、41 deselected**，可靠性冒烟 **21/21** 通过。

四份历史补丁在新建 checkout 中重新验收，三份仍失败、一份仍通过。复验没有生成新补丁，也没有改变历史任务成绩。

真实模型实验未启动：两次独立端点探测均返回 HTTP 503，`model_not_found` / `No available channel for model gpt-5.6-luna`。本轮没有新的模型正确完成率提升数据。不能用离线测试通过或提示词调整宣称编码能力提高。

## 2. 两类问题需要分开

### 2.1 SymPy 18087：上层表现修复，底层错误仍在

旧输入正文描述 `cos(x) + sqrt(sin(x)**2)` 对一般复数被错误简化。数据集的 `hints_text` 还包含公开讨论：错误沿 `TR10i → trig_split → Factors.as_expr` 传播，底层幂的重建可能不合法。

旧评测有意只提供 `problem_statement`，没有提供 `hints_text`。这符合本地的 problem-only 协议，**不等于运行时 Bug，也不能认定不提供讨论就是评测错误**。公开讨论提供了额外定位信息，需要作为另一输入条件单独比较。

上一阶段 guided、extended 两份补丁都修改 `sympy/simplify/fu.py`，独立的 `test_fu` 通过，`test_Factors` 失败。本轮在新 checkout 重放补丁和冻结测试，结果一致。可确认补丁未覆盖验收要求的共享行为；不能仅凭这两份轨迹证明增加讨论或增加某条指令一定能修好。

### 2.2 Flask 4992：公开建议与验收 API 的差异

问题正文建议使用 `mode="b"`；数据集讨论中维护者进一步提出 `text=True/False` 参数。旧 off 组实现 `mode` 参数，达到 max_turns，独立测试调用 `text=False` 时失败；production 组在相同 problem-only 输入下实现了 `text` 参数，正常完成且通过验收。

因此，该案例同时涉及 API 约定、输入条件和运行轨迹，不能把 off 组简单描述为“完全不理解二进制读取”，也不能把另一组通过解释为某个单因素的效果。两组旧实验均未触发压缩，更不能将差异归因于压缩。

## 3. 生产代码改动

新增 `src/carbide/prompts.py`，将稳定的代码修改验证指引供 SDK 默认提示词与通用 Worker 复用：

1. 编辑前明确可观察的验收条件、兼容性和边界；多步任务写入现有 Todo 的 description。
2. 复现问题，沿调用方和共享函数追踪错误值，判断应修复的位置。
3. 为原始错误建立针对性回归，同时验证公开入口和受影响的共享行为。
4. 最后一次编辑后重跑相关检查，核对最终 diff；报告实际命令、结果、未完成检查及假设。

这是模型行为指引，执行效果仍需真实模型验证。没有将模型自报完成升级为质量保证：`completed` 继续表示 QueryLoop 自然结束，实验中的严格成功仍另外要求非空生产补丁、独立验收通过且无执行异常。

指引对代码修改请求生效，解释和审查请求维持原有任务范围，简单对话没有新增强制工具步骤。自定义 system prompt 继续由调用方控制。Worker 仍从共享 Todo 自主认领任务，没有增加 Worker 类型或父 Agent 分配表。

验收条件复用已有 Todo 描述和工具结果，不增加每轮变化的 system 字段；新版本稳定指引会改变部署后的系统前缀，单次执行中仍保持固定。

## 4. 评测运行器改动

共用 repository harness 的 `task_prompt(..., include_public_hints=False)` 默认保持原始输入。已与历史请求 JSON 中实际发送的首条用户消息逐字比对，一致；旧系统提示词 fixture 的文本也与历史请求的 system 文本一致。Windows 文本文件换行转换可能影响 `prompt.txt` 的回读，应以请求 JSON 中的消息文本做逐字比对。

只有显式开启时才附加数据集讨论；参考补丁、隐藏测试补丁和验收测试 ID 仍不进入模型提示词。运行结果记录输入与系统提示词哈希。带冻结声明的新实验会在模型工作前、评分前校验运行时、harness、任务提示及系统提示词，拒绝混入发生变化的配置。

新增 `evals/performance/verification_study.py`，默认只做离线复验和协议准备；指定 `--live` 才探测和运行真实模型。每次使用新输出目录。中转报告 `No available channel` 后停止启动后续尝试，已失败的记录仍保留。

对照为两因素四组，两个目的选取的历史案例，各重复两次，共计划 16 次：

| 组别 | 任务输入 | 系统指引 |
| --- | --- | --- |
| problem_only_before | 问题正文 | 改进前的冻结文本 |
| problem_only_acceptance | 同一问题正文 | 新验证指引 |
| discussion_before | 正文加数据集讨论 | 改进前的冻结文本 |
| discussion_acceptance | 同一正文及讨论 | 新验证指引 |

所有组采用相同当前运行时、40 次模型调用上限、420 秒硬超时、预算提醒、200K 压缩、工具和独立验收。每个案例内固定随机种子打乱组别顺序，两个案例可并行。改进前组仅复用旧提示文本，不运行旧代码。

比较指引时必须在相同输入条件内比较；讨论组属于辅助诊断条件，不能与原 problem-only 成绩合并。两个案例来自已知失败，目的在诊断机制，不代表一般编码能力。即使后续全部通过，也不足以报告普遍成功率。

## 5. 本轮实际执行数据

### 历史补丁的独立复验

| 历史样本 | 原执行状态 | 新 checkout 验收结果 |
| --- | --- | --- |
| SymPy 18087 guided | completed，严格失败 | `test_fu` 通过，`test_Factors` 失败 |
| SymPy 18087 extended | completed，严格失败 | `test_fu` 通过，`test_Factors` 失败 |
| Flask 4992 production | completed，严格成功 | 新增 TOML 目标及原有回归通过 |
| Flask 4992 off | max_turns，严格失败 | TOML 目标失败，原有回归通过 |

前三份记录：`eval_results/verification-study-20261026/study/report.json` 的 audits。第四份记录：`eval_results/verification-study-20261026/flask-off-audit/audit.json`。各目录保留实际测试输出、JUnit、源补丁及冻结测试补丁。

复验执行既验证了历史失败可重复，也保留了通过的对照。它们不是新模型尝试，不计入新的成功率分母。

### 当前源码的工程回归

- 默认全量离线测试：**626 passed，41 deselected，169.87 秒**。
- 本轮定向测试：**39 passed，11.92 秒**；包含新增 13 项评测与兼容性测试，已计入全量 626 项。
- 可靠性冒烟：原 7 场景 × 3 次，**21/21**。覆盖 Provider 断流、进程退出恢复、多进程 Todo 认领、旧 owner fencing、无冲突/冲突 Worktree 集成、未 ACK mailbox 重投。
- Ruff：核心源码、相关 repository harness、新评测模块及新增测试通过。
- mypy：核心源码、相关评测与新增测试共 **86 个文件**通过。
- Import Linter：**4 项依赖契约全部通过**。

测试覆盖输入条件隔离、冻结配置漂移拒绝、未尝试单元不计算成功率、部分正确补丁不晋升为严格成功、原始失败记录保留、离线模式不调用 Provider、端点失败后不启动 Worker，以及普通会话与自定义系统提示词兼容性。

JUnit：`offline-suite.xml`、`targeted-final.xml`；可靠性原始结果：`reliability-smoke/results.json`，均位于 `eval_results/verification-study-20261026/`。

### 真实模型状态

`study/report.json` 状态为 **endpoint_unavailable**：planned=16，attempted=0。四组成功率均为 null，而非 0%。端点探测不算编码任务尝试。

错误响应见根目录与 study 子目录各自的 `endpoint-probe.json`。运行器在开始任务前停止；不切换模型、不补造输出、不覆盖之前失败，也不将准备好的 16 个 spec 说成完成了 16 次实验。

当前可用于介绍项目的数据是 626 项离线测试和本轮 21/21 可靠性冒烟。新验证指引带来的正确完成率、成本和耗时变化，当前均无真实模型证据。

## 6. 复现

离线回归：

```powershell
uv run pytest
uv run pytest tests/test_verification_study.py tests/test_repository_evaluation.py tests/test_completion_study.py -o addopts= -q
```

离线复验历史补丁；输出路径必须尚不存在：

```powershell
uv run python -m evals.performance.verification_study --selection eval_results/readiness-20261025/real-issues/selection.json --output eval_results/verification-rerun-offline --audit eval_results/completion-study-20261025/study/runs/sympy__sympy-18087/guided
```

端点恢复后运行相同预算的四组对照；该命令消耗 API 额度：

```powershell
uv run python -m evals.performance.verification_study --selection eval_results/readiness-20261025/real-issues/selection.json --output eval_results/verification-rerun-live --repetitions 2 --live
```

该协议用于 Windows 原生 SWE-bench Lite 子集诊断，不是官方 Docker 榜单成绩，也不是上下文压缩收益实验。下一步有效证据是端点恢复后的同输入、同预算重复对照。
