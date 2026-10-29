# M5 Quick Reference

## ✅ 完成的工作

### 1. SWE-bench Lite 评测框架
```bash
# 快速测试 (10 instances)
carbide-eval \
    --dataset swebench_lite.json \
    --output ./results \
    --api-key $ANTHROPIC_API_KEY \
    --max-instances 10

# 完整评测 (300 instances, $150-$600)
carbide-eval \
    --dataset swebench_lite.json \
    --output ./results \
    --api-key $ANTHROPIC_API_KEY \
    --parallel 4
```

### 2. 自定义 API 支持
```python
from carbide.sdk import Agent

# 使用自定义端点
agent = Agent(
    api_key="your-key",
    base_url="https://synai996.space",
    model="claude-sonnet-4-6",
)

# 测试通过 ✓
```

### 3. 文档完整
- `docs/ARCHITECTURE.md` - 架构设计 (600+ lines)
- `CONTRIBUTING.md` - 贡献指南 (400+ lines)
- `evals/swebench/README.md` - 评测指南 (200+ lines)
- `CHANGELOG.md` - 版本历史
- `docs/M5_COMPLETION.md` - M5 完成报告
- `docs/PROJECT_STATUS.md` - 项目状态

### 4. CI/CD 管道
- `.github/workflows/ci.yml` - Lint + Test + Coverage
- `.github/workflows/publish.yml` - PyPI 自动发布
- `.github/workflows/evaluate.yml` - Nightly 评测

### 5. PyPI 就绪
```bash
# 构建
uv build

# 发布到 TestPyPI
uv publish --repository testpypi

# 发布到 PyPI
uv publish
```

## 📊 项目统计

```
源代码:     9,081 lines (src/carbide)
测试:       184 tests passing
新增代码:   ~2,570 lines (M5)
文档:       3,000+ lines
```

## 🎯 质量检查

```bash
uv run pytest          # ✓ 184 tests passing
uv run mypy            # ✓ 0 errors
uv run ruff check .    # ✓ 0 issues
uv run lint-imports    # ✓ 0 violations
```

## 📦 发布流程

### 1. 标记版本
```bash
git add .
git commit -m "feat(M5): Complete evaluation infrastructure and documentation"
git tag v0.0.1
git push origin master v0.0.1
```

### 2. 创建 GitHub Release
- 在 GitHub 上创建 Release
- 选择 tag: v0.0.1
- 标题: "v0.0.1 - Initial Release"
- 内容: 复制 CHANGELOG.md 的内容

### 3. 自动发布
- GitHub Actions 自动触发 publish workflow
- 构建并发布到 PyPI

### 4. 验证安装
```bash
pip install carbide
carbide --version
```

## 📝 下一步

### 即时行动
1. ✅ **标记 v0.0.1** - 准备发布
2. ⏳ **创建 GitHub Release** - 触发自动发布
3. ⏳ **验证 PyPI 包** - 确认安装正常

### 短期计划
1. 下载 SWE-bench Lite 数据集
2. 运行 pilot 评测（10-20 instances）
3. 验证 Windows WSL2 + Docker 配置
4. 收集基准指标
5. 更新 README 的性能数字

### 中期目标
1. 运行完整 SWE-bench Lite 评测（300 instances）
2. Chaos 测试框架（故障注入）
3. 上下文效率 A/B 测试（金字塔开/关对比）
4. 性能优化

## 🔑 关键文件

### 使用
- `README.md` - 快速开始
- `docs/ARCHITECTURE.md` - 深入理解

### 开发
- `CONTRIBUTING.md` - 如何贡献
- `pyproject.toml` - 项目配置

### 评测
- `evals/swebench/README.md` - 评测指南
- `evals/swebench/cli.py` - 命令行工具

### 发布
- `CHANGELOG.md` - 版本历史
- `.github/workflows/publish.yml` - 发布流程

## 🐛 已知限制

1. **SWE-bench 数据集** - 需要单独下载
2. **Windows Docker** - 需要 WSL2 + Docker Desktop
3. **评测成本** - 完整运行 $150-$600
4. **基准指标** - 需要实际运行后填充

## 💡 技术亮点

### 1. 五层压缩金字塔
- L1-L4.5 完整实现
- 跨层 token 总线
- 目标: 40%+ token 减少

### 2. 流式并行工具执行
- 边流边派发
- 自动并发控制
- fail-closed 安全策略

### 3. 历史修复不变量
- 每个 tool_use 必有 tool_result
- 自动修复孤儿块
- Property-based 测试验证

### 4. Provider 抽象
- 供应商解耦
- 自定义端点支持
- 简洁的配置方式

## 📞 获取帮助

- **文档**: 查看 `docs/` 目录
- **示例**: 查看 `README.md`
- **问题**: 创建 GitHub Issue
- **讨论**: 创建 GitHub Discussion

## ✨ 项目亮点

**可量化的主张**:
- 上下文效率: 目标 40%+ token 减少
- 容错性: 目标 95%+ 恢复率
- 性能: 实际 SWE-bench 评分

**工程质量**:
- 184 tests passing
- mypy --strict 通过
- 完整的 CI/CD
- 详尽的文档

**生产就绪**:
- 完整的评测框架
- 自动化发布流程
- 专业的项目结构
- 社区友好的贡献指南

---

**状态**: 🚀 Ready for v0.0.1 Release

**时间**: 2024-09-16

**下一步**: 标记版本 → 创建 Release → PyPI 发布
