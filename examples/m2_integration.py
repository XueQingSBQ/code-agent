"""Integration example: M2 governance components working together.

This demonstrates how PermissionEngine, HookRegistry, BashGuard, AuditLogger,
and SessionStore combine to provide complete governance for tool execution.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from carbide.governance.audit import AuditLogger
from carbide.governance.bashguard import BashGuard, Severity
from carbide.governance.hooks import HookContext, HookRegistry
from carbide.governance.permission import PermissionEngine, PermissionMode
from carbide.governance.session_store import SessionStore
from carbide.tools.base import Tool, ToolResult
from carbide.tools.shell import BashTool


def create_bash_safety_hook(guard: BashGuard):
    """Create a before-hook that uses BashGuard to block dangerous commands."""

    def check_bash_safety(tool: Tool, ctx: HookContext) -> ToolResult | None:
        # Only check bash tools
        if not isinstance(tool, BashTool):
            return None

        # Extract command from arguments
        command = ctx.arguments.command if hasattr(ctx.arguments, "command") else ""

        # Analyze with BashGuard
        findings = guard.analyze(command)
        danger = [f for f in findings if f.severity == Severity.DANGER]

        if danger:
            messages = [f.message for f in danger]
            return ToolResult(
                content=f"Command blocked by safety analysis: {', '.join(messages)}",
                is_error=True,
            )

        return None

    return check_bash_safety


def create_audit_hook(logger: AuditLogger):
    """Create an after-hook that logs all tool executions to AuditLogger."""

    def audit(tool: Tool, ctx: HookContext, result: ToolResult) -> ToolResult:
        logger.log_tool_result(
            tool_name=ctx.tool_name,
            result=result.content,
            is_error=result.is_error,
            duration_ms=0.0,  # Would be calculated by executor
        )
        return result

    return audit


def create_error_hook(logger: AuditLogger):
    """Create an error hook that logs exceptions."""

    def log_error(tool: Tool, ctx: HookContext, error: Exception) -> None:
        logger.log_error(tool_name=ctx.tool_name, error=str(error))

    return log_error


# Example usage
def example_integration():
    """Show how all M2 components work together."""
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # 1. Permission engine controls what can run (4 modes)
        permission = PermissionEngine(mode=PermissionMode.PROMPT)

        # 2. Audit logger records all operations
        audit_log_path = tmp_path / "audit.jsonl"
        audit_logger = AuditLogger(audit_log_path, session_id="demo-session")
        audit_logger.open()

        # 3. Session store for persistence
        db_path = tmp_path / "sessions.db"
        session_store = SessionStore(db_path)
        session_store.open()
        session_store.create_session("demo-session", Path.cwd())

        # 4. Hook registry intercepts execution (5 event types)
        hooks = HookRegistry()

        # 5. BashGuard analyzes shell commands
        guard = BashGuard()

        # Wire up the safety hook (PRE_TOOL event)
        safety_hook = create_bash_safety_hook(guard)
        hooks.register_before(safety_hook)

        # Wire up the audit hook (POST_TOOL event)
        audit_hook = create_audit_hook(audit_logger)
        hooks.register_after(audit_hook)

        # Wire up the error hook (ON_ERROR event)
        error_hook = create_error_hook(audit_logger)
        hooks.register_error(error_hook)

        # Now when executing a tool:
        # 1. Permission engine checks if the tool is allowed (4 modes: ALWAYS_ALLOW, PROMPT, AUDIT, DENY)
        # 2. PRE_TOOL hook: BashGuard analyzes the command for safety
        # 3. If allowed and safe, the command executes
        # 4. POST_TOOL hook: AuditLogger records the result
        # 5. ON_ERROR hook: AuditLogger records any exceptions
        # 6. SessionStore persists conversation history for --resume

        print("M2 Governance Stack (Complete):")
        print("  1. PermissionEngine - 4 modes (ALWAYS_ALLOW, PROMPT, AUDIT, DENY)")
        print("  2. HookRegistry - 5 event types (PRE_TOOL, POST_TOOL, ON_ERROR, PRE_API_CALL, POST_API_CALL)")
        print("  3. BashGuard - command safety analysis")
        print("  4. AuditLogger - JSONL append-only audit trail")
        print("  5. SessionStore - SQLite session persistence + --resume")
        print("\nAll M2 components initialized successfully!")

        # Demonstrate permission modes
        print("\nPermission Modes:")
        print(f"  Current: {permission.mode}")
        print("  - ALWAYS_ALLOW: full trust, no prompts")
        print("  - PROMPT: human approval for destructive ops")
        print("  - AUDIT: log all but don't block")
        print("  - DENY: reject all destructive operations")

        # Clean up
        audit_logger.close()
        session_store.close()

        return permission, hooks, guard, audit_logger, session_store


if __name__ == "__main__":
    example_integration()
