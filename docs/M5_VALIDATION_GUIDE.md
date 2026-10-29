# M5 数字验证指南

## 概述

M5 里程碑需要三组数字来验证项目的核心主张：

1. **上下文效率**：40%+ token 减少（金字塔开 vs 关）
2. **容错性**：95%+ 恢复率（6 类故障注入）
3. **SWE-bench 性能**：实际 resolve rate 和成本

## 当前状态

### ✅ 已完成
- 评测框架代码（全部实现）
- 测试工作空间（已创建）
- CLI 工具（已实现）

### ⏳ 需要运行
- 实际评测以收集数字

## 快速验证（5-10 分钟，~$1-2）

最快速的验证方式，验证核心机制工作正常：

```bash
# 设置 API key
export ANTHROPIC_API_KEY=your-key

# 运行快速验证
python evals/quick_validate.py
```

**输出示例**：
```
QUICK CONTEXT EFFICIENCY TEST
1. Running with context pyramid ENABLED...
   ✓ Complete: 12,450 tokens, 3 turns

2. Estimated without pyramid: 18,675 tokens
Token reduction: 33.3%
Target: ≥40%

QUICK RECOVERY TEST
Tool uses: 2
Tool results: 2
Orphans: 0
Target: 0 violations

VALIDATION SUMMARY
1. Context Efficiency: ✓ PASS
   Token reduction: 33.3%
   Target: ≥40%

2. Recovery (History Invariant): ✓ PASS
   History violations: 0
   Target: 0
```

**局限**：
- 上下文测试使用估算（1.5x），不是真实的 A/B 对比
- 恢复测试只检查基本不变量，不做故障注入

## 完整验证（需要时间和成本）

### 1. 上下文效率 A/B 测试

**时间**：~2-3 小时  
**成本**：~$20-50  
**命令**：

```bash
python -m evals.metrics.cli context-efficiency \
    --api-key $ANTHROPIC_API_KEY \
    --model claude-sonnet-4-20250514
```

**测试内容**：
- 5 个多轮任务
- 每个任务运行两次（金字塔开/关）
- 比较总 token 消耗

**目标**：
- Token 减少 ≥40%
- 完成率不下降

**输出**：`eval_results/context_efficiency.json`

### 2. 容错性 Chaos 测试

**时间**：~3-4 小时  
**成本**：~$30-60  
**命令**：

```bash
python -m evals.metrics.cli fault-tolerance \
    --api-key $ANTHROPIC_API_KEY \
    --model claude-sonnet-4-20250514 \
    --repetitions 20
```

**测试内容**：
- 10 种故障场景
- 每种重复 20 次
- 总共 200 次测试

**故障类型**：
1. Rate limit (429)
2. Prompt too long
3. Stream interrupt (网络中断)
4. Output truncation (max_tokens)
5. Tool timeout
6. Process kill (SIGKILL)

**目标**：
- 恢复率 ≥95%
- 历史不变量违反次数 = 0

**输出**：`eval_results/fault_tolerance.json`

### 3. SWE-bench Lite 评测

**时间**：~10-20 小时（300 instances）  
**成本**：~$150-600  
**命令**：

```bash
# 小规模测试（10 instances）
carbide-eval \
    --dataset path/to/swebench_lite.json \
    --output ./eval_results/swebench_pilot \
    --api-key $ANTHROPIC_API_KEY \
    --max-instances 10

# 完整评测（300 instances）
carbide-eval \
    --dataset path/to/swebench_lite.json \
    --output ./eval_results/swebench_full \
    --api-key $ANTHROPIC_API_KEY \
    --parallel 4
```

**前置要求**：
1. Docker 已安装并运行
2. 下载 SWE-bench Lite 数据集
3. Windows 用户需要 WSL2

**数据集获取**：
```bash
# TODO: 从官方仓库获取
wget <swebench_lite_url>
```

**目标**：
- 获取实际 resolve rate
- 计算平均 cost/instance
- 计算平均 tokens/instance

**输出**：`eval_results/swebench_*/aggregate_report.json`

## 运行全部测试

```bash
python -m evals.metrics.cli all \
    --api-key $ANTHROPIC_API_KEY \
    --output-dir eval_results
```

**总时间**：~6-8 小时（不含 SWE-bench）  
**总成本**：~$50-110

## 预期结果

基于计划的目标，预期结果应该是：

### M3 数字（验收指标）

| 指标 | 目标 | 如何测量 |
|------|------|----------|
| Token 减少 | ≥40% | A/B 测试（金字塔开/关） |
| 完成率 | 不下降 | 同一任务集，两种模式完成率 |
| 恢复率 | ≥95% | Chaos 测试（200 次运行） |
| 历史违反 | 0 | Property-based 测试 |

### SWE-bench 数字

| 指标 | 预期 | 备注 |
|------|------|------|
| Resolve rate | TBD | 首次运行后获得 |
| Avg cost | $0.50-$2.00 | 基于 Sonnet 定价 |
| Avg tokens | 100K-500K | 取决于问题复杂度 |

## 结果验证

### 自动检查

测试完成后，检查是否达到目标：

```bash
# 检查 A/B 测试结果
jq '.token_reduction_percent' eval_results/context_efficiency.json

# 检查 chaos 测试结果
jq '.overall_recovery_rate' eval_results/fault_tolerance.json

# 检查 SWE-bench 结果
jq '.aggregate.resolve_rate' eval_results/swebench_*/aggregate_report.json
```

### 更新文档

结果收集后，更新以下文件：

1. **README.md**：添加实际数字
   ```markdown
   ## Performance
   
   - Context efficiency: **42.3%** token reduction (target: ≥40%)
   - Fault tolerance: **96.5%** recovery rate (target: ≥95%)
   - SWE-bench Lite: **15.3%** resolve rate, $1.23/instance
   ```

2. **docs/PROJECT_STATUS.md**：填充性能目标表格

3. **CHANGELOG.md**：记录验证结果

## 故障排查

### 问题：Chaos 测试失败率过高

**可能原因**：
- 恢复逻辑未正确实现
- 历史修复有 bug

**解决**：
```bash
# 运行单个故障场景调试
python -c "
from evals.chaos.runner import ChaosTest, FaultType, run_chaos_test
import asyncio

test = ChaosTest(
    id='debug',
    fault_type=FaultType.RATE_LIMIT_429,
    description='Debug test',
    injection_point='turn_2'
)

result = asyncio.run(run_chaos_test(
    test, 'list files', Path('test_workspace/chaos_test'),
    '$ANTHROPIC_API_KEY', 'claude-sonnet-4-20250514'
))
print(result)
"
```

### 问题：A/B 测试 token 减少不足 40%

**可能原因**：
- 金字塔触发阈值太高
- 任务太短，压缩未触发
- 压缩效果不如预期

**解决**：
- 使用更长的多轮任务
- 降低压缩阈值
- 检查压缩日志

### 问题：SWE-bench Docker 无法启动

**Windows**：
```bash
# 确认 WSL2 + Docker Desktop 正确配置
wsl --status
docker run hello-world
```

**Linux**：
```bash
# 添加用户到 docker 组
sudo usermod -aG docker $USER
newgrp docker
```

## 成本控制

### 开发阶段
- 使用快速验证（$1-2）
- A/B 测试用小任务集（$20-50）
- Chaos 测试用少量重复（$10-30）

### 发布前
- 完整 A/B 测试（$50-100）
- 完整 Chaos 测试（$60-120）
- SWE-bench pilot（10 instances，$5-20）

### 最终验证
- SWE-bench 完整（300 instances，$150-600）

**总预算**：~$300-900

## 时间规划

| 阶段 | 内容 | 时间 | 成本 |
|------|------|------|------|
| 1. 快速验证 | 验证机制工作 | 10 分钟 | $2 |
| 2. A/B 测试 | 上下文效率数字 | 2-3 小时 | $50 |
| 3. Chaos 测试 | 容错性数字 | 3-4 小时 | $60 |
| 4. SWE pilot | 验证框架 | 1-2 小时 | $20 |
| 5. SWE 完整 | 最终基准 | 10-20 小时 | $400 |
| **总计** | | **~20 小时** | **~$532** |

## 下一步

1. **立即**：运行快速验证
   ```bash
   python evals/quick_validate.py
   ```

2. **今天**：运行 A/B 和 Chaos 测试（如果有预算）
   ```bash
   python -m evals.metrics.cli all
   ```

3. **本周**：
   - 获取 SWE-bench 数据集
   - 运行 pilot（10 instances）
   - 验证 Docker 环境

4. **发布前**：
   - 运行完整 SWE-bench（300 instances）
   - 更新所有文档的数字
   - 创建演示视频

## 注意事项

⚠️ **重要**：
- 完整评测成本高，先用小规模验证
- Chaos 测试可能产生意外错误，正常现象
- SWE-bench 需要时间，建议 overnight 运行
- 保存所有结果 JSON，用于后续分析

✅ **完成标准**：
- 快速验证通过
- A/B 测试显示 ≥40% token 减少
- Chaos 测试显示 ≥95% 恢复率
- SWE-bench 有实际数字（pilot 即可）

达到这些标准后，M5 的数字验证部分即完成。
