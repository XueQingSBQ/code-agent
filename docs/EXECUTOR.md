# 工具执行器

Carbide 只保留一套生产工具执行器：

```text
src/carbide/engine/executor.py
└── StreamingToolExecutor
```

`QueryLoop` 直接使用这个执行器。旧的 `engine/executor_old.py` 和另一套
`execution/executor.py` 已删除，避免两套并发、权限和事件模型继续漂移。

## 执行流程

```text
Provider stream
    ↓ 识别完整 tool_use
StreamingToolExecutor.add_tool()
    ↓ 后台任务
权限检查 → BashGuard → before hook → ToolRegistry.invoke()
    ↓
after hook → AuditLogger / metrics
    ↓
gather_remaining()
    ↓ 按派发顺序生成 tool_result
QueryLoop 发起下一次模型请求
```

## 并发规则

- 只读且声明 `concurrency_safe=True` 的工具可以并行执行。
- Write、Edit、Bash 等有副作用的工具是屏障，会等待之前的任务，并阻止后续任务越过它。
- 未注册或未声明安全属性的工具默认按不安全处理。
- 执行结果始终按照模型派发顺序返回，而不是按照完成时间排序。

## 失败与取消

每个 `tool_use` 都必须生成对应的 `tool_result`。工具异常会转换为错误结果，不能因为
一个工具失败而取消其他工具。取消时，执行器会取消未完成任务，并为每个调用生成错误
结果，确保下一次 Provider 请求仍满足消息协议。

因此这里没有使用 `asyncio.TaskGroup`：TaskGroup 的默认异常传播会取消兄弟任务，破坏
“每个 tool_use 必须有 tool_result”的历史不变量。

## 测试入口

当前执行器的回归测试位于：

```text
tests/test_executor.py
```

压缩、重试和执行指标仍分别由以下模块负责：

```text
src/carbide/execution/compression.py
src/carbide/execution/retry.py
src/carbide/execution/metrics.py
```

它们是执行器之外的独立能力模块，不包含另一套工具调度实现。
