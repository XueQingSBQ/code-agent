# 压缩和 Token 追踪修复 - 进度报告

**日期**: 2026-09-16  
**任务**: 修复压缩管理器和 token 追踪

## 修复进度

### ✅ 已完成

1. **启用压缩管理器** (src/carbide/sdk.py)
   - 移除了注释掉的代码
   - 正确初始化 `CompressionManager`
   - 创建 spill 目录
   - 配置 token budget (200K tokens, 80% threshold)
   - ✅ 测试通过：压缩管理器正确初始化

2. **更新 QueryLoop token 追踪** (src/carbide/engine/loop.py)
   - 在流式响应中捕获 usage 信息
   - 累加 input_tokens 和 output_tokens
   - 更新 QueryState 统计

3. **更新 SDK get_stats()** (src/carbide/sdk.py)
   - 添加 `total_tokens` 字段
   - 添加 `compression_enabled` 标志
   - 返回完整统计信息

4. **修复 Anthropic Provider** (src/carbide/providers/anthropic.py)
   - 在 `message_delta` 事件中返回 usage 信息
   - 解析 `event.usage` 并创建 `TokenUsage` 对象

### ❌ 未完成 / 问题

**Token 追踪仍然返回 0**

测试结果显示：
```
Input tokens: 0
Output tokens: 0
Total tokens: 0
```

**可能原因**:

1. **API 端点问题**
   - 使用的自定义 API (https://synai996.space)
   - 可能不返回 usage 信息（或格式不同）
   - 之前已经遇到 502 错误

2. **事件流结构不同**
   - 自定义端点可能使用不同的事件流格式
   - `message_delta` 事件可能不包含 usage
   - 或者 usage 信息在不同的事件中

3. **累加逻辑问题**
   - 每次收到 usage 信息时累加，可能重复计数
   - 应该只在最后一次 message_delta 时记录

## 验证测试

**压缩管理器测试**: ✅ 2/2 通过
- [OK] Compression enabled
- [OK] Compression disabled

**Token 追踪测试**: ❌ 0/1 通过
- [FAIL] Token tracking - 返回 0

## 下一步选项

### 选项 A: 使用官方 Anthropic API 测试

切换到官方 API 端点来验证 token 追踪：
```python
base_url = "https://api.anthropic.com"
```

**优点**: 官方 API 肯定返回正确的 usage 信息  
**缺点**: 需要使用官方 API key（可能没有配额）

### 选项 B: 调试自定义 API 的 usage 格式

添加调试输出来查看实际的事件结构：
```python
print(f"Event type: {event.type}")
print(f"Has usage: {hasattr(event, 'usage')}")
if hasattr(event, 'usage'):
    print(f"Usage: {event.usage}")
```

**优点**: 理解自定义 API 的实际格式  
**缺点**: 需要额外的调试时间

### 选项 C: 使用估算而非实际 usage

使用简单的启发式估算（4 字符 = 1 token）：
```python
estimated_tokens = len(content) // 4
```

**优点**: 立即可用，不依赖 API  
**缺点**: 不准确，无法用于 A/B 测试

### 选项 D: 先完成 Chaos 测试

暂时跳过 A/B 测试（需要准确 token 计数），先完成 Chaos 测试（容错性验证）。

**优点**: 可以获得一个关键指标  
**缺点**: 仍然缺少 token 减少证据

## 当前状态总结

### 代码层面: 80% 完成

✅ 压缩管理器已启用  
✅ Token 追踪代码已添加  
✅ 统计接口已更新  
⚠️ Token 追踪在自定义 API 上不工作  

### 测试验证: 40% 完成

✅ 压缩管理器初始化正确  
❌ Token 追踪返回 0  
❌ A/B 测试无法运行（需要 token 数据）  
❌ Chaos 测试未运行  

## 建议

**立即行动**: 

1. **调试 token 追踪**
   - 添加详细日志，查看 event.usage 的实际结构
   - 测试官方 API 端点验证代码是否正确
   - 确定是代码问题还是 API 端点问题

2. **如果是 API 问题**
   - 使用官方 API（如果有配额）
   - 或者使用估算方法进行 A/B 测试
   - 或者跳过 token 减少证明，专注于其他指标

3. **并行推进 Chaos 测试**
   - 容错性测试不依赖 token 计数
   - 可以独立完成
   - 获得一个关键的量化指标

## 时间估算

- **修复 token 追踪**: 2-4 小时（调试 + 测试）
- **运行 A/B 测试**: 3-5 小时（修复后）
- **运行 Chaos 测试**: 3-5 小时（可以并行）

**总计**: 8-14 小时完成所有验证

---

**报告时间**: 2026-09-16  
**下一步决策**: 继续调试 token 追踪 vs 跳过转做 Chaos 测试？
