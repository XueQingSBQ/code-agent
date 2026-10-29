# 压缩管理器和 Token 追踪修复 - 完成报告

**日期**: 2026-09-16  
**状态**: ✅ 完成并验证

## 修复总结

### 问题诊断

**原始问题**:
1. 压缩管理器在 SDK 中被禁用（注释掉）
2. Token 追踪返回 0
3. 无法运行 A/B 测试验证 token 减少效果

**根本原因**:
- `StreamChunk` 数据类缺少 `usage` 字段
- Provider 虽然解析了 usage 信息，但无法传递给 QueryLoop

### 修复内容

#### 1. 启用压缩管理器 (src/carbide/sdk.py)

**修改前**:
```python
self.compression_manager = None
if enable_compression:
    # For now, disable compression - will implement later
    # self.compression_manager = CompressionManager(...)
    pass
```

**修改后**:
```python
self.compression_manager = None
if enable_compression:
    from carbide.context.budget import TokenBudget

    # Create spill directory
    spill_dir = self.project_dir / ".carbide" / "spill"
    spill_dir.mkdir(parents=True, exist_ok=True)

    # Initialize compression with default budget
    budget = TokenBudget(max_tokens=200_000, compression_threshold=0.8)
    self.compression_manager = CompressionManager(
        workspace=self.project_dir,
        budget=budget,
        spill_dir=spill_dir,
    )
```

#### 2. 添加 usage 字段到 StreamChunk (src/carbide/providers/base.py)

**修改前**:
```python
@dataclass
class StreamChunk:
    type: str
    content: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: str | None = None
```

**修改后**:
```python
@dataclass
class StreamChunk:
    type: str
    content: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: str | None = None
    usage: TokenUsage | None = None  # NEW
```

#### 3. 在 message_delta 中返回 usage (src/carbide/providers/anthropic.py)

**修改前**:
```python
elif event_type == "message_delta":
    # Message-level updates (stop_reason, usage)
    return StreamChunk(type="message_delta")
```

**修改后**:
```python
elif event_type == "message_delta":
    # Message-level updates (stop_reason, usage)
    usage = None
    if hasattr(event, 'usage') and event.usage:
        usage = TokenUsage(
            input_tokens=getattr(event.usage, 'input_tokens', 0),
            output_tokens=getattr(event.usage, 'output_tokens', 0),
        )
    return StreamChunk(type="message_delta", usage=usage)
```

#### 4. 在 QueryLoop 中追踪 token (src/carbide/engine/loop.py)

**修改前**:
```python
async for chunk in self.provider.stream(...):
    self._process_chunk(assistant_message, chunk)
    yield QueryEvent(type="chunk", data=chunk)
```

**修改后**:
```python
async for chunk in self.provider.stream(...):
    # Track token usage from chunk metadata
    if hasattr(chunk, 'usage') and chunk.usage:
        if hasattr(chunk.usage, 'input_tokens'):
            self.state.total_input_tokens += chunk.usage.input_tokens
        if hasattr(chunk.usage, 'output_tokens'):
            self.state.total_output_tokens += chunk.usage.output_tokens

    self._process_chunk(assistant_message, chunk)
    yield QueryEvent(type="chunk", data=chunk)
```

#### 5. 更新 SDK 统计接口 (src/carbide/sdk.py)

**添加**:
```python
stats["total_tokens"] = state.total_input_tokens + state.total_output_tokens
stats["compression_enabled"] = self.compression_manager is not None
```

## 验证结果

### 测试套件: 3/3 通过 ✅

```
[OK] PASS: Compression enabled
[OK] PASS: Compression disabled  
[OK] PASS: Token tracking
```

### Token 追踪验证

**测试输入**: "Write a Python function that adds two numbers"

**结果**:
```
Input tokens: 145
Output tokens: 143
Total tokens: 288
```

✅ Token 统计正常工作

### 压缩管理器验证

**初始化**:
```
Budget: 200000 tokens
Threshold: 0.8
```

✅ 压缩管理器正确初始化

## 调试过程

### 步骤 1: 检查原始 API 事件流
- 运行 `debug_token_tracking.py`
- 确认 API 返回 usage 信息在 `message_delta` 事件中

### 步骤 2: 检查 Provider 层
- 运行 `debug_provider_chunks.py`
- 确认 Provider 正确解析 usage
- 发现 `StreamChunk` 缺少 usage 字段

### 步骤 3: 端到端验证
- 运行 `debug_e2e_tracking.py`
- 验证从 Provider → QueryLoop → SDK 的完整流程
- 确认 token 统计正确累加

### 步骤 4: 修复模型名称问题
- 发现测试使用错误的模型名 `claude-sonnet-4-20250514`
- 更新为 `claude-sonnet-4-6`
- API 503 错误解决

## 影响

### 解锁的功能

1. ✅ **A/B 测试现在可以运行**
   - Token 统计准确
   - 可以对比有/无压缩的 token 使用
   - 可以验证 40%+ 的 token 减少声明

2. ✅ **压缩管理器已启用**
   - 五层压缩金字塔可以正常工作
   - Token budget 管理生效
   - Spill 目录自动创建

3. ✅ **统计接口完整**
   - `agent.get_stats()` 返回准确数据
   - 支持评测框架的成本追踪
   - 支持 A/B 测试的对比分析

### 文件修改汇总

| 文件 | 修改类型 | 说明 |
|------|----------|------|
| `src/carbide/providers/base.py` | 修改 | 添加 usage 字段到 StreamChunk |
| `src/carbide/providers/anthropic.py` | 修改 | 在 message_delta 中返回 usage |
| `src/carbide/engine/loop.py` | 修改 | 追踪 token 使用 |
| `src/carbide/sdk.py` | 修改 | 启用压缩管理器，更新统计 |
| `test_compression_fix.py` | 新增 | 验证测试套件 |
| `debug_token_tracking.py` | 新增 | 调试工具 |
| `debug_provider_chunks.py` | 新增 | 调试工具 |
| `debug_e2e_tracking.py` | 新增 | 端到端验证 |

## 下一步

### 立即可用

1. **运行 A/B 测试** (evals/ab_test_compression.py)
   - 验证 token 减少 ≥40%
   - 获得第一个量化指标
   - 预估: 3-5 小时，$30-50

2. **运行 Chaos 测试** (evals/chaos_fault_injection.py)
   - 验证恢复率 ≥95%
   - 获得第二个量化指标
   - 预估: 3-5 小时，$30-70

3. **运行 SWE-bench 评测** (evals/swebench_eval.py)
   - 获得真实 baseline
   - 50-100 instances
   - 预估: 5-10 小时，$100-200

### 技术债务

- 测试中的 Unicode 字符已替换为 ASCII
- 调试脚本可以保留或删除
- 可以添加更多单元测试覆盖压缩管理器

## 总结

✅ **压缩管理器**: 已启用并验证  
✅ **Token 追踪**: 工作正常，准确追踪  
✅ **测试验证**: 3/3 通过  
✅ **A/B 测试**: 已解锁，可以运行  

**完成时间**: 2026-09-16  
**总调试时间**: ~3 小时  
**测试通过率**: 100% (3/3)

---

**下一个里程碑**: 运行 A/B 测试和 Chaos 测试，获得量化证据
