# 工具注册架构说明

## 设计意图

工具注册是**有意设计**的架构分层：

### 架构分层

```
CLI Layer (M5)
  ├─ create_default_registry()  ← 在这里注册工具
  ├─ registry.api_schemas()      ← 获取API schemas
  └─ Agent(tool_registry=registry) ← 传递给SDK

SDK Layer (M4)
  ├─ Agent.__init__(tool_registry=None) ← 接受但不创建
  └─ QueryLoop(tool_registry=...)       ← 传递给执行层

Execution Layer (M1)
  └─ StreamingToolExecutor(registry)    ← 实际执行工具
```

### 为什么这样设计？

1. **职责分离**
   - SDK层：通用agent逻辑，不依赖具体工具
   - CLI层：决定哪些工具可用

2. **灵活性**
   - 可以创建无工具的Agent（纯对话）
   - 可以创建自定义工具集的Agent
   - 测试时可以mock工具注册表

3. **可扩展性**
   - 不同应用场景使用不同工具集
   - Web UI可能需要不同的工具
   - API服务器可能限制某些工具

## CLI层的实现

```python
# src/carbide/cli/app.py:116-120

# 1. CLI创建工具注册表
registry = create_default_registry()

# 2. 获取API schemas传递给provider
tools = registry.api_schemas()

# 3. 创建Agent时传入registry
async with Agent(config=config, tool_registry=registry) as agent:
    await run_once(agent, renderer, prompt, tools=tools)
```

## 默认工具集

```python
# src/carbide/tools/defaults.py

def create_default_registry() -> ToolRegistry:
    """Create registry with all default tools."""
    registry = ToolRegistry()
    
    # 注册基础工具
    registry.register("read_file", read_file_tool)
    registry.register("write_file", write_file_tool)
    registry.register("list_directory", list_directory_tool)
    registry.register("bash", bash_tool)
    registry.register("search", search_tool)
    # ... 更多工具
    
    return registry
```

## 测试场景的问题

### 我们的测试

```python
# tests/test_2_governance.py

agent = Agent(
    project_dir=project_dir,
    enable_governance=True,
    # 问题：没有传递 tool_registry ❌
)
```

### 结果
- `tool_registry=None`
- Agent可以初始化，但**无法执行任何工具**
- 模型说："I can't access files directly"

## 正确的测试方式

### 选项1：使用默认工具集

```python
from carbide.tools.defaults import create_default_registry

registry = create_default_registry()
tools = registry.api_schemas()

agent = Agent(
    project_dir=project_dir,
    enable_governance=True,
    tool_registry=registry,  ✅
)

# 查询时传递tools schemas
async for event in agent.query("Read test.txt", tools=tools):
    ...
```

### 选项2：创建测试专用工具集

```python
from carbide.tools.registry import ToolRegistry

registry = ToolRegistry()
registry.register("read_file", mock_read_tool)
registry.register("write_file", mock_write_tool)

agent = Agent(
    tool_registry=registry,
)
```

## 这不是Bug，是设计特性

### ✅ 正确的行为

1. **SDK不应该假设工具集**
   - 不同应用需要不同工具
   - 保持SDK层的通用性

2. **CLI负责工具决策**
   - CLI知道用户环境
   - CLI决定哪些工具安全可用

3. **测试可以灵活控制**
   - Mock工具进行单元测试
   - 注入真实工具进行集成测试

### 当前状态

| 场景 | 工具来源 | 状态 |
|------|----------|------|
| CLI运行 | `create_default_registry()` | ✅ 正常工作 |
| SDK直接使用 | 用户提供 `tool_registry` | ✅ 按设计工作 |
| 我们的测试 | 未提供 | ❌ 测试写法错误 |

## 测试修复方案

### 立即修复

更新 `test_2_governance.py`:

```python
from carbide.tools.defaults import create_default_registry

# 创建带工具的agent
registry = create_default_registry()
tools = registry.api_schemas()

agent = Agent(
    project_dir=project_dir,
    enable_governance=True,
    tool_registry=registry,
)

# 查询时传递tools
async for event in agent.query(
    f"Read the file at {test_file}",
    tools=tools,
):
    ...
```

### 验证目标

修复后应该看到：
- ✅ 工具被调用 (tool_use events)
- ✅ Governance检查工具权限
- ✅ AuditLogger记录工具执行
- ✅ 审计日志文件被创建

## 结论

**这不是集成问题，是测试写法问题**

- ✅ SDK设计正确：不假设工具集
- ✅ CLI实现正确：提供默认工具集
- ✅ 架构清晰：职责分离明确
- ❌ 测试写法错误：忘记提供tool_registry

**修复后的验证率预期**: 从62.5%提升到85%+

因为：
- Test 1 (Memory): 已经100% ✅
- Test 2 (Governance): 将从50%提升到100% ✅
- Test 3 (Compression): 仍然超时（需要其他优化）⏳
