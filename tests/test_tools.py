"""Tool behaviour, with emphasis on how failures are reported.

A tool's error message is a prompt: the model reads it and decides what to do
next. "not found" ends a run; "not found, read the file and retry" recovers one.
These tests assert the messages are actionable, not just correct.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from carbide.tools.base import Tool, ToolContext, ToolResult
from carbide.tools.fs import DEFAULT_READ_LIMIT, EditTool, ReadTool, WriteTool
from carbide.tools.search import GLOB_LIMIT, GlobTool, GrepTool
from carbide.tools.shell import OUTPUT_LIMIT, BashTool, _shell_command, validate_command_lifecycle


async def _run(tool: Tool, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Validate through the tool's own params model, as the registry does."""
    return await tool.run(type(tool).params.model_validate(args), ctx)


class TestRead:
    async def test_returns_numbered_lines(self, tool_ctx: ToolContext) -> None:
        result = await _run(ReadTool(), {"path": "hello.py"}, tool_ctx)
        assert result.content == "     1\tprint('hi')"

    async def test_offset_and_limit_window(self, tool_ctx: ToolContext) -> None:
        (tool_ctx.workspace / "many.txt").write_text(
            "\n".join(f"line {i}" for i in range(10)), encoding="utf-8"
        )
        result = await _run(ReadTool(), {"path": "many.txt", "offset": 3, "limit": 2}, tool_ctx)
        # Line numbers stay absolute, so the model can cite them.
        assert "     4\tline 3" in result.content
        assert "     5\tline 4" in result.content
        assert "line 5" not in result.content
        assert "5 more lines" in result.content

    async def test_truncation_says_how_to_continue(self, tool_ctx: ToolContext) -> None:
        (tool_ctx.workspace / "big.txt").write_text(
            "\n".join(str(i) for i in range(DEFAULT_READ_LIMIT + 50)), encoding="utf-8"
        )
        result = await _run(ReadTool(), {"path": "big.txt"}, tool_ctx)
        assert "truncated: 50 more lines" in result.content
        assert "offset" in result.content

    async def test_missing_file_is_an_error_not_a_crash(self, tool_ctx: ToolContext) -> None:
        result = await _run(ReadTool(), {"path": "nope.py"}, tool_ctx)
        assert result.is_error
        assert "Not a file" in result.content

    async def test_directory_is_rejected(self, tool_ctx: ToolContext) -> None:
        (tool_ctx.workspace / "subdir").mkdir()
        result = await _run(ReadTool(), {"path": "subdir"}, tool_ctx)
        assert result.is_error

    async def test_binary_content_does_not_raise(self, tool_ctx: ToolContext) -> None:
        (tool_ctx.workspace / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
        result = await _run(ReadTool(), {"path": "blob.bin"}, tool_ctx)
        assert not result.is_error

    async def test_empty_file_is_labelled(self, tool_ctx: ToolContext) -> None:
        (tool_ctx.workspace / "empty.txt").write_text("", encoding="utf-8")
        result = await _run(ReadTool(), {"path": "empty.txt"}, tool_ctx)
        assert result.content == "[empty file]"


class TestWrite:
    async def test_creates_parent_directories(self, tool_ctx: ToolContext) -> None:
        result = await _run(WriteTool(), {"path": "a/b/c.txt", "content": "hello\n"}, tool_ctx)
        assert not result.is_error
        assert (tool_ctx.workspace / "a/b/c.txt").read_text(encoding="utf-8") == "hello\n"

    async def test_overwrites(self, tool_ctx: ToolContext) -> None:
        await _run(WriteTool(), {"path": "hello.py", "content": "print('bye')\n"}, tool_ctx)
        assert (tool_ctx.workspace / "hello.py").read_text(encoding="utf-8") == "print('bye')\n"


class TestEdit:
    async def test_replaces_a_unique_string(self, tool_ctx: ToolContext) -> None:
        result = await _run(
            EditTool(), {"path": "hello.py", "old_text": "hi", "new_text": "hello"}, tool_ctx
        )
        assert not result.is_error
        assert (tool_ctx.workspace / "hello.py").read_text(encoding="utf-8") == "print('hello')\n"

    async def test_ambiguous_match_is_refused_with_a_count(self, tool_ctx: ToolContext) -> None:
        """Replacing the wrong one of three is worse than refusing."""
        (tool_ctx.workspace / "dup.py").write_text("x = 1\nx = 1\nx = 1\n", encoding="utf-8")
        result = await _run(
            EditTool(), {"path": "dup.py", "old_text": "x = 1", "new_text": "x = 2"}, tool_ctx
        )
        assert result.is_error
        assert "3 times" in result.content
        assert "unique" in result.content
        # Nothing was written.
        assert (tool_ctx.workspace / "dup.py").read_text(encoding="utf-8").count("x = 1") == 3

    async def test_no_match_tells_the_model_what_to_do(self, tool_ctx: ToolContext) -> None:
        result = await _run(
            EditTool(),
            {"path": "hello.py", "old_text": "not there", "new_text": "x"},
            tool_ctx,
        )
        assert result.is_error
        assert "Read the file and retry" in result.content


class TestWorkspaceContainment:
    """Path traversal is refused by every filesystem tool, not just Read."""

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            (ReadTool(), {"path": "../outside.txt"}),
            (WriteTool(), {"path": "../outside.txt", "content": "x"}),
            (EditTool(), {"path": "../outside.txt", "old_text": "a", "new_text": "b"}),
        ],
        ids=["read", "write", "edit"],
    )
    async def test_parent_traversal_is_refused(
        self, tool: Tool, args: Mapping[str, Any], tool_ctx: ToolContext
    ) -> None:
        outside = tool_ctx.workspace.parent / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        result = await _run(tool, args, tool_ctx)
        assert result.is_error
        assert "escapes the workspace" in result.content
        assert outside.read_text(encoding="utf-8") == "secret\n"

    async def test_absolute_path_outside_is_refused(self, tool_ctx: ToolContext) -> None:
        target = "C:/Windows/win.ini" if sys.platform == "win32" else "/etc/hosts"
        result = await _run(ReadTool(), {"path": target}, tool_ctx)
        assert result.is_error
        assert "escapes the workspace" in result.content

    async def test_absolute_path_inside_is_allowed(self, tool_ctx: ToolContext) -> None:
        inside = str(tool_ctx.workspace / "hello.py")
        result = await _run(ReadTool(), {"path": inside}, tool_ctx)
        assert not result.is_error


class TestGlob:
    async def test_sorts_newest_first(self, tool_ctx: ToolContext) -> None:
        for name in ("old.py", "new.py"):
            (tool_ctx.workspace / name).write_text("x\n", encoding="utf-8")
        now = time.time()
        os.utime(tool_ctx.workspace / "old.py", (now - 1000, now - 1000))
        os.utime(tool_ctx.workspace / "new.py", (now, now))

        result = await _run(GlobTool(), {"pattern": "*.py"}, tool_ctx)
        listed = result.content.splitlines()
        assert listed.index("new.py") < listed.index("old.py")

    async def test_no_match_is_not_an_error(self, tool_ctx: ToolContext) -> None:
        result = await _run(GlobTool(), {"pattern": "*.nothing"}, tool_ctx)
        assert not result.is_error
        assert result.content == "No files matched."

    async def test_caps_results_and_says_so(self, tool_ctx: ToolContext) -> None:
        for i in range(GLOB_LIMIT + 5):
            (tool_ctx.workspace / f"f{i}.tmp").write_text("x", encoding="utf-8")
        result = await _run(GlobTool(), {"pattern": "*.tmp"}, tool_ctx)
        assert len(result.content.splitlines()) == GLOB_LIMIT + 2  # + blank + notice
        assert "5 more matches" in result.content

    async def test_skips_an_inaccessible_file(self, tool_ctx: ToolContext, monkeypatch: Any) -> None:
        """One Windows-inaccessible path must not abort the whole search."""
        blocked = tool_ctx.workspace / "blocked.py"
        visible = tool_ctx.workspace / "visible.py"
        blocked.write_text("blocked", encoding="utf-8")
        visible.write_text("visible", encoding="utf-8")

        original_is_file = Path.is_file

        def flaky_is_file(path: Path) -> bool:
            if path == blocked:
                raise OSError("simulated WinError 1920")
            return original_is_file(path)

        monkeypatch.setattr(Path, "is_file", flaky_is_file)
        result = await _run(GlobTool(), {"pattern": "*.py"}, tool_ctx)

        assert not result.is_error
        assert "visible.py" in result.content
        assert "skipped 1 inaccessible path" in result.content


def _grep(*, use_ripgrep: bool) -> GrepTool:
    tool = GrepTool()
    # Instance attribute shadows the class default, so the two backends can be
    # driven independently without touching global state.
    object.__setattr__(tool, "prefer_ripgrep", use_ripgrep)
    return tool


BACKENDS = [
    pytest.param(False, id="python"),
    pytest.param(
        True,
        id="ripgrep",
        marks=pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed"),
    ),
]


@pytest.mark.parametrize("use_ripgrep", BACKENDS)
class TestGrep:
    """Run the whole suite against both backends.

    The fallback is not a second-class path: on a machine without ripgrep it is
    the only path, so it gets the same coverage.
    """

    @pytest.fixture(autouse=True)
    def _corpus(self, tool_ctx: ToolContext) -> None:
        (tool_ctx.workspace / "a.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
        (tool_ctx.workspace / "b.py").write_text("def beta():\n    alpha()\n", encoding="utf-8")
        (tool_ctx.workspace / "c.txt").write_text("alpha in text\n", encoding="utf-8")

    async def test_default_mode_returns_paths_only(
        self, tool_ctx: ToolContext, use_ripgrep: bool
    ) -> None:
        result = await _run(_grep(use_ripgrep=use_ripgrep), {"pattern": "alpha"}, tool_ctx)
        assert not result.is_error
        assert set(result.content.splitlines()) == {"a.py", "b.py", "c.txt"}
        # Paths only: no matched source lines.
        assert "def alpha" not in result.content

    async def test_content_mode_returns_path_line_text(
        self, tool_ctx: ToolContext, use_ripgrep: bool
    ) -> None:
        result = await _run(
            _grep(use_ripgrep=use_ripgrep), {"pattern": "def alpha", "mode": "content"}, tool_ctx
        )
        assert result.content == "a.py:1:def alpha():"

    async def test_count_mode_returns_per_file_counts(
        self, tool_ctx: ToolContext, use_ripgrep: bool
    ) -> None:
        (tool_ctx.workspace / "d.py").write_text("alpha\nalpha\n", encoding="utf-8")
        result = await _run(
            _grep(use_ripgrep=use_ripgrep), {"pattern": "alpha", "mode": "count"}, tool_ctx
        )
        assert "d.py:2" in result.content

    async def test_glob_filter_applies(self, tool_ctx: ToolContext, use_ripgrep: bool) -> None:
        result = await _run(
            _grep(use_ripgrep=use_ripgrep), {"pattern": "alpha", "glob": "*.py"}, tool_ctx
        )
        assert "c.txt" not in result.content
        assert "a.py" in result.content

    async def test_no_matches_is_a_normal_result(
        self, tool_ctx: ToolContext, use_ripgrep: bool
    ) -> None:
        """ripgrep exits 1 for no matches, which is an outcome, not a failure."""
        result = await _run(
            _grep(use_ripgrep=use_ripgrep), {"pattern": "zzz_nothing_zzz"}, tool_ctx
        )
        assert not result.is_error
        assert result.content == "No matches."

    async def test_invalid_regex_is_reported_as_an_error(
        self, tool_ctx: ToolContext, use_ripgrep: bool
    ) -> None:
        result = await _run(_grep(use_ripgrep=use_ripgrep), {"pattern": "([unclosed"}, tool_ctx)
        assert result.is_error
        assert "Invalid regular expression" in result.content

    async def test_case_insensitive(self, tool_ctx: ToolContext, use_ripgrep: bool) -> None:
        result = await _run(
            _grep(use_ripgrep=use_ripgrep),
            {"pattern": "ALPHA", "case_insensitive": True},
            tool_ctx,
        )
        assert "a.py" in result.content

    async def test_single_file_target(self, tool_ctx: ToolContext, use_ripgrep: bool) -> None:
        result = await _run(
            _grep(use_ripgrep=use_ripgrep),
            {"pattern": "alpha", "path": "b.py", "mode": "content"},
            tool_ctx,
        )
        assert result.content == "b.py:2:    alpha()"

    async def test_traversal_is_refused(self, tool_ctx: ToolContext, use_ripgrep: bool) -> None:
        result = await _run(
            _grep(use_ripgrep=use_ripgrep), {"pattern": "alpha", "path": "../.."}, tool_ctx
        )
        assert result.is_error
        assert "escapes the workspace" in result.content

    async def test_binary_files_are_skipped(self, tool_ctx: ToolContext, use_ripgrep: bool) -> None:
        (tool_ctx.workspace / "blob.bin").write_bytes(b"alpha\x00\x01\x02alpha")
        result = await _run(_grep(use_ripgrep=use_ripgrep), {"pattern": "alpha"}, tool_ctx)
        assert "blob.bin" not in result.content

    async def test_noise_directories_are_pruned(
        self, tool_ctx: ToolContext, use_ripgrep: bool
    ) -> None:
        noise = tool_ctx.workspace / "node_modules" / "pkg"
        noise.mkdir(parents=True)
        (noise / "index.js").write_text("alpha\n", encoding="utf-8")
        result = await _run(_grep(use_ripgrep=use_ripgrep), {"pattern": "alpha"}, tool_ctx)
        assert "node_modules" not in result.content


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
class TestBackendEquivalence:
    """The two backends must agree, or behaviour depends on the machine."""

    @pytest.fixture(autouse=True)
    def _corpus(self, tool_ctx: ToolContext) -> None:
        (tool_ctx.workspace / "one.py").write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
        (tool_ctx.workspace / "two.py").write_text("gamma\nalpha\n", encoding="utf-8")
        nested = tool_ctx.workspace / "pkg"
        nested.mkdir()
        (nested / "three.py").write_text("alpha beta\n", encoding="utf-8")

    @pytest.mark.parametrize("mode", ["files_with_matches", "content", "count"])
    async def test_same_output_for_the_same_query(self, tool_ctx: ToolContext, mode: str) -> None:
        args: Mapping[str, Any] = {"pattern": "alpha", "mode": mode}
        with_rg = await _run(_grep(use_ripgrep=True), args, tool_ctx)
        without = await _run(_grep(use_ripgrep=False), args, tool_ctx)
        assert sorted(with_rg.content.splitlines()) == sorted(without.content.splitlines())


class TestShellCommandConstruction:
    """Backend selection is pure, so it is testable without running a shell."""

    def test_wsl_wraps_in_bash_login(self) -> None:
        argv = _shell_command("wsl", "echo hi")
        assert argv[-3:] == ["bash", "-lc", "echo hi"]

    def test_posix_uses_bin_bash(self) -> None:
        assert _shell_command("posix", "echo hi") == ["/bin/bash", "-lc", "echo hi"]

    def test_command_is_one_argv_element(self) -> None:
        """The command is never split on whitespace.

        Passing it as a single argument is what keeps shell metacharacters from
        being reinterpreted by the argv layer.
        """
        argv = _shell_command("gitbash", "echo 'a b'; rm -rf /tmp/x")
        assert argv[-1] == "echo 'a b'; rm -rf /tmp/x"
        assert argv[-2] == "-lc"


class TestShellCommandLifecycle:
    """Long-lived commands must not block the persistent shell protocol."""

    @pytest.mark.parametrize(
        "command",
        [
            "python3 -m http.server 8000",
            "npm run dev",
            "node -e \"require('http').createServer().listen(8000)\"",
        ],
    )
    def test_foreground_server_is_rejected(self, command: str) -> None:
        error = validate_command_lifecycle(command)
        assert error is not None
        assert "foreground" in error
        assert "background" in error

    def test_background_server_is_allowed(self) -> None:
        command = "python3 -m http.server 8000 > .carbide/server.log 2>&1 & echo $!"
        assert validate_command_lifecycle(command) is None

    def test_ampersand_inside_quotes_is_not_backgrounding(self) -> None:
        command = 'node -e "require(\'http\').createServer().listen(8000, \'a&b\')"'
        error = validate_command_lifecycle(command)
        assert error is not None

    def test_markdown_url_is_rejected_with_plain_url_guidance(self) -> None:
        error = validate_command_lifecycle(
            "curl -I [http://127.0.0.1:8000/](http://127.0.0.1:8000/)"
        )
        assert error is not None
        assert "plain URL" in error


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
class TestBash:
    async def test_captures_output(self, tool_ctx: ToolContext) -> None:
        result = await _run(BashTool(), {"command": "echo hello"}, tool_ctx)
        assert not result.is_error
        assert result.content == "hello"

    async def test_nonzero_exit_reports_the_status(self, tool_ctx: ToolContext) -> None:
        result = await _run(BashTool(), {"command": "echo oops >&2; exit 3"}, tool_ctx)
        assert result.is_error
        assert "exit 3" in result.content
        # stderr is merged, so the model sees why it failed.
        assert "oops" in result.content

    async def test_runs_in_the_workspace(self, tool_ctx: ToolContext) -> None:
        result = await _run(BashTool(), {"command": "ls"}, tool_ctx)
        assert "hello.py" in result.content

    async def test_timeout_kills_the_process(self, tool_ctx: ToolContext) -> None:
        result = await _run(BashTool(), {"command": "sleep 5", "timeout": 0.3}, tool_ctx)
        assert result.is_error
        assert "timed out" in result.content

    async def test_huge_output_is_elided_from_the_middle(self, tool_ctx: ToolContext) -> None:
        """Head and tail are what carry the signal; the middle is filler.

        Every character returned is paid for again on every later turn, so the
        cap is cost control rather than tidiness.
        """
        result = await _run(
            BashTool(),
            {"command": f"python -c \"print('x' * {OUTPUT_LIMIT * 2})\""},
            tool_ctx,
        )
        assert "characters elided" in result.content
        assert len(result.content) < OUTPUT_LIMIT + 500

    async def test_no_output_is_labelled(self, tool_ctx: ToolContext) -> None:
        result = await _run(BashTool(), {"command": "true"}, tool_ctx)
        assert result.content == "[no output]"

    async def test_git_pager_is_disabled(self, tool_ctx: ToolContext) -> None:
        """A pager waiting for input would hang the agent forever."""
        result = await _run(BashTool(), {"command": "echo $GIT_PAGER"}, tool_ctx)
        assert result.content == "cat"

    async def test_foreground_server_is_rejected_before_spawn(
        self, tool_ctx: ToolContext
    ) -> None:
        result = await _run(BashTool(), {"command": "python3 -m http.server 8000"}, tool_ctx)
        assert result.is_error
        assert "foreground" in result.content


async def test_registry_returns_validation_errors_to_the_model(tool_ctx: ToolContext) -> None:
    """Bad arguments must come back as a readable error, not an exception.

    The model can correct itself from a validation message; it cannot recover
    from a crashed run.
    """
    from carbide.sdk import default_registry

    result = await default_registry().invoke("read", {"path": 123, "offset": -5}, tool_ctx)
    assert result.is_error
    assert "Invalid arguments for read" in result.content
    assert "offset" in result.content


async def test_unknown_tool_name_is_an_error_result(tool_ctx: ToolContext) -> None:
    from carbide.sdk import default_registry

    result = await default_registry().invoke("nonexistent", {}, tool_ctx)
    assert result.is_error
    assert "Unknown tool" in result.content


def test_read_limit_is_declared_in_the_description() -> None:
    """The model plans around the cap, so the number has to be in the prompt.

    A cap the model cannot see is a cap it will keep hitting by accident.
    """
    assert str(DEFAULT_READ_LIMIT) in ReadTool.description
    assert str(GLOB_LIMIT) in GlobTool.description
