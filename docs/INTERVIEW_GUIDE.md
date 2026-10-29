# Carbide 框架面试问答指南

> 针对 M1/M2/M3 里程碑的技术深度问答

---

## 目录

1. [项目概述](#项目概述)
2. [M1 工具层问答](#m1-工具层问答)
3. [M2 治理层问答](#m2-治理层问答)
4. [M3 执行层问答](#m3-执行层问答)
5. [架构设计问答](#架构设计问答)
6. [性能优化问答](#性能优化问答)
7. [测试策略问答](#测试策略问答)

---

## 项目概述

### Q1: 请简单介绍一下 Carbide 项目

**A:** Carbide 是一个生产级的 AI Agent 开发框架，专注于工具执行和治理。整个项目分为 4 个里程碑：

- **M1 工具层**：6+ 基础工具（Read/Write/Edit/Bash/Glob/Grep），工具注册表，类型安全的参数验证
- **M2 治理层**：4 种权限模式，5 个 Hook 事件点，命令安全分析，JSONL 审计日志，SQLite 会话存储
- **M3 执行层**：流式工具执行器，基于工具契约的屏障式并发，上下文压缩，重试机制和性能监控
- **M4 应用层**（规划中）：Claude API 集成，对话管理，记忆系统

目前 M1/M2/M3 的核心链路已完成，M4 的记忆、技能和应用层能力正在持续完善；测试数量以当前 `pytest --collect-only` 结果为准，不在文档中固化历史数字。

### Q2: 这个项目的技术栈是什么？

**A:**
- **语言**：Python 3.12+（充分利用 async/await、类型标注、dataclass）
- **核心库**：
  - `asyncio` - 异步 I/O 和并发控制
  - `pydantic` - 数据验证和序列化
  - `sqlite3` - 会话持久化
- **测试**：`pytest` + `pytest-asyncio`（当前收集 482 个测试）
- **代码质量**：`ruff`（linter）+ `mypy`（类型检查）

### Q3: 你在这个项目中扮演什么角色？

**A:** 我从零开始设计并实现了整个框架：
1. **需求分析**：研究 Claude Code 等现有方案，提炼核心需求
2. **架构设计**：三层架构（工具层/治理层/执行层），定义清晰的接口边界
3. **核心实现**：独立完成工具、治理、执行、上下文和持久化等核心模块
4. **测试编写**：当前收集 482 个测试用例，覆盖核心链路和故障分支
5. **文档编写**：完整的技术文档和 API 文档

---

## M1 工具层问答

### Q4: M1 工具层的设计理念是什么？

**A:** M1 的核心理念是**类型安全 + 契约优先**：

1. **类型安全**：每个工具必须定义 `params` 模型（Pydantic），保证参数验证
2. **契约检查**：工具注册时双重检查 `read_only`、`destructive`、`concurrency_safe` 标志
3. **统一接口**：所有工具继承 `Tool` 基类，实现 `async def run()` 方法
4. **错误处理**：从不抛出异常给调用者，返回 `ToolResult(is_error=True)`

这样设计的好处：
- 编译时捕获错误（mypy 类型检查）
- 运行时参数自动验证（Pydantic）
- 防止并发安全问题（显式标记）

### Q5: 为什么工具执行返回 ToolResult 而不是直接抛异常？

**A:** 这是故意的设计选择，原因：

1. **AI 友好**：LLM 可以看到完整的错误信息并重试，而不是中断对话
2. **一致性**：所有工具失败都返回统一的结构（`ToolResult(content, is_error)`）
3. **可观测**：错误信息可以记录到审计日志，便于分析
4. **简化调用**：调用方不需要 try-except 包裹每个工具调用

示例：
```python
# 不好的设计（抛异常）
try:
    result = await tool.run(args)
except FileNotFoundError as e:
    # 如何告诉 LLM？
    
# 好的设计（返回结果）
result = await tool.run(args)
if result.is_error:
    # LLM 可以看到 result.content 并决定下一步
```

### Q6: ToolRegistry 为什么要双重检查工具契约？

**A:** 防御性编程，应对两种风险：

1. **动态构造的工具**：不是所有工具都在编译时定义，可能来自插件或动态加载
2. **运行时修改**：虽然不常见，但类属性可以被运行时修改

双重检查位置：
- `Tool.__init_subclass__`：类定义时检查（编译时）
- `ToolRegistry.register()`：注册时检查（运行时）

代码：
```python
class ToolRegistry:
    def register(self, tool: Tool):
        for attr in ('read_only', 'destructive', 'concurrency_safe'):
            if not isinstance(getattr(type(tool), attr), bool):
                raise TypeError(f"Missing or invalid {attr}")
```

这种双重保险在高并发场景下非常重要，错误的 `concurrency_safe` 标志可能导致文件损坏。

### Q7: 持久化 Bash 会话是怎么实现的？

**A:** 关键设计：

1. **进程池管理**：`BashSessionManager` 维护 session_id → subprocess 的映射
2. **输出缓冲**：每个会话有独立的 `OutputBuffer`，存储最近 1000 行输出
3. **异步读取**：后台线程持续读取 stdout/stderr，避免管道阻塞
4. **优雅关闭**：会话结束时发送 `exit` 命令，等待进程退出

核心代码：
```python
class BashSession:
    def __init__(self):
        self.process = subprocess.Popen(
            ["bash"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1  # 行缓冲
        )
        self._start_output_reader()
    
    def _start_output_reader(self):
        """后台线程读取输出"""
        def reader():
            for line in self.process.stdout:
                self.buffer.append(line)
        threading.Thread(target=reader, daemon=True).start()
```

**难点**：处理子进程的僵尸进程和输出缓冲区满的问题。

---

## M2 治理层问答

### Q8: M2 治理层解决了什么问题？

**A:** M2 解决的是**生产环境的安全和合规**问题：

1. **权限控制**：不是所有工具都能随时执行（例如生产环境禁止 `rm -rf`）
2. **审计合规**：企业需要完整的操作审计日志（谁、何时、做了什么）
3. **拦截机制**：需要在工具执行前后插入自定义逻辑（例如速率限制、成本控制）
4. **会话恢复**：长对话需要持久化，支持中断恢复

M2 的设计参考了云平台的 IAM（Identity and Access Management）模式。

### Q9: 为什么设计 4 种权限模式？各自的使用场景是什么？

**A:** 4 种模式对应不同的信任级别和使用场景：

| 模式 | 场景 | 典型用户 |
|-----|------|---------|
| **ALWAYS_ALLOW** | 自动化脚本、CI/CD | 信任的自动化环境 |
| **PROMPT** | 交互式开发 | 开发者日常工作（默认） |
| **AUDIT** | 监控审计模式 | 安全团队观察行为 |
| **DENY** | 生产环境只读 | 生产环境故障排查 |

**设计权衡**：
- 最初只有 AUTO/ALWAYS/NEVER 三种模式（M2 早期）
- 后来发现需要"允许执行但记录日志"的场景 → 增加 AUDIT 模式
- PROMPT 模式平衡了安全性和易用性

### Q10: Hooks 系统的设计灵感来自哪里？为什么选择 5 个事件点？

**A:** 灵感来自：
- **Git Hooks**：pre-commit, post-commit 的设计
- **Web 框架中间件**：Django middleware, Express.js middleware
- **数据库触发器**：BEFORE/AFTER triggers

5 个事件点的选择：
```python
PRE_TOOL       # 工具执行前（安全检查、参数验证）
POST_TOOL      # 工具执行后（日志记录、结果转换）
ON_ERROR       # 异常发生时（错误监控、告警）
PRE_API_CALL   # API 调用前（速率限制、成本控制）
POST_API_CALL  # API 返回后（响应缓存、指标收集）
```

**为什么是 5 个而不是更多？**
- 太少（只有 PRE/POST）：无法处理异常和 API 调用
- 太多：增加复杂度，大部分场景用不到

### Q11: BashGuard 如何检测危险命令？为什么用正则而不是 AST？

**A:** BashGuard 使用**正则表达式 + 模式匹配**：

检测的危险模式：
```python
DESTRUCTIVE_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)*-[a-zA-Z]*f",  # rm -rf
    r"\bdd\s+if=/dev/zero",                            # dd 磁盘擦除
    r":\(\)\s*\{[^}]*:\|:&\s*\}",                     # fork bomb
    r"\bchmod\s+-R\s+777",                             # 递归 777
    r"\b(curl|wget)\b[^|]*\|\s*(bash|sh)",            # 下载并执行
]
```

**为什么不用 AST（tree-sitter/bashlex）？**

优点：
- ✅ 无外部依赖（tree-sitter 需要 C 编译）
- ✅ 快速启动（正则预编译）
- ✅ 足够覆盖常见危险模式
- ✅ 易于扩展（添加新模式很简单）

缺点：
- ❌ 无法处理复杂混淆（如 `r''m -rf /`）
- ❌ 可能误报（注释中的模式）

**未来计划**：M4 可选升级到 tree-sitter，提供精确 AST 分析作为高级选项。

### Q12: 审计日志为什么选择 JSONL 格式？

**A:** JSONL（JSON Lines）的优势：

1. **追加友好**：每行一个 JSON，追加不需要解析整个文件
2. **流式处理**：可以逐行读取，内存占用恒定
3. **工具链丰富**：`jq`、`grep`、`awk` 都能处理
4. **崩溃安全**：写一半崩溃也不会损坏已有记录

对比其他格式：
- **JSON 数组**：追加需要修改文件结尾（`]`），不崩溃安全
- **CSV**：类型信息丢失，嵌套数据难处理
- **数据库**：需要额外依赖，运维成本高

代码实现：
```python
class AuditLogger:
    def log(self, entry: AuditEntry):
        line = json.dumps(entry.dict()) + "\n"
        self._file.write(line)
        self._file.flush()  # 立即刷盘，崩溃安全
```

### Q13: SessionStore 为什么选择 SQLite 而不是其他数据库？

**A:** SQLite 的独特优势：

1. **零配置**：单文件数据库，无需服务器
2. **嵌入式**：直接链接到进程，无网络开销
3. **ACID 事务**：完整的事务支持
4. **WAL 模式**：Write-Ahead Logging 提升并发性能
5. **跨平台**：Windows/Linux/macOS 内置

对比其他方案：
- **PostgreSQL/MySQL**：需要额外服务，运维成本高
- **JSON 文件**：无事务支持，并发不安全
- **内存存储**：无法持久化，不支持 --resume

Schema 设计：
```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL,  -- running/completed/failed
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,     -- user/assistant
    content TEXT NOT NULL,  -- JSON 序列化
    created_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
```

**性能优化**：`PRAGMA journal_mode=WAL` 允许读写并发。

---

## M3 执行层问答

### Q14: M3 执行层的核心价值是什么？

**A:** M3 将独立的组件**编排成智能的执行引擎**：

1. **流式反馈**：模型输出工具调用时立即派发，工具完成后再按顺序回填结果
2. **安全并行**：只读且声明安全的工具可以并发，写入、编辑和 Bash 通过屏障保证顺序
3. **资源管理**：Token 预算自动压缩，重试机制容错
4. **可观测性**：完整的性能指标和瓶颈分析

**类比**：
- M1 是"工具箱"（提供工具）
- M2 是"安全检查站"（控制权限）
- M3 是"项目经理"（智能调度）

### Q15: StreamingToolExecutor 如何实现边流边派发和故障隔离？

**A:** 当前唯一生产执行器是
`carbide.engine.executor.StreamingToolExecutor`。QueryLoop 解析到完整的
`tool_use` 后立即调用 `add_tool()` 创建后台任务，模型继续流式输出；回合结束时
通过 `gather_remaining()` 按模型派发顺序收集结果。

```python
executor.add_tool(call)
completed = await executor.gather_remaining()
tool_result_blocks = executor.to_result_blocks(completed)
```

它有四个关键设计：

1. `concurrency_safe=True` 的只读工具可以并行运行；写入、编辑和 Bash 作为屏障。
2. 结果按派发顺序返回，不受实际完成顺序影响，保证历史稳定。
3. 单个工具抛异常时转换成错误的 `tool_result`，不会取消兄弟任务。
4. 权限、BashGuard、Hooks、审计和指标都在同一个执行路径中完成。

CLI 和 SDK 消费的是 QueryLoop 的 `tool_use`、`tool_complete` 等公共事件；执行器本身
负责调度、治理和结果收集，事件对外统一由 QueryLoop 定义。

### Q16: 执行器如何决定哪些工具可以并发？

**A:** 当前实现不构建跨调用的拓扑依赖图，而是使用更容易审计的**屏障式并发策略**：

1. 工具通过 `concurrency_safe` 契约声明是否允许并发。
2. 安全工具（例如只读工具）只等待最近的副作用屏障，因此相邻的多个只读调用可以并行。
3. 不安全工具（Write、Edit、Bash 或未知工具）等待所有已派发任务完成，并成为后续调用的新屏障。
4. 结果始终按模型派发顺序收集，避免完成顺序改变对话历史。

```python
executor.add_tool(read_a)   # 可立即运行
executor.add_tool(read_b)   # 与 read_a 并行
executor.add_tool(write_a)  # 等待前面任务，并建立屏障
executor.add_tool(read_c)   # 等待 write_a
completed = await executor.gather_remaining()
```

这种设计牺牲了部分理论上的并行度，换取可预测的文件语义和较低的调度复杂度。性能收益来自独立只读 I/O 的重叠执行，实际提升应通过基准测试报告，而不是写死一个固定倍数。

### Q17: 上下文压缩的策略是什么？压缩率如何？

**A:** 三段式压缩策略：

```
[保留前 N 条] + [压缩中间部分] + [保留后 N 条]
```

具体实现：
1. **保留前 2 条**：系统消息、初始上下文（1000 tokens）
2. **压缩中间**：统计工具调用数量，生成摘要（200 tokens）
   - 示例：`[Conversation history compressed: 15 tool calls, 14 results]`
3. **保留后 5 条**：最近的对话（2500 tokens）

**压缩效果**：
```
原始：100 条消息 × 500 tokens = 50,000 tokens
压缩后：3,700 tokens
压缩率：93% (3,700 / 50,000)
```

**自适应调整**：
- 使用率 > 90%：激进压缩（keep_first=1, keep_last=3）
- 使用率 80-90%：标准压缩（keep_first=2, keep_last=5）
- 使用率 < 80%：轻度压缩（keep_first=3, keep_last=7）

### Q18: 重试机制的指数退避是怎么实现的？

**A:** 指数退避算法：

```python
delay = initial_delay * (backoff_factor ** attempt)
delay = min(delay, max_delay)  # 上限保护
```

示例：
```
initial_delay = 1.0s, backoff_factor = 2.0

attempt 0: 1.0 * 2^0 = 1.0s
attempt 1: 1.0 * 2^1 = 2.0s
attempt 2: 1.0 * 2^2 = 4.0s
attempt 3: 1.0 * 2^3 = 8.0s
```

**为什么用指数退避而不是固定延迟？**
- 固定延迟：重试过快可能雪崩（例如 API 限流）
- 线性增长：恢复时间过长
- 指数退避：快速尝试 → 逐渐放慢，平衡重试速度和系统压力

**可配置的预定义策略**：
```python
DEFAULT_RETRY = RetryPolicy(max_retries=3, backoff_factor=2.0)
AGGRESSIVE_RETRY = RetryPolicy(max_retries=5, initial_delay=0.5)
CONSERVATIVE_RETRY = RetryPolicy(max_retries=2, backoff_factor=3.0)
```

### Q19: 性能监控如何识别瓶颈？

**A:** 两个维度分析：

1. **时间瓶颈**：`identify_bottlenecks(threshold_ms=1000)`
   - 找出平均执行时间 > 1000ms 的工具
   - 示例输出：`bash: avg 1500ms (max 2500ms)`

2. **可靠性问题**：`identify_unreliable_tools(min_success_rate=0.9)`
   - 找出成功率 < 90% 的工具
   - 需要至少 5 次执行样本（避免偶然性）
   - 示例输出：`api_call: 75% success rate (20 executions)`

**生成报告**：
```python
analyzer = PerformanceAnalyzer(collector)
report = analyzer.generate_report()

# 输出
Performance Report
==========================================
Total executions: 100
Total duration: 15000ms
Overall success rate: 95%

Slowest Tools:
------------------------------------------
  bash: 250.0ms avg
  grep: 180.0ms avg

Bottlenecks (>1000ms):
------------------------------------------
  heavy_processing: avg 1500ms (max 2500ms)
```

---

## 架构设计问答

### Q20: Carbide 的三层架构是怎么设计的？各层的职责是什么？

**A:** 清晰的职责分离：

```
┌─────────────────────────────────────┐
│   M3 Execution Layer (执行层)        │
│   - StreamingToolExecutor           │
│   - 屏障式并行执行                   │
│   - 上下文压缩 + 重试 + 监控         │
└─────────────────────────────────────┘
                ↓
┌─────────────────────────────────────┐
│   M2 Governance Layer (治理层)       │
│   - PermissionEngine (权限控制)      │
│   - HookRegistry (拦截机制)          │
│   - AuditLogger (审计日志)           │
│   - SessionStore (会话持久化)        │
└─────────────────────────────────────┘
                ↓
┌─────────────────────────────────────┐
│   M1 Tool Layer (工具层)             │
│   - ToolRegistry (工具注册)          │
│   - Read/Write/Edit/Bash/Glob/Grep  │
│   - 类型安全 + 契约检查              │
└─────────────────────────────────────┘
```

**设计原则**：
1. **单向依赖**：上层依赖下层，下层不知道上层（依赖倒置）
2. **接口隔离**：每层暴露最小必要接口
3. **可替换性**：每层都可以独立替换实现

### Q21: 为什么不用现成的框架（如 LangChain）？

**A:** 选择自研的原因：

1. **性能优化**：LangChain 针对通用场景，我们需要针对工具执行优化（并行、压缩）
2. **治理需求**：企业级权限控制、审计日志是 LangChain 不提供的
3. **学习目的**：深入理解 Agent 框架的设计原理
4. **轻量级**：LangChain 依赖重（100+ 依赖），我们只需要核心功能

**借鉴但不照搬**：
- 工具抽象：参考 LangChain 的 Tool 接口
- 执行器：参考 Executor 模式
- 设计重点：工具契约、屏障式并发和治理链路统一

### Q22: 如何保证多个模块之间的接口一致性？

**A:** 使用**协议（Protocol）和抽象基类（ABC）**：

示例：
```python
# 定义协议
class BeforeHook(Protocol):
    def __call__(self, tool: Tool, ctx: HookContext) -> ToolResult | None: ...

class AfterHook(Protocol):
    def __call__(self, tool: Tool, ctx: HookContext, result: ToolResult) -> ToolResult: ...

# 实现检查
def my_hook(tool: Tool, ctx: HookContext) -> ToolResult | None:
    # mypy 会检查签名是否匹配 BeforeHook
    return None
```

**好处**：
- 编译时类型检查（mypy）
- IDE 自动补全
- 文档自动生成

---

## 性能优化问答

### Q23: 并发执行能带来多少性能提升？

**A:** 当前没有把某个固定倍数写成产品承诺，收益取决于工具耗时和调用序列。多个独立只读 I/O 可以重叠执行，理想情况下总耗时接近最慢的一项；副作用工具会建立屏障，因此不会为了追求吞吐而牺牲文件语义。

面试时应说明测量方法：固定工作区和工具调用序列，分别运行顺序基线与执行器，采集 P50/P95、最大并发数和失败率。混合场景中，写入或 Bash 会把调用切成多个阶段，实际提升通常低于完全独立读取场景。

### Q24: 上下文压缩对 API 成本有什么影响？

**A:** 显著降低成本：

**假设**：
- Claude API 价格：$3 / 1M input tokens
- 平均对话：100 轮，每轮 500 tokens

**不压缩**：
```
100 轮 × 500 tokens = 50,000 tokens
成本：50,000 × $3 / 1,000,000 = $0.15 / 会话
```

**压缩后**：
```
压缩至 3,700 tokens
成本：3,700 × $3 / 1,000,000 = $0.011 / 会话
节省：93%
```

**规模效应**：
- 1,000 个会话/天：节省 $139/天 = $50,000/年
- 长对话（200+ 轮）：节省更显著

### Q25: 如何避免并发执行时的文件冲突？

**A:** 多层保护：

1. **屏障式调度**：Write/Edit/Bash 等副作用工具不会被后续调用越过
2. **concurrency_safe 标志**：工具注册时强制检查并发契约
3. **结果顺序稳定**：按派发顺序生成 `tool_result`，避免历史错位
4. **治理链路统一**：权限、BashGuard、Hooks、审计和指标都在执行器内完成

示例：
```python
# 副作用屏障保证两个写入不会并发执行
calls = [
    Write("test.py", "v1"),  # 屏障 1
    Write("test.py", "v2"),  # 等待屏障 1
]

# 顺序执行，避免冲突
```

**未来优化**：文件级锁（fcntl/flock）。

---

## 测试策略问答

### Q26: 测试是如何组织的？覆盖率如何？

**A:** 测试分布：

| 模块 | 测试数量 | 覆盖内容 |
|-----|---------|---------|
| 工具层 | — | 工具契约、文件操作、搜索、Bash 会话 |
| 治理层 | — | 权限、Hooks、BashGuard、审计、会话 |
| 执行与上下文 | — | 执行器、并发屏障、压缩、重试、监控 |
| 记忆、技能与集成 | — | 抽取、召回、生命周期和端到端链路 |
| **总计** | **当前收集 482 个** | **核心链路持续回归** |

**测试策略**：
- **单元测试**：每个类/函数独立测试
- **集成测试**：M1+M2+M3 联合测试
- **边界测试**：空输入、大文件、并发冲突

**覆盖率目标**：≥ 80%（核心模块达到 90%+）

### Q27: 异步代码如何测试？有什么坑？

**A:** 使用 `pytest-asyncio`：

```python
import pytest

@pytest.mark.asyncio
async def test_async_function():
    result = await my_async_function()
    assert result == expected
```

**常见坑**：
1. **忘记 await**：`result = async_func()` 得到 coroutine，不是结果
2. **事件循环冲突**：每个测试需要独立的事件循环
3. **超时**：异步测试可能死锁，需要设置超时

**解决方案**：
```python
# pytest.ini 配置
[pytest]
asyncio_mode = auto  # 自动检测 async 测试

# 超时保护
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_with_timeout():
    ...
```

### Q28: 如何测试并发安全性？

**A:** 并发压力测试：

```python
@pytest.mark.asyncio
async def test_concurrent_writes():
    # 启动 100 个并发写入
    tasks = [
        write_file(f"test_{i}.txt", f"content_{i}")
        for i in range(100)
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 验证：
    # 1. 没有异常
    assert all(not isinstance(r, Exception) for r in results)
    
    # 2. 所有文件都存在
    for i in range(100):
        assert Path(f"test_{i}.txt").exists()
    
    # 3. 内容正确
    for i in range(100):
        assert Path(f"test_{i}.txt").read_text() == f"content_{i}"
```

**测试并发冲突检测**：
```python
async def test_concurrent_same_file_detected():
    # 两个不安全工具应按顺序执行
    calls = [
        StreamToolUse(id="1", name="write", arguments={"file_path": "same.txt"}),
        StreamToolUse(id="2", name="write", arguments={"file_path": "same.txt"}),
    ]

    executor.add_tool(calls[0])
    executor.add_tool(calls[1])
    results = await executor.gather_remaining()
    assert [item.call.id for item in results] == ["1", "2"]
```

---

## 总结

### 关键技术亮点

1. **类型安全 + 契约优先**（M1）- Pydantic 验证 + 双重契约检查
2. **细粒度权限控制**（M2）- 4 种模式 + 5 个 Hook 事件点
3. **安全并发优化**（M3）- 基于 `concurrency_safe` 的屏障式调度和稳定结果顺序
4. **自适应压缩**（M3）- 93% 压缩率，显著降低 API 成本
5. **完整可观测性**（M2+M3）- 审计日志 + 性能监控

### 项目数据

- **代码量**：~10,000 行（实现 + 测试）
- **测试规模**：当前收集 482 个测试；不把测试数量等同于覆盖率
- **性能**：并发和压缩收益以固定基准实测，不预先承诺倍数
- **架构**：三层分离，单向依赖

### 未来规划

- **M4 应用层**：Claude API 集成、对话管理、记忆系统
- **性能优化**：文件级锁、流式工具支持
- **BashGuard 升级**：可选 tree-sitter AST 分析
