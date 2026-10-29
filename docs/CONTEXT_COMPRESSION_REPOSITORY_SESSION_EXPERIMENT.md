# Carbide 真实仓库上下文压缩评测

生成时间：2026-10-09T13:22:51.819835+00:00。模型：gpt-5.6-luna。

将 4 个兼容的真实 GitHub 缺陷移植到同一 SymPy 提交，构成一个自主维护任务。
每个问题重复 3 次，已有 6/9 个运行槽位保存终态。评测尚未完成，以下结果为中期记录。

这是一项本地原生环境的 SWE-bench Lite 子集评测，不是官方 Docker Harness 的排行榜成绩。固定提交的源码通过 Git blob 哈希核验；每次运行使用独立进程、独立检出和真实 Carbide 工具。

## 样本和独立评分

模型只收到原始问题描述与环境说明，自主搜索、读取、修改、运行测试和维护 Todo。没有固定读文件顺序、重复日志填充、指定裁剪 ID 或模拟空闲时间。

样本在模型执行前按数据集顺序筛选：原始代码必须复现至少一个失败，官方补丁必须通过全部指定的FAIL_TO_PASS 和 PASS_TO_PASS 测试，原始代码的 PASS_TO_PASS 必须通过。环境不适用的样本及原因保存在 selection.json；不会根据 Agent 是否修复成功更换样本。

评分在新的检出中仅应用模型的生产 Python 代码补丁，再应用隐藏的原始回归测试补丁。模型对测试和测试配置的改动不进入评分。漏测、跳过、收集错误均不算通过。成功还要求正常完成且无运行时错误；超时和部分修复保留在分母。

## 总体结果

| 配置 | 完整任务通过 | 已报告输入 Token | 未缓存输入 | 缓存命中 | 平均 Agent 用时 | 触发压缩的运行 | 摘要请求 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 关闭压缩 | 0/1 | 760,673 | 54,881 | 92.8% | 321.8s | 0 | 0 |
| 32K 压力条件 | 0/3 | 10,719,901 | 8,115,485 | 24.3% | 1609.9s | 2 | 116 |
| 默认 200K | 0/2 | 7,219,550 | 247,646 | 96.6% | 733.3s | 0 | 0 |

## 服务异常与计量质量

| 配置 | Usage 有效完整 | 代码测试失败 | Provider 错误 | 全零或无效 Usage | 运行时失败 | 评分错误 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 关闭压缩 | 0/1 | 0 | 1 | 0 | 0 | 0 |
| 32K 压力条件 | 0/3 | 0 | 1 | 2 | 0 | 0 |
| 默认 200K | 2/2 | 2 | 0 | 0 | 0 | 0 |

另有 2 次评测器中断记录，保存在 interrupted/，不覆盖原补丁和请求。中断尝试额外记录了 97 个请求、3,227,372 个已报告输入 Token；这些消耗不混入后续新尝试的配对效果。逻辑运行槽位与实际发起的尝试分别记录。

非空请求的总输入为零视为计量无效；未缓存输入为零但缓存读取大于零仍合法。服务错误与计量异常保留在端到端失败率中，不能直接解释为模型修复能力失败。表中输入是已报告 Usage 的总和，存在缺失计量时不能当作完整消耗。

恢复仅允许改变调度并发，原始冻结配置和运行时代码保持不变。首次批量运行的并发保存在 experiment.json；新增执行阶段记录的实际并发：1, 1。遇到账号耗尽或无效 Usage 停止派发新尝试，已启动工作收尾后保存记录。

组合工作负载来源: sympy__sympy-16503, sympy__sympy-16792, sympy__sympy-17655, sympy__sympy-19254。问题移植后的官方修复组合通过独立回归测试，随后才固定工作负载。它是一个派生任务，不算四个独立样本，也不与原始提交上的单问题结果混算。


## 配对比较

| 配置 | Usage 完整的配对 | 全尝试加权输入减少 | 各任务平均输入减少 | 任务 bootstrap 95% 区间 | 双方均成功的配对 | 双方均成功时输入减少 |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| 32K 压力条件 | 0 | 无完整配对 | 无完整配对 | 样本不足 | 0 | 无完整配对 |
| 默认 200K | 0 | 无完整配对 | 无完整配对 | 样本不足 | 0 | 无完整配对 |

## 实际压缩触发与额外开销

| 配置 | 峰值单请求输入 | 压缩事件 | 触发层 | 摘要输入 Token | 净释放估算 Token | 有效配对中触发压缩 | 配对未缓存输入减少 |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| 关闭压缩 | 41,140 | 0 | 无 | 0 | 0 | 0 | 无完整配对 |
| 32K 压力条件 | 51,908 | 116 | L4.5:restore, L4:compact | 4,530,113 | 58,267 | 0 | 无完整配对 |
| 默认 200K | 75,823 | 0 | 无 | 0 | 0 | 0 | 无完整配对 |

净释放是压缩器的本地估算，不是累计 Provider 输入减少。摘要输入和文件恢复会消耗上下文；多次摘要、缓存失效可能使未缓存输入增加。只有实际触发压缩的运行能够观察压缩机制，仍需结合完整任务通过率判断是否有效。

所有摘要请求均计入 Provider Usage。缓存输入也属于总输入；未验证实际中转单价，不把 Token 减少换算为费用减少。Usage 不完整的尝试保留在成功率中，但不会作为零输入制造节省。

全尝试指标可能受提前失败影响，必须和成功率一起看；双方均成功的指标仅为补充。置信区间按问题聚类：先对同一问题的重复结果求均值，再重采样问题，不能把模型请求当独立样本。

## 逐任务结果

| 真实问题 | 关闭压缩通过 | 默认 200K 通过 | 32K 压力条件通过 | 实际触发层 |
| --- | ---: | ---: | ---: | --- |
| sympy-maintenance-4-issues | 0/1 | 0/2 | 0/3 | L4.5:restore, L4:compact |

## 失败样本

- sympy-maintenance-4-issues/off/repeat=1：类别 provider_error；结束原因 provider_error；运行错误 ['{\'reason\': \'provider_error\', \'message\': "Error code: 529 - {\'type\': \'error\', \'error\': {\'type\': \'overloaded_error\', \'message\': \'No available accounts. All accounts are expired or rate-limited.\'}}"}']；评分错误 无；未通过的指定测试 {'test_pretty_sum': 'failed', 'test_dup_zz_mignotte_bound': 'failed', 'test_dmp_zz_mignotte_bound': 'failed', 'test_dmp_zz_wang': 'failed'}。
- sympy-maintenance-4-issues/pressure/repeat=1：类别 invalid_provider_usage；结束原因 None；运行错误 ['TimeoutError: ']；评分错误 无；未通过的指定测试 {'test_pretty_sum': 'failed', 'test_ccode_unused_array_arg': 'failed', 'test_point': 'failed', 'test_point3D': 'failed', 'test_dup_zz_mignotte_bound': 'failed'}。
- sympy-maintenance-4-issues/production/repeat=1：类别 test_failure；结束原因 completed；运行错误 []；评分错误 无；未通过的指定测试 {'test_pretty_sum': 'failed', 'test_dup_zz_mignotte_bound': 'failed', 'test_dmp_zz_mignotte_bound': 'failed', 'test_dmp_zz_wang': 'failed'}。
- sympy-maintenance-4-issues/pressure/repeat=3：类别 provider_error；结束原因 provider_error；运行错误 ['{\'reason\': \'provider_error\', \'message\': "Error code: 529 - {\'type\': \'error\', \'error\': {\'type\': \'overloaded_error\', \'message\': \'No available accounts. All accounts are expired or rate-limited.\'}}"}']；评分错误 无；未通过的指定测试 {'test_pretty_sum': 'failed', 'test_ccode_unused_array_arg': 'failed', 'test_dup_zz_mignotte_bound': 'failed', 'test_Routine_argument_order': 'failed', 'test_dmp_zz_mignotte_bound': 'failed', 'test_dmp_zz_wang': 'failed'}。
- sympy-maintenance-4-issues/production/repeat=2：类别 test_failure；结束原因 completed；运行错误 []；评分错误 无；未通过的指定测试 {'test_pretty_sum': 'failed', 'test_dup_zz_mignotte_bound': 'failed', 'test_dmp_zz_mignotte_bound': 'failed', 'test_dmp_zz_wang': 'failed'}。
- sympy-maintenance-4-issues/pressure/repeat=2：类别 invalid_provider_usage；结束原因 None；运行错误 ['TimeoutError: ']；评分错误 无；未通过的指定测试 {'test_pretty_sum': 'failed', 'test_ccode_unused_array_arg': 'failed', 'test_point': 'failed', 'test_point3D': 'failed', 'test_dup_zz_mignotte_bound': 'failed', 'test_Routine_argument_order': 'failed'}。

## 解释边界

- 默认预算与压力条件分别报告；32K 的结果不能写成默认 200K 的生产效果。
- 短任务没有触发压缩时，差异主要来自模型执行路径和缓存，不能归因于压缩。
- 这些是真实缺陷修复任务，但仅覆盖两个 Python 仓库，不覆盖前端、部署、持续多需求开发或多 Agent。
- 没有强制触发 L2/L3；如未触发，只能说明本次工作负载未覆盖，不等于验证其生产效果。
- 时间包含真实工具执行与 API 等待；并发负载和中转缓存影响耗时，不能给出生产 SLA。
- 历史合成实验的 41.1% 和 88.7% 不与这次真实任务混算。

## 复现与原始记录

```powershell
uv run python -m evals.context_efficiency.repository_session `
  --source eval_results/context-efficiency/repository-ab `
  --output eval_results/context-efficiency/repository-session --issues 4
```

输出包含冻结配置和运行时代码指纹、原始数据集、源码哈希、依赖清单、预检查、每次模型请求的完整上下文及 Usage、工具事件、提交补丁、独立测试 XML 和失败日志。同目录重跑只读取已完成记录，不把失败偷偷重试成成功。
