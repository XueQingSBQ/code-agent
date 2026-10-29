# Carbide 可靠性缺口修复与复测

日期：2026-10-25。本轮只处理上一轮可靠性评测暴露的两处缺口：拒绝旧 Worker 消息后的事务残留，以及 Mailbox 未确认消息的恢复和消费一致性。不改写原失败基线，不重算真实模型任务成绩。

## 1. 修复范围

### 1.1 拒绝旧 Worker 后，合法写入仍可继续

`SessionStore.append_agent_message()` 改用显式事务：条件 INSERT、唯一键冲突、外键错误等异常均回滚。事务边界也覆盖取消和 commit 失败，不留下阻塞后续 `BEGIN IMMEDIATE` 的事务。

新增测试检查拒绝后连接不处于事务中，同一 store 可以继续事务写入，另一个真实 SQLite 连接也能取得写锁。原评测还继续检查旧 Worker 被拒绝之后，新 Worker 能合法完成 Todo。

### 1.2 Mailbox 的投递租约和消费边界

数据库从 v6 升级到 v7，为消息增加 `delivery_owner`、`delivery_token`、`delivery_until`。投递状态如下：

```text
queued ──原子领取──> delivered + 投递租约 + 独立凭证
                       │
                       ├─显式关闭消费者：释放其未确认消息，重新 queued
                       ├─进程非正常退出：租约到期后重新领取，生成新凭证
                       └─同一事务写入会话上下文并 ACK──> acknowledged
```

- 默认投递租约为 30 秒。持久化时间使用 UTC；故障注入使用短租约加速测试。
- 显式 `SessionStore.close()` 释放当前消费者尚未确认的消息；Agent 的 ephemeral 连接回收不等于消费者关闭，不会提前释放租约。
- 新消费者重领后，旧消费者的 ACK/上下文写入凭证失效；旧消费者关闭也不能释放新消费者的租约。
- 管理器只负责领取，不再提前 ACK。QueryLoop 在模型请求边界，将上下文写入与 ACK 放入同一个 SQLite 事务，提交成功后才加入内存历史。
- 同一消息再次接受时返回重复标记，不重复写入上下文或发出子消息事件；接收会话必须与消息方向、父子关系匹配。
- 存储异常不再被消息注入路径静默吞掉：当前 query 报告 `mailbox_error` 并停止进入模型请求，消息保持可恢复状态。
- 保留 `acknowledge_agent_message()` 给非上下文消费者，但要求当前投递凭证。Agent 运行路径必须使用原子接受接口，不能自行拆开上下文写入和 ACK。

v6 中 `delivered` 且没有投递租约的历史消息可重新领取；已经 `acknowledged` 的历史消息不自动重放，因为旧库无法可靠判断它是否曾进入模型上下文。升级不能追溯修复旧版本已经发生的提前 ACK 丢失窗口。

## 2. 新增回归测试

文件：`tests/test_mailbox_recovery.py`，共 20 个参数化实例。

覆盖内容：拒绝旧 owner、重复消息 ID、缺失 run 的失败回滚；事务取消回滚；持久/ephemeral store 的关闭语义；租约过期重领和旧凭证拒绝；双向接收范围校验与去重；上下文 INSERT、ACK UPDATE 两处故障注入；v6 迁移；四个独立 SQLite 连接并发消费 24 条消息；QueryLoop 错误可见性和重复注入。

其中 4 个是真实进程强制退出用例：父→子、子→父分别在领取后和提交后退出。提交后用例通过真实 QueryLoop 接受路径，在事务提交后、内存 append 前执行 `os._exit(23)`。恢复后使用新的 SDK Agent 加载会话并继续查询，检查消息恰好出现一次。模型使用确定性 Provider，不调用付费 API。

## 3. 原协议复测与质量检查

原始失败基线保留于：`eval_results/readiness-20261025/reliability-final/results.json`。

本轮最终结果：`eval_results/readiness-fix-20261025/reliability-final/results.json`。复用原 7 场景 × 20 轮，未修改评测脚本、场景顺序或通过标准。

| 场景 | 修复前 | 修复后 |
| --- | ---: | ---: |
| 工具派发后 Provider 断流，再恢复会话 | 20/20 | 20/20 |
| 子进程非正常退出，再恢复会话 | 20/20 | 20/20 |
| 8 个操作系统进程同时抢同一 Todo | 20/20 | 20/20 |
| 租约到期，拒绝旧 Worker 写入，新 Worker 完成 | 0/20 | 20/20 |
| 两个 Worktree 并发集成不重叠修改 | 20/20 | 20/20 |
| 两个 Worktree 并发修改同一行，冲突阻断 | 20/20 | 20/20 |
| 消息投递后、ACK 前显式关闭并重建消费者 | 0/20 | 20/20 |
| **合计** | **100/140** | **140/140** |

原协议复测退出码为 0。这里的 140 次只指固定组件故障注入场景，不是 140 个真实模型编码任务，更不能解释为线上可用率 100%。评测与全量 pytest 在同一机器并行执行，不以时延差异推导性能提升。本轮没有调用付费模型 API。

新增消息测试的机器可读记录：`eval_results/readiness-fix-20261025/mailbox-regression.xml`。全量默认离线测试记录：`eval_results/readiness-fix-20261025/offline-suite.xml`。

质量检查结果：

- 最终全量默认离线测试：**595 passed，41 deselected**，160.28 秒；JUnit 记录 595 个测试、0 failures、0 errors。41 项 live 测试没有运行，不计入通过数。
- 新增消息回归：**20 passed**，28.75 秒。它们已经包含在上述 595 项中，不能再相加。
- Ruff：`src/carbide`、`evals/performance`、`tests/test_reliability_evaluation.py`、`tests/test_mailbox_recovery.py`、`tests/test_subagents.py` 均通过。本轮没有宣称全部历史 tests 的 Ruff 已通过。
- mypy：`src/carbide tests/test_mailbox_recovery.py --follow-imports=silent` 通过，共 83 个文件。
- 可靠性复测启动时的 runtime SHA-256 为 `c3e295c1111492dd6475b0c275dd24c1cda81a2e52d5b676552a94c5ede4bee4`，完成后再次计算一致。
- 评测脚本 SHA-256 与原失败基线完全相同：`721cc7b3abcd7f4e400d891e6b05f13e95c696a7579ead48dcacf44a0fd35355`。

## 4. 能力保证与边界

这里实现的是**有租约、可重投的消息传输，以及去重的持久化上下文写入**。不是模型调用、Shell 命令、文件写入等外部副作用的端到端 exactly-once，也不是分布式消息中间件或安全沙箱。模型可能在处理同一已恢复历史时再次推理，外部操作仍需要各自的幂等或验收机制。

租约过期意味着消息可被下一次领取接口重新获取，不是独立后台投递服务；仍需接收方继续 query/恢复会话来消费。也不会把旧 child run 的指令自动迁移给另一个新建 Worker。

原可靠性协议中的 Mailbox 场景是显式关闭/重开 store，不应描述为进程崩溃。真实进程退出窗口由本轮新增的四个测试单独验证，不能把它们混入原 140 次场景执行。

上一轮真实模型任务的严格成功仍是 2/12；Worker 两组试跑仍未完成整体维护任务；默认压缩未触发，也没有新增可用的压缩收益或多 Agent 加速比证据。修复可靠性缺口，不等于真实编码任务已经全面达到生产验收。

后续优先级仍是分析真实任务轮数/时长耗尽与错误收尾，再改善 Worker 已完成结果的逐个持久化和质量验收，之后以新输出目录做独立实测。

## 5. 复现

在项目根目录执行；可靠性输出必须选择尚不存在的新目录：

```powershell
uv run pytest tests/test_mailbox_recovery.py
uv run pytest
uv run python -m evals.performance.reliability --output eval_results/readiness-fix-rerun/reliability-final --repetitions 20
uv run ruff check src/carbide evals/performance tests/test_reliability_evaluation.py tests/test_mailbox_recovery.py
uv run mypy src/carbide --follow-imports=silent
uv run mypy tests/test_mailbox_recovery.py --follow-imports=silent
```
