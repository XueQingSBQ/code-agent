"""Throwaway: time each phase of BashTool's timeout path."""

import asyncio
import os
import time
from pathlib import Path

from carbide.tools.shell import _shell_command


async def main() -> None:
    argv = _shell_command("gitbash", "sleep 5")
    print("argv:", argv)
    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=Path.cwd(),
        env={**os.environ, "GIT_PAGER": "cat", "PAGER": "cat"},
    )
    print(f"spawn      {time.perf_counter() - t0:6.2f}s")

    t1 = time.perf_counter()
    try:
        await asyncio.wait_for(proc.communicate(), timeout=0.3)
    except TimeoutError:
        print(f"wait_for   {time.perf_counter() - t1:6.2f}s (timed out as expected)")

    t2 = time.perf_counter()
    proc.kill()
    print(f"kill()     {time.perf_counter() - t2:6.2f}s")

    t3 = time.perf_counter()
    await proc.wait()
    print(f"wait()     {time.perf_counter() - t3:6.2f}s  <-- the suspect")
    print(f"total      {time.perf_counter() - t0:6.2f}s")


asyncio.run(main())
