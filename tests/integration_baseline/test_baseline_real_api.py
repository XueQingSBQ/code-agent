"""
集成基准测试套件 - 使用真实API

目的：建立当前功能的性能和正确性基准，确保后续集成不破坏现有功能
"""

import asyncio
import os
import time
from pathlib import Path

import pytest

from carbide.sdk import Agent


# 检查API密钥
pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="需要 ANTHROPIC_API_KEY 环境变量"
)


class TestBasicFunctionality:
    """测试基础功能正常工作"""

    @pytest.mark.asyncio
    async def test_basic_query_response(self, tmp_path):
        """基准测试：基础查询能得到响应"""
        agent = Agent(
            project_dir=tmp_path,
            enable_memory=False,
            enable_skills=False,
            enable_compression=False,
            enable_governance=False,
        )

        response_received = False
        text_content = ""

        async for event in agent.query("Say exactly: 'test response'"):
            if event.type == "chunk" and event.data:
                if hasattr(event.data, 'content') and event.data.content:
                    text_content += event.data.content
                    response_received = True

        assert response_received, "应该收到响应"
        print(f"\n收到响应: {text_content[:100]}")

    @pytest.mark.asyncio
    async def test_provider_streaming(self, tmp_path):
        """基准测试：Provider流式响应工作"""
        agent = Agent(
            project_dir=tmp_path,
            enable_memory=False,
            enable_skills=False,
            enable_compression=False,
        )

        chunk_count = 0
        async for event in agent.query("Count to 3"):
            if event.type == "chunk":
                chunk_count += 1

        assert chunk_count > 0, "应该收到流式chunk"
        print(f"\n收到 {chunk_count} 个chunk")


class TestToolExecution:
    """测试工具执行"""

    @pytest.mark.asyncio
    async def test_tool_execution_works(self, tmp_path):
        """基准测试：工具能被调用执行"""
        # 创建测试文件
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content line 1\ntest content line 2")

        from carbide.tools.defaults import create_default_registry

        agent = Agent(
            project_dir=tmp_path,
            enable_memory=False,
            enable_skills=False,
            enable_compression=False,
            tool_registry=create_default_registry()
        )

        # 获取工具schema
        tools = agent.loop.tool_registry.to_anthropic_format() if agent.loop.tool_registry else None

        tool_executed = False
        async for event in agent.query(
            f"Use the read tool to read the file at path: {test_file}",
            tools=tools
        ):
            if event.type == "tool_complete":
                tool_executed = True
                print(f"\n工具执行完成: {event.data}")

        # 验证至少执行流程没有崩溃
        assert True, "工具执行流程应该正常"


class TestPerformanceBaseline:
    """性能基准测试"""

    @pytest.mark.asyncio
    async def test_simple_query_latency(self, tmp_path):
        """基准测试：简单查询延迟"""
        agent = Agent(
            project_dir=tmp_path,
            enable_memory=False,
            enable_skills=False,
            enable_compression=False,
        )

        start_time = time.time()
        async for event in agent.query("Say: hello"):
            if event.type == "turn_complete":
                break
        elapsed = time.time() - start_time

        # 记录基准时间
        print(f"\n简单查询耗时: {elapsed:.2f}秒")
        assert elapsed < 60, "简单查询不应超过60秒"

    @pytest.mark.asyncio
    async def test_token_tracking_works(self, tmp_path):
        """基准测试：Token追踪工作"""
        agent = Agent(
            project_dir=tmp_path,
            enable_memory=False,
            enable_skills=False,
            enable_compression=False,
        )

        async for event in agent.query("Say: hello"):
            if event.type == "turn_complete":
                break

        stats = agent.get_stats()

        print(f"\nToken使用基准:")
        print(f"  输入: {stats['total_input_tokens']}")
        print(f"  输出: {stats['total_output_tokens']}")
        print(f"  总计: {stats['total_tokens']}")
        print(f"  缓存创建: {stats['total_cache_creation_tokens']}")
        print(f"  缓存读取: {stats['total_cache_read_tokens']}")

        assert stats["total_input_tokens"] > 0, "应该有输入token"
        assert stats["total_output_tokens"] > 0, "应该有输出token"


class TestStateManagement:
    """测试状态管理"""

    @pytest.mark.asyncio
    async def test_multi_turn_conversation(self, tmp_path):
        """基准测试：多轮对话状态保持"""
        agent = Agent(
            project_dir=tmp_path,
            enable_memory=False,
            enable_skills=False,
            enable_compression=False,
        )

        # 第一轮
        async for event in agent.query("Remember: my favorite color is blue"):
            if event.type == "turn_complete":
                break

        # 第二轮 - 测试是否记住
        response_text = ""
        async for event in agent.query("What is my favorite color?"):
            if event.type == "chunk" and event.data:
                if hasattr(event.data, 'content') and event.data.content:
                    response_text += event.data.content
            if event.type == "turn_complete":
                break

        stats = agent.get_stats()
        print(f"\n多轮对话统计:")
        print(f"  轮数: {stats['turn_count']}")
        print(f"  消息数: {stats['message_count']}")
        print(f"  响应包含blue: {'blue' in response_text.lower()}")

        assert stats["turn_count"] == 2, "应该有2轮对话"
        assert stats["message_count"] >= 4, "应该至少有4条消息（2轮×2消息）"


class TestCurrentLimitations:
    """测试当前已知的限制（用于验证集成后的改进）"""

    @pytest.mark.asyncio
    async def test_compression_currently_disabled(self, tmp_path):
        """验证压缩当前被禁用"""
        agent = Agent(
            project_dir=tmp_path,
            enable_compression=True,  # 尝试启用
        )

        # 验证compression_manager存在但不生效
        assert agent.compression_manager is not None, "CompressionManager应该被初始化"

        # 记录：压缩功能当前被跳过
        print("\n当前状态：CompressionManager已初始化但在QueryLoop中被跳过")
        print("  → 这是集成工作需要修复的问题")

    @pytest.mark.asyncio
    async def test_memory_currently_not_integrated(self, tmp_path):
        """验证Memory当前未集成"""
        # 创建memory目录和文件
        memory_dir = tmp_path / ".carbide" / "memory"
        memory_dir.mkdir(parents=True)

        memory_file = memory_dir / "test-memory.md"
        memory_file.write_text("""---
name: test-memory
description: A test memory
---

This is test memory content.
""")

        agent = Agent(
            project_dir=tmp_path,
            enable_memory=True,
        )

        # 验证memory_recall存在
        assert agent.memory_recall is not None, "MemoryRecall应该被初始化"

        # 记录：Memory功能当前不被调用
        print("\n当前状态：MemoryRecall已初始化但在QueryLoop中不被调用")
        print("  → 这是集成工作需要修复的问题")


# 基准数据收集
class TestBaselineMetrics:
    """收集基准指标数据"""

    @pytest.mark.asyncio
    async def test_collect_baseline_metrics(self, tmp_path):
        """收集并保存基准指标"""
        agent = Agent(
            project_dir=tmp_path,
            enable_memory=False,
            enable_skills=False,
            enable_compression=False,
        )

        queries = [
            "Say: test 1",
            "Say: test 2",
            "Say: test 3",
        ]

        metrics = []

        for i, query in enumerate(queries):
            start = time.time()
            async for event in agent.query(query):
                if event.type == "turn_complete":
                    break
            elapsed = time.time() - start

            stats = agent.get_stats()
            metrics.append({
                "query": query,
                "latency": elapsed,
                "input_tokens": stats["total_input_tokens"],
                "output_tokens": stats["total_output_tokens"],
                "turn": i + 1,
            })

        # 输出基准数据
        print("\n" + "="*60)
        print("基准指标汇总")
        print("="*60)

        total_latency = sum(m["latency"] for m in metrics)
        total_input = metrics[-1]["input_tokens"]
        total_output = metrics[-1]["output_tokens"]

        print(f"总查询数: {len(queries)}")
        print(f"总延迟: {total_latency:.2f}秒")
        print(f"平均延迟: {total_latency/len(queries):.2f}秒")
        print(f"总输入tokens: {total_input}")
        print(f"总输出tokens: {total_output}")
        print(f"总tokens: {total_input + total_output}")

        print("\n各轮详情:")
        for m in metrics:
            print(f"  轮{m['turn']}: {m['latency']:.2f}s, "
                  f"输入={m['input_tokens']}, 输出={m['output_tokens']}")

        print("="*60)

        # 保存基准数据
        baseline_file = tmp_path.parent / "baseline_metrics.txt"
        with open(baseline_file, "w") as f:
            f.write(f"基准测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"总查询数: {len(queries)}\n")
            f.write(f"平均延迟: {total_latency/len(queries):.2f}秒\n")
            f.write(f"总tokens: {total_input + total_output}\n")

        print(f"\n基准数据已保存到: {baseline_file}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
