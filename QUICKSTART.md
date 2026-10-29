# Carbide Agent 启动指南

## 快速开始

### 1. 安装依赖

```bash
cd E:/claude
pip install -e .
```

### 2. 配置 API 密钥

创建 `.env` 文件：

```bash
cp .env.example .env
```

编辑 `.env` 添加你的 API 密钥：

```env
ANTHROPIC_API_KEY=你的密钥
ANTHROPIC_BASE_URL=https://synai996.space  # 或官方 API
ANTHROPIC_MODEL=claude-sonnet-4-6
```

### 3. 基本使用

#### 方式 A: Python 脚本

创建 `test_agent.py`:

```python
import asyncio
from carbide import Agent

async def main():
    # 创建 agent
    agent = Agent(
        enable_compression=True,  # 启用压缩
        enable_memory=False,
        enable_skills=False,
        enable_rules=False,
    )
    
    # 发送查询
    print("Agent: ", end="", flush=True)
    async for event in agent.query("帮我写一个 Python 快速排序函数"):
        if event.type == "chunk" and event.data:
            if hasattr(event.data, 'content') and event.data.content:
                print(event.data.content, end="", flush=True)
    
    print("\n")
    
    # 查看统计
    stats = agent.get_stats()
    print(f"Token 使用: {stats['total_tokens']} (输入: {stats['total_input_tokens']}, 输出: {stats['total_output_tokens']})")

if __name__ == "__main__":
    asyncio.run(main())
```

运行：

```bash
python test_agent.py
```

#### 方式 B: 交互式使用

创建 `chat.py`:

```python
import asyncio
from carbide import Agent

async def chat():
    agent = Agent(enable_compression=True)
    
    print("Carbide Agent (输入 'quit' 退出)")
    print("=" * 50)
    
    while True:
        # 获取用户输入
        user_input = input("\nYou: ").strip()
        
        if user_input.lower() in ['quit', 'exit', 'q']:
            break
            
        if not user_input:
            continue
        
        # 发送查询
        print("Agent: ", end="", flush=True)
        async for event in agent.query(user_input):
            if event.type == "chunk" and event.data:
                if hasattr(event.data, 'content') and event.data.content:
                    print(event.data.content, end="", flush=True)
        
        print()  # 换行
        
        # 显示统计
        stats = agent.get_stats()
        print(f"\n[Tokens: {stats['total_tokens']}]")

if __name__ == "__main__":
    asyncio.run(chat())
```

运行：

```bash
python chat.py
```

### 4. 运行测试

#### 压缩和 Token 追踪测试

```bash
cd E:/claude
ANTHROPIC_API_KEY=你的密钥 ANTHROPIC_BASE_URL=https://synai996.space python test_compression_fix.py
```

应该看到：

```
[OK] PASS: Compression enabled
[OK] PASS: Compression disabled
[OK] PASS: Token tracking

Result: 3/3 tests passed
```

#### A/B 测试（验证压缩效果）

```bash
python evals/run_ab_test.py
```

这会对比有/无压缩的 token 使用，验证是否减少 40%+。

#### Chaos 测试（验证容错性）

```bash
python evals/chaos_fault_injection.py
```

验证在各种故障下的恢复率是否 ≥95%。

### 5. 使用完整功能

启用所有子系统：

```python
from pathlib import Path
from carbide import Agent

# 创建项目目录结构。项目只保存项目状态和规则。
project_dir = Path("my_project")
project_dir.mkdir(exist_ok=True)

# 项目数据目录
(project_dir / ".carbide" / "memory").mkdir(parents=True, exist_ok=True)
(project_dir / ".carbide" / "rules").mkdir(parents=True, exist_ok=True)

# Agent 技能属于用户级 Carbide 安装，不属于项目目录
agent_skills_dir = Path.home() / ".carbide" / "skills"
agent_skills_dir.mkdir(parents=True, exist_ok=True)

# 创建 agent（启用所有功能）
agent = Agent(
    project_dir=project_dir,
    enable_compression=True,  # 压缩管理
    enable_memory=True,       # 记忆系统
    enable_skills=True,       # 技能系统
    agent_skills_dir=agent_skills_dir,
    enable_rules=True,        # 规则系统
)
```

### 6. 环境变量

可以通过环境变量配置：

```bash
export ANTHROPIC_API_KEY=你的密钥
export ANTHROPIC_BASE_URL=https://synai996.space
export ANTHROPIC_MODEL=claude-sonnet-4-6
export CARBIDE_HOME=~/.carbide
# 可选：覆盖 Agent 技能目录
export CARBIDE_SKILLS_DIR=~/.carbide/skills

python your_script.py
```

或者在代码中直接指定：

```python
from carbide.providers.anthropic import AnthropicProvider
from carbide import Agent

provider = AnthropicProvider(
    api_key="你的密钥",
    base_url="https://synai996.space",
    model="claude-sonnet-4-6",
)

agent = Agent(provider=provider)
```

## 常见问题

### Q: 提示 "ANTHROPIC_API_KEY not set"

**A**: 设置环境变量或在代码中提供 API key

```bash
export ANTHROPIC_API_KEY=你的密钥
```

### Q: 提示 "module 'carbide' has no attribute 'Agent'"

**A**: 重新安装包：

```bash
pip install -e .
```

### Q: 503 错误 "model_not_found"

**A**: 检查模型名称，使用 `claude-sonnet-4-6` 而不是 `claude-sonnet-4-20250514`

```python
provider = AnthropicProvider(model="claude-sonnet-4-6")
```

### Q: Token 统计为 0

**A**: 已修复，确保使用最新代码：

```bash
git pull
pip install -e .
```

## 目录结构

```
E:/claude/
├── src/carbide/          # 核心代码
│   ├── sdk.py           # Agent 主接口
│   ├── providers/       # LLM providers
│   ├── engine/          # 查询循环
│   ├── context/         # 压缩管理器
│   ├── memory/          # 记忆系统
│   ├── skills/          # 技能系统
│   └── rules/           # 规则系统
├── evals/               # 评测脚本
├── test_*.py            # 测试脚本
└── docs/                # 文档

```

说明：项目的 `.carbide/` 目录保存记忆、规则、审计和会话数据；Agent 技能默认从
`Path.home() / ".carbide" / "skills"` 加载。也可以通过 `CARBIDE_HOME`、
`CARBIDE_SKILLS_DIR` 或 SDK 的 `agent_skills_dir` 指定位置。项目中的
`.carbide/skills/` 不会被自动当作 Agent 技能加载。对话中使用 `/skill-name` 可以激活
已安装的 Agent 技能，例如 `/code-review review src/main.py`。模型也会看到技能的名称和
描述，并可在任务需要时自主调用 `load_skill` 工具加载完整内容。

## 下一步

1. **基础使用**: 运行 `chat.py` 体验交互
2. **运行测试**: 验证 token 追踪工作正常
3. **A/B 测试**: 验证压缩效果
4. **Chaos 测试**: 验证容错性

---

需要帮助？查看 `docs/` 目录下的详细文档。
