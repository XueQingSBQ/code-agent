# Session Todo

Carbide 的 Todo 是会话级状态，用于保持长任务的执行方向。Todo 数据和会话消息一起存放在工作区的：

```text
.carbide/sessions.db
```

一个 session 只有一个 active plan。模型工具和交互命令使用同一个 `TodoService`，不会维护两套状态机。

## 交互命令

```text
/todo init
/todo list
/todo add "实现登录接口"
/todo add "编写登录测试" --depends <task-id>
/todo update <task-id> --status in_progress
/todo update <task-id> --status completed
/todo done <task-id>
/todo remove <task-id>
/todo checkpoint "before risky changes"
/todo checkpoints
/todo rollback <checkpoint-id>
```

修改操作会自动读取当前 `plan_version`。如果模型或其他执行者已经修改了计划，操作会被拒绝并要求重新读取 Todo，而不是覆盖新状态。

## 状态和依赖

持久化状态包括：`pending`、`in_progress`、`completed`、`failed`、`blocked`、`cancelled`。

`ready` 和“因依赖未完成而 blocked”是根据当前依赖计算出的展示状态，不单独写入数据库。依赖修改会在同一个事务中进行环检测和写入。

Todo 的数据库事务回滚只回滚 Todo 数据，不会撤销文件、Shell、Git 或其他外部副作用。

checkpoint 会保存任务、状态和依赖的精确快照，但不会增加 plan version。rollback 必须携带当前版本，成功后生成新的 plan version 和审计事件；原 checkpoint 会保留，可以重复使用。

## 上下文策略

- 首次查询或恢复会话时，如果存在计划则注入一次有上限的快照。
- 连续三个顶层查询没有成功的 Todo 修改后，下一次查询重新注入快照。
- Todo 工具返回当前快照，因此工具调用后的下一轮不依赖隐式状态才能继续工作。

Todo 不会因为每个内部工具轮次而重复注入，也不会把完整历史计划无限追加到上下文。

## Worker 自主认领

通用子 Agent 不接收父 Agent 分配的 Todo ID，而是通过共享父会话调用：

```text
todo_claim
todo_heartbeat
todo_update
todo_release
```

`todo_claim` 在 SQLite 写事务中选择一个依赖已满足的 `ready` 任务，并写入
`owner_id`、`lease_until` 和任务 revision。多个 Worker 同时竞争时只有一个
Claim 成功。完成或失败时，`todo_update` 会校验 owner 和 revision；Worker
Lease 过期后，Manager 会把未完成任务重新放回 `pending`。

`plan_version` 仍然用于保护整个计划的结构性修改，例如增加任务、删除任务
或修改依赖。任务 Claim 解决执行权，`plan_version` 解决计划写入冲突。
