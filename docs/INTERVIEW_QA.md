# Carbide Code Agent - 面试问答文档

> 本文档针对 M0 里程碑（生产级 Code Agent 骨架），涵盖技术选型、架构设计、实现细节和踩过的坑。

---

## 一、项目概述类问题

### 1.1 介绍一下这个项目

**回答要点**：
- **定位**：生产级别的 AI Code Agent，不是 demo。目标是可靠性和上下文效率，而非功能堆砌
- **交付形态**：CLI/TUI + 可编程 SDK，支持脚本化和人工交互两种场景
- **核心能力**（M0 已实现）：
  - 流式工具执行（模型还在生成时工具已开始运行）
  - 故障恢复（截断/窗口耗尽/API 失败）
  - 工作区隔离（路径逃逸防护）
  - 架构契约强制（import-linter）
- **技术栈**：Python 3.12+，Anthropic API，Rich 终端，pytest + Hypothesis

**拓展点**：
- 为什么不用 LangChain/LlamaIndex？→ 它们是 RAG 框架，code agent 的核心是**可靠的循环和容错**，而非向量检索
- 为什么不用 ReAct？→ 用的是 tool-use loop：**只通过 `tool_use` 块的存在决定继续/停止**，没有意图分类

---

### 1.2 为什么叫 Carbide？

**回答要点**：
- **碳化物（Carbide）**= 极硬、耐磨、抗高温，寓意系统的可靠性
- 避免了 AI/Agent/LLM 等烂大街的命名
- 短、好记、无歧义

---

### 1.3 这个项目的差异化是什么？

**回答要点**：
1. **可靠性优先**：
   - 三层截断恢复（escalate → nudge × 2 → give up）
   - 孤儿 tool_use 修复（防止下一轮请求被拒）
   - 重试保护（已消费的流不会重放）

2. **上下文效率**（M0 基础，M3 完整实现）：
   - 五层压缩金字塔（spill → snip → micro-compact → Context Collapse → Auto-Compact）
   - LayerBus（token 释放总线）避免重复压缩
   - Prompt cache 保护（静态/动态系统提示分离）

3. **工程品质**：
   - 85% 测试覆盖率，152 个测试
   - 架构契约作为 CI 信号（import-linter）
   - 属性测试（Hypothesis）验证不变量

**避免的陷阱**：
- ❌ 不要说"我实现了 xxx 功能"（功能不是差异化）
- ✅ 说"我解决了 xxx 在生产环境中的 yyy 问题"

---

## 二、架构设计类问题

### 2.1 整体架构是怎样的？

**回答要点**（画图或口述分层）：

```
┌─────────────────────────────────────────┐
│  CLI (app.py + render.py)               │  ← 用户界面层
├─────────────────────────────────────────┤
│  SDK (sdk.py)                           │  ← 可编程接口
├─────────────────────────────────────────┤
│  Engine (loop + executor + state)       │  ← 核心查询循环
├─────────────────────────────────────────┤
│  Provider (anthropic.py)                │  ← API 适配层
├─────────────────────────────────────────┤
│  Tools (fs + search + shell)            │  ← 工具执行层
└─────────────────────────────────────────┘
```

**关键设计**：
- **严格分层**：工具层不能回调引擎（import-linter 强制）
- **Provider 协议**：`base.py` 定义接口，`anthropic.py` 是唯一实现（未来可扩展 OpenAI/Gemini）
- **事件驱动**：`events.py` 是无依赖的词汇表，所有层都可以发射事件

**追问应对**：
- Q: "为什么不用消息队列？" → A: 单进程已够用，过早引入分布式会增加复杂度
- Q: "如果要支持多 provider 怎么办？" → A: 已经抽象了 `Provider` 协议，只需实现 `stream()` 方法

---

### 2.2 查询循环（Query Loop）是怎么工作的？

**回答要点**（背下来这五步）：

```python
while True:
    # 1. 准备上下文（修复孤儿 tool_use，压缩历史）
    messages = repair_messages(state.messages)
    messages = context_manager.prepare(messages)
    
    # 2. 流式调用模型，边流式边分发工具
    async for chunk in provider.stream(...):
        if isinstance(chunk, StreamToolUse):
            executor.add_tool(chunk)  # 立即分发！
    
    # 3. 收集工具结果
    results = await executor.gather_remaining()
    
    # 4. 处理特殊停止原因（窗口耗尽 / 截断）
    if stop_reason == "model_context_window_exceeded":
        compact_and_retry()
    if stop_reason == "max_tokens" and no tools ran:
        recover_truncation()
    
    # 5. 决定继续或停止
    if no tools ran:
        return Done(COMPLETED)
    # 否则继续循环
```

**关键细节**：
- **不是 ReAct**：没有 "Thought" / "Action" / "Observation" 的格式化，只看有没有 `tool_use` 块
- **工具在流式中分发**：JSON 块关闭的瞬间就 `asyncio.create_task()`，不等流结束
- **失败封闭的并发**：工具的 `concurrency_safe` 缺失/False/未知 → 串行屏障

**追问应对**：
- Q: "为什么不用 TaskGroup？" → A: TaskGroup 会在一个任务失败时取消兄弟任务，但每个 `tool_use` 必须产生 `tool_result`，否则下一轮请求会被拒
- Q: "如果两个工具写同一个文件怎么办？" → A: 写工具的 `concurrency_safe = False`，自动串行化

---

### 2.3 如何处理 API 调用失败？

**回答要点**（三层防护）：

1. **重试层**（`providers/anthropic.py`）：
   ```python
   for attempt in range(3):
       try:
           async for chunk in stream:
               produced = True
               yield chunk
           return
       except ProviderError as exc:
           if produced or not exc.retryable:
               raise  # 已经开始流式 → 不能重放
           await asyncio.sleep(backoff)
   ```
   - **关键**：`produced` 标志防止重放已渲染的文本

2. **恢复层**（`engine/loop.py`）：
   - `PromptTooLongError` → 强制压缩 + 重试
   - `model_context_window_exceeded` → 压缩 + 重试（最多 2 次）
   - 其他 `ProviderError` → 立即结束，返回 `PROVIDER_ERROR`

3. **修复层**（`engine/repair.py`）：
   - 孤儿 `tool_use`（无配对的 `tool_result`）→ 合成错误结果
   - 防止下一轮请求被 API 直接拒绝

**追问应对**：
- Q: "为什么不用指数退避？" → A: 用了，`delay = BASE * (2 ** attempt) + random(0, 0.25)`
- Q: "如果压缩也失败了呢？" → A: 返回 `COMPACTION_FAILED`，诚实地告诉用户（M0 故意不实现压缩，让这条路径可测试）

---

### 2.4 截断恢复（Truncation Recovery）的设计逻辑？

**回答要点**（背下来这个梯阶）：

```
第 1 次截断 → escalate    (8k → 16k max_tokens，丢弃半成品，重试)
第 2 次截断 → nudge       (保留半成品，追加 "继续" 提示)
第 3 次截断 → nudge       (再次保留 + 提示)
第 4 次截断 → give up     (返回 OUTPUT_TRUNCATION_UNRECOVERED)
```

**关键细节**：
- **escalate 时必须回滚**：否则尾部的 assistant 消息会变成 prefill，模型会续写而非重新回答
  ```python
  if recovered == "escalate":
      state = state.with_messages(request_messages)  # 回滚！
  ```
- **计数器不能重置**：`next_turn(reset_recovery=not truncated)`，否则永远卡在第一阶

**踩过的坑**：
- Bug A：`next_turn()` 无条件重置计数器 → 梯阶永远不会到 give up
- Bug B：escalate 时保留了截断的 assistant 消息 → 变成 prefill

**测试覆盖**：
- `test_the_ladder_gives_up_instead_of_looping` - 验证恰好 4 轮后放弃
- `test_escalated_retry_does_not_prefill_the_truncated_text` - 验证回滚

---

### 2.5 并发工具执行的安全性怎么保证？

**回答要点**：

1. **声明式契约**：
   ```python
   class WriteTool(Tool):
       concurrency_safe: ClassVar[bool] = False  # 必须声明！
   ```
   - 缺失 / False / 未知 → 作为串行屏障

2. **四道防御**：
   - `__init_subclass__`：类创建时检查声明
   - `ToolRegistry.register`：注册时再检查
   - mypy strict：静态类型检查
   - `test_tool_contract.py`：运行时测试

3. **执行逻辑**（`StreamingToolExecutor`）：
   ```python
   if tool.concurrency_safe:
       tasks.append(asyncio.create_task(run(tool)))
   else:
       await barrier()  # 等待所有已分发的任务
       result = await run(tool)
   ```

**追问应对**：
- Q: "为什么不用锁？" → A: 锁是细粒度的，但我们需要的是**粗粒度的顺序保证**（比如 shell 命令必须串行）
- Q: "如果忘记声明怎么办？" → A: `__init_subclass__` 会在类定义时抛 `TypeError`

---

## 三、实现细节类问题

### 3.1 为什么要从 SDK 的 `messages.stream()` 换成原始 `messages.create(stream=True)`？

**回答要点**：

**问题**：
- `messages.stream()` 返回的是累积辅助器流，事件类型是 SDK 的包装类
- 无法针对 API 实际发送的原始事件做类型检查
- mypy 报了 130 个 `union-attr` 错误（比如 `.index` / `.thinking` / `.stop_reason` 在某些事件上不存在）

**解决**：
- 用 `messages.create(..., stream=True)` 拿到原始事件联合
- 自己写 `_Block` 累加器逐索引组装 `assistant_content`
- 代码可类型检查，且更贴近 API 协议

**代价**：
- 多写了 ~50 行累加逻辑
- 但换来了类型安全 + 对协议的完全控制

---

### 3.2 Grep 工具为什么有两个后端？

**回答要点**：

**设计考量**：
- ripgrep 快（几十倍于纯 Python），但不是所有环境都装了
- 一个回答 "先装 ripgrep" 的工具是死路，因为模型无法装软件

**实现**：
```python
class GrepTool:
    prefer_ripgrep: ClassVar[bool] = True
    
    def _run(self, ...):
        if self.prefer_ripgrep and shutil.which("rg"):
            result = self._ripgrep(...)
            if result is not None:
                return result
        # 回退到纯 Python
        return self._python_search(...)
```

**测试保证**：
- 参数化测试，两个后端必须输出**字节相同**的结果
- CI 在 ripgrep 测试跳过时会失败（防止核心功能未验证）

**追问应对**：
- Q: "为什么不打包一个 ripgrep wheel？" → A: PyPI 上的几个都不够维护，不敢依赖
- Q: "为什么要保证字节相同？" → A: 输出格式是工具契约的一部分，模型会解析它

---

### 3.3 工作区包含检查（Workspace Containment）是怎么实现的？

**回答要点**：

**安全漏洞**：
- 早期版本的 `GrepTool` / `GlobTool` 从未验证路径
- 用户输入 `../../etc/passwd` 会逃逸工作区

**修复**（`tools/paths.py`）：
```python
def resolve_in_workspace(workspace: Path, raw: str) -> Path | None:
    root = workspace.resolve()
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() 
                else root / candidate).resolve()
    try:
        resolved.relative_to(root)  # 检查是否在 root 下
    except ValueError:
        return None  # 逃逸 → 拒绝
    return resolved
```

**测试覆盖**（`test_tools.py::TestWorkspaceContainment`）：
- `../outside.txt` → 拒绝
- 绝对路径在工作区外 → 拒绝
- 符号链接指向外部 → 拒绝（`resolve()` 会展开）

**设计原则**：
- 共享模块而非各工具自己实现 → "最弱的那个才是重要的"

---

### 3.4 如何保证架构不会腐化？

**回答要点**：

**工具**：`import-linter` + CI

**契约定义**（`pyproject.toml`）：
```toml
[[tool.importlinter.contracts]]
name = "Layered architecture"
type = "layers"
layers = ["cli", "sdk", "engine", "providers", "tools"]

[[tool.importlinter.contracts]]
name = "Tools do not reach back into the engine"
type = "forbidden"
source_modules = ["carbide.tools"]
forbidden_modules = ["carbide.engine"]
```

**CI 强制**（`.github/workflows/ci.yml`）：
```yaml
- name: Architecture contracts
  run: uv run lint-imports
```

**为什么重要**：
- 分层是设计意图，但代码会漂移
- 把架构变成独立的 CI 信号，和 lint/test 同级

**追问应对**：
- Q: "为什么不用 Python 的模块可见性？" → A: Python 没有 `private` 关键字，`_` 前缀只是约定
- Q: "契约会不会太严格？" → A: 只禁止反向依赖（下层调上层），同层互调是允许的

---

## 四、测试与质量类问题

### 4.1 测试覆盖率 85%，剩下 15% 是什么？

**回答要点**：

**未覆盖的部分**：
1. **错误分支**：
   - `except Exception as exc` 的罕见异常路径
   - 文件不存在 / 权限拒绝等边缘 case

2. **CLI 入口**：
   - `__main__.py` / `cli/app.py` 的参数解析
   - 这些在集成测试中不方便覆盖

3. **部分 `context/manager.py`**：
   - `_summarise()` 故意抛 `NotImplementedError`（M3 才实现）

**为什么不追求 100%**：
- 性价比递减：从 85% → 95% 需要写大量 mock
- 关键路径已覆盖：循环、工具、恢复逻辑都 >90%

**追问应对**：
- Q: "为什么不 mock 掉文件系统？" → A: 用了 `tmp_path` fixture，真实文件系统更能发现问题
- Q: "为什么不测 CLI？" → A: 有 `test_integration.py` 用 `StringIO` 测了 Renderer

---

### 4.2 什么是属性测试（Property-Based Testing）？

**回答要点**：

**定义**：
- 不写具体输入/输出，而是写**不变量**（property）
- 测试框架（Hypothesis）生成随机输入，验证不变量

**例子**（`test_invariants.py`）：
```python
@given(st.lists(st.text(), min_size=1, max_size=20))
def test_every_tool_use_gets_a_result(history):
    # 不变量：每个 tool_use 必须有配对的 tool_result
    tool_use_ids = {block["id"] for msg in history 
                    for block in msg if block["type"] == "tool_use"}
    tool_result_ids = {block["tool_use_id"] for msg in history 
                       for block in msg if block["type"] == "tool_result"}
    assert tool_use_ids == tool_result_ids
```

**为什么用**：
- "每个 tool_use 都被应答" 是关于**所有**历史的声明，手写 case 覆盖不全
- Hypothesis 会生成边缘 case（空列表 / 单元素 / 重复 ID）

**追问应对**：
- Q: "属性测试不是很慢吗？" → A: Hypothesis 默认 100 个例子，~1 秒
- Q: "失败了怎么调试？" → A: Hypothesis 会缩减（shrink）到最小失败用例

---

### 4.3 如何保证后端等价性（Backend Equivalence）？

**回答要点**：

**测试策略**（`test_tools.py::TestBackendEquivalence`）：
```python
@pytest.mark.parametrize("use_ripgrep", [False, True])
def test_same_output(workspace, use_ripgrep):
    tool = _grep(use_ripgrep=use_ripgrep)
    result = tool.run(pattern="def", path=workspace)
    assert sorted(result.content.splitlines()) == expected
```

**CI 保护**：
```yaml
- name: Fail if ripgrep tests skipped
  run: |
    if grep -q "ripgrep not installed" skips.txt; then
      echo "::error::Backend equivalence check did not run"
      exit 1
    fi
```

**为什么重要**：
- 两个后端输出不一致 → 模型会混淆
- 早期本地没装 ripgrep，14 个测试静默跳过了 3 周

---

### 4.4 发现了哪些 Bug？

**回答要点**（选 3 个讲）：

**Bug A - 截断梯阶永不放弃**：
- **现象**：输出截断后无限重试
- **根因**：`next_turn()` 无条件重置 `output_recovery_count`，但计数器在 `next_turn()` 之后读取
- **修复**：添加 `reset_recovery` 参数
- **测试**：`test_the_ladder_gives_up_instead_of_looping` - 验证恰好 4 轮

**Bug B - 重试变成 prefill**：
- **现象**：escalate 后模型续写半句话，而非重新回答
- **根因**：保留了截断的 assistant 消息，API 把它当作 prefill
- **修复**：回滚到 `request_messages`
- **测试**：`test_escalated_retry_does_not_prefill_the_truncated_text`

**Bug C - 重试重放流**：
- **现象**：API 失败重试时，文本重复渲染
- **根因**：旧注释说 "不可能发生"，但没强制
- **修复**：`produced` 标志，一旦流开始就不重试
- **测试**：无直接测试（难以稳定复现），但代码审查时发现

**共性**：都是并发 + 状态管理的边缘 case，单元测试很难发现

---

## 五、技术选型类问题

### 5.1 为什么选 Python 而不是 Rust/Go？

**回答要点**：

**优势**：
1. **生态成熟**：Anthropic SDK、Rich、pytest 等直接可用
2. **快速迭代**：10-14 周业余时间，Rust 会卡在类型体操上
3. **AI 社区主力**：招聘 + 开源贡献者都偏 Python

**劣势**：
1. **性能**：但 code agent 的瓶颈是 API 延迟（秒级），不是语言（毫秒级）
2. **类型安全**：用 mypy strict mode 补偿

**未来**：
- 性能热点（比如 grep）可以用 Rust 写扩展
- 但核心循环不需要重写

---

### 5.2 为什么不用 LangChain？

**回答要点**：

**LangChain 的定位**：
- RAG 框架：向量数据库 + 检索 + Prompt 模板
- 适合问答系统 / 知识库

**Code Agent 的核心需求**：
- **可靠的循环**：失败恢复、截断处理、孤儿修复
- **精确的并发控制**：工具安全性声明、串行屏障
- **上下文效率**：五层压缩、token 记账

**LangChain 提供不了**：
- 它的 `AgentExecutor` 只是简单循环，没有恢复逻辑
- 工具并发是黑盒，无法控制
- 没有 context window 管理

**结论**：LangChain 解决的是"怎么调模型"，而我要解决的是"怎么让它可靠运行"

---

### 5.3 为什么用 Rich 而不是纯文本输出？

**回答要点**：

**Rich 的优势**：
1. **进度反馈**：Spinner + 实时流式输出
2. **结构化**：Panel / Table 让复杂输出可读
3. **颜色高亮**：错误红色、成功绿色、工具调用青色

**设计约束**：
- `Renderer` 是可拔插的：换个 `Console` 就能输出到文件 / JSON
- `test_integration.py` 用 `Console(file=StringIO())` 测试渲染逻辑

**为什么不用 TUI（Textual）**：
- M0 只需流式输出，不需要交互
- M2 添加权限审批时会引入 TUI

---

## 六、开放性问题

### 6.1 如果让你重新设计，会改什么？

**回答要点**：

**会保留的**：
- 严格分层 + 契约强制
- 双后端设计（ripgrep + 纯 Python）
- 属性测试

**会改进的**：
1. **事件系统**：当前是 `AsyncIterator[Event]`，考虑改成 pub-sub（多消费者）
2. **配置管理**：当前是 Pydantic，考虑支持热重载
3. **日志**：当前用 structlog，但没接入 observability（M4 会加）

**不会改的**：
- 技术栈（Python + Anthropic）
- 核心架构（query loop）

---

### 6.2 你觉得最难的部分是什么？

**回答要点**：

**技术难度 Top 3**：
1. **截断恢复的状态机**：
   - 三个计数器（`output_recovery_count` / `window_recovery_count` / `attempted_reactive_compact`）
   - 每个有自己的重置时机
   - Bug A 就是这里的逻辑错误

2. **并发工具的失败处理**：
   - 不能用 `TaskGroup`（会取消兄弟任务）
   - 每个 `tool_use` 必须产生 `tool_result`，即使失败
   - `abort()` 要正确处理部分完成的任务

3. **后端等价性**：
   - ripgrep 和 Python 的行为微妙不同（比如 `.gitignore` 处理）
   - 输出格式必须完全一致（包括路径分隔符）

**非技术难度**：
- 测试覆盖 vs 时间预算的权衡
- 何时止步（不过度设计）

---

### 6.3 下一步计划（M1-M5）？

**回答要点**（简要提及，不要背全部）：

**M1（第 3-4 周）** - 工具层完善：
- 持久化 Bash 会话（多轮命令共享状态）
- 更多文件操作（move / delete / list）
- 文件监视（hot reload）

**M2（第 5-6 周）** - 权限与审计：
- 4 种权限模式（always-allow / prompt / deny / audit）
- Hooks（pre_tool / post_tool / on_error）
- bashguard（tree-sitter AST 分析，拒绝 `rm -rf /`）
- SQLite 会话恢复（`--resume`）

**M3（第 7-9 周）** - 上下文管理（**简历重点**）：
- 五层压缩金字塔
- LayerBus（token 释放总线）
- 混沌测试（注入窗口压力，验证恢复逻辑）

**M4（第 10-11 周）** - 内存与技能：
- 持久化记忆（用户偏好 / 项目知识）
- 技能系统（可复用的工具组合）

**M5（第 12-13 周）** - 打磨与发布：
- SWE-bench Lite 评估
- PyPI 发布
- 文档 + Demo 视频

---

## 七、行为问题（附加）

### 7.1 遇到最大的挑战是什么？

**回答模板**（STAR 法则）：

**Situation**：
- M0 开发中期，mypy 报了 130 个类型错误，都集中在 `providers/anthropic.py`

**Task**：
- 必须在不破坏功能的前提下修复类型错误

**Action**：
1. 定位根因：SDK 的累积辅助器流事件类型不匹配 API 原始协议
2. 重写为原始流 + 自己的 `_Block` 累加器
3. 回归测试：38 个 provider 测试全部通过

**Result**：
- 类型错误清零
- 代码更贴近 API 协议，未来更好维护

---

### 7.2 如何保证代码质量？

**回答要点**：

**自动化**：
- ruff（格式 + lint）
- mypy strict
- import-linter
- pytest（覆盖率 85%）
- 全部接入 CI

**人工**：
- 代码审查（即使是个人项目，也会隔天重新读）
- 文档字符串必须解释 "为什么"，不只是 "做什么"

**文化**：
- "测试是设计工具"：难测的代码通常是设计有问题
- "契约优于注释"：用类型 / 协议 / linter 强制约束

---

## 八、快速自查清单

面试前过一遍，确保能流畅回答：

- [ ] 能画出架构分层图
- [ ] 能口述查询循环的五步
- [ ] 能解释三个 Bug 的根因和修复
- [ ] 能说清楚为什么不用 LangChain/TaskGroup/ReAct
- [ ] 能讲清楚截断恢复的梯阶
- [ ] 能解释并发工具的安全性设计
- [ ] 能说出测试覆盖率和关键测试用例
- [ ] 能简述 M1-M5 的里程碑

---

## 附录：关键代码片段速查

### A1 - 查询循环骨架

```python
async def run(self, state: TurnState) -> AsyncIterator[Event]:
    while True:
        # 1. 准备上下文
        messages = repair_messages(state.messages)
        messages = self._context.prepare(messages)
        
        # 2. 流式 + 分发工具
        executor = StreamingToolExecutor(...)
        async for chunk in self._provider.stream(...):
            if isinstance(chunk, StreamToolUse):
                executor.add_tool(chunk)
        
        # 3. 收集结果
        results = await executor.gather_remaining()
        
        # 4. 特殊停止原因
        if end.stop_reason == "model_context_window_exceeded":
            # 压缩 + 重试
        if end.stop_reason == "max_tokens" and not results:
            # 截断恢复
        
        # 5. 继续或停止
        if not results:
            yield Done(COMPLETED)
            return
```

### A2 - 并发执行器

```python
def add_tool(self, call: StreamToolUse) -> None:
    safe = self._registry.is_concurrency_safe(call.name)
    waits = list(self._tasks.values()) if not safe else list(self._barrier)
    task = asyncio.create_task(self._run(call, waits))
    self._tasks[call.id] = task
    if not safe:
        self._barrier = [task]

async def gather_remaining(self) -> list[CompletedTool]:
    await asyncio.wait(self._tasks.values())
    return [self._harvest(call, self._tasks[call.id]) for call in self._order]
```

### A3 - 工作区包含检查

```python
def resolve_in_workspace(workspace: Path, raw: str) -> Path | None:
    root = workspace.resolve()
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() 
                else root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved
```

---

**最后建议**：
- 面试时不要背诵，用自己的话讲
- 准备 1-2 个具体的 "踩坑-分析-解决" 故事
- 如果被问到未实现的部分（M3-M5），诚实说 "这是后续里程碑，当前的设计已经预留了扩展点"

祝面试顺利！
