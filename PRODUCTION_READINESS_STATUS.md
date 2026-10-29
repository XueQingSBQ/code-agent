# Carbide 生产就绪状态

更新时间：2026-09-19  
判定：**尚未通过完整 production-ready 门禁；核心运行时已达到可持续验收的 Alpha/Beta 前置状态。**

## 已完成并有自动化证据

| 领域 | 当前结果 | 证据 |
| --- | --- | --- |
| QueryLoop 主流程 | 已接入流式事件、工具执行、故障恢复、终止原因 | 全量离线测试、`test_agent_lifecycle.py` |
| 工具协议 | `tool_use` 与 `tool_result` 配对，工具失败不会破坏下一轮历史 | `test_continuation.py`、`test_agent_lifecycle.py` |
| Bash 生命周期 | 持久 Shell、超时/取消清理、后台 PID 登记、会话归属、attached 回收和 detached 保留 | `test_bash_session.py`、`test_background_jobs.py`、`test_agent_lifecycle.py` |
| 搜索工具 | Glob 跳过不可访问项；Grep 有大小限制、超时和取消清理 | `test_glob.py`、搜索相关回归测试 |
| 上下文压缩 | L1-L4/L4.5 有运行时覆盖；恢复路径限制在 workspace 内 | `test_compression_runtime.py`、`test_context_boundaries.py` |
| 记忆模块 | 抽取、敏感信息拒绝、持久化、召回和失败降级已覆盖 | `test_memory_extraction.py` |
| 治理 | PermissionEngine、BashGuard、HookRegistry、审计接入主路径 | `test_governance_runtime.py`、生命周期测试 |
| 审计日志 | JSONL、立即 flush、凭据脱敏、字段截断、大小轮转 | `test_audit_logger.py` |
| SessionStore | SQLite WAL、状态校验、跨实例恢复、完整性检查、备份/原子恢复、schema 版本与迁移入口 | `test_session_store.py`、`test_session_backup.py` |
| CLI | 中间进度、终止原因、会话列表、后台 jobs 状态/停止、备份/检查/恢复命令 | `test_cli_status.py`、`test_cli_jobs.py`、CLI/会话测试 |
| 质量门禁 | 离线测试与静态检查可重复执行 | 最近一次：全量离线测试通过、零 warning；核心源码与评测入口 Ruff/mypy/import-linter 通过 |
| SWE-bench 评测入口 | 数据校验、容器生命周期、容器 Bash 隔离、资源上限、超时透传、终止原因和报告落盘有离线覆盖 | `test_eval_harness.py`（15 项） |
| 构建 | wheel/sdist 构建和隔离 CLI smoke test 已通过 | `uv build` |
| 真实 API 基础矩阵 | Anthropic-compatible endpoint 的流式文本、工具调用、多轮读写、usage 和 cache-read 字段均已实测通过 | `tests/test_api.py`、`tests/test_live_api_matrix.py` |
| 离线性能基线 | 1000 文件、20 次迭代、8 并发的工具/压缩/Agent 编排基线已生成，全部成功 | `eval_results/performance/offline-baseline.json` |

## 当前还未关闭的发布门禁

这些项目不是代码是否存在的问题，而是还缺少可审计的真实运行证据：

1. 覆盖率路线图目标为 85%+，当前约 79%。低覆盖集中在 QueryLoop 深层故障分支、CLI 主入口、Provider 抽象、BashSession 和 memory writer。
2. 真实 Anthropic-compatible endpoint 的基础多轮与工具调用矩阵已通过；速率限制恢复、断线恢复和长时间 API 稳定性仍未完成。
3. 尚未完成 24 小时或等价长时间 soak test，无法宣称内存、子进程和 SQLite 句柄长期稳定。
4. 已建立离线工具/压缩/并发 Agent 基线；API P95、内存增长、长时间并发会话吞吐仍未完成。
5. 尚未完成 Docker 隔离、最小权限运行用户、镜像扫描和部署回滚验证。
6. 当前有本地 JSONL 审计轮转与脱敏，但尚未接入生产日志聚合、告警、保留策略和集中查询。
7. 数据库已支持人工/CLI 备份恢复和本地 schema migration，但尚未有定时备份、异地保存、真实恢复演练和 migration 发布流程。
8. 本次本地验证环境无法连接 Docker Desktop Linux daemon，且未配置真实 Anthropic-compatible API key；因此真实容器 smoke 和真实 endpoint 矩阵目前只能保留为外部发布门禁。

## P0-P2 进度

### P0：阻塞项

- [x] 工具失败、Provider 断流、取消和 `Ctrl+C` 的历史修复。
- [x] Bash 超时/取消/退出时的进程及 transport 清理。
- [x] 后台 Bash PID 登记、`.carbide/jobs/` 持久化、会话归属隔离和 Agent 退出回收；`carbide jobs` 运维入口。
- [x] workspace 路径边界、Glob/Grep 输入和大结果处理。
- [x] 关键配置与审计参数的启动期校验。
- [x] 真实 endpoint 基础流式/工具/多轮证据；速率限制、断线、长时间运行和 API 性能证据仍待完成。
- [x] SWE-bench 评测入口的离线生命周期与参数透传验证；真实 Docker/API 端到端仍待执行。

### P1：高优先级

- [x] PermissionEngine、BashGuard、AuditLogger 主路径接入及回归测试。
- [x] SessionStore 持久化、恢复、完整性检查、备份和原子恢复。
- [x] 审计脱敏、字段上限、轮转和 CLI 运维入口。
- [ ] 生产日志聚合/告警和数据库定时备份策略。
- [ ] 85% 覆盖率目标。

### P2：完善项

- [x] L4.5 文件恢复边界和 L1 总消息阈值。
- [x] CLI 会话检查/备份/恢复命令。
- [x] SWE-bench 容器资源限制和 Agent Bash 容器通道；完整最小权限、镜像扫描、部署和恢复演练仍待完成。
- [x] schema migration 工具和旧库/未来版本保护；真实部署恢复演练仍待完成。
- [x] 离线 performance benchmark 报告；24 小时 soak、API P95 和内存增长仍待完成。

## 发布建议

当前适合：

- 可信开发机上的内部试用；
- 使用 mock provider 的集成回归；
- 以“具备生产化设计和核心故障恢复能力的 Agent runtime 原型”作为面试或技术展示。

当前不适合直接宣称：

- 已通过 24 小时稳定性验证；
- 已验证某个真实模型 endpoint 的 SLA/P95；
- 已完成 Docker 沙箱和生产运维闭环；
- 已达到完整 production-ready 或 v1.0。

关闭剩余门禁后，建议在 clean clone 中重新执行 README 的 release gate，并保存测试、覆盖率、压测、容器扫描和恢复演练产物，再更新本文件的最终判定。
