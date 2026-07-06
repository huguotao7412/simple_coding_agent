"""Minimal integration test — verify MCPToolProvider can start, list tools,
and execute calls end-to-end within a temp directory.

Run: python tests/test_mcp_integration.py
"""

import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.mcp import MCPToolProvider


async def _run_mcp_filesystem_read_write() -> None:
    """Start MCP servers, create a file, read it back, run a bash command."""
    tmpdir = tempfile.mkdtemp(prefix="mcp_test_")
    print(f"Test directory: {tmpdir}")

    try:
        # 1. Start provider
        print("[1/6] Starting MCPToolProvider...")
        provider = MCPToolProvider()
        await provider.start(tmpdir)
        print("       Done.")

        # 2. List tools
        print("[2/6] Listing tools...")
        schemas = await provider.list_tools()
        tool_names = {s["function"]["name"] for s in schemas}
        print(f"       Found {len(schemas)} tools: {sorted(tool_names)}")

        # Verify essential tools exist
        assert "read_file" in tool_names, (
            f"read_file missing from tools: {sorted(tool_names)}"
        )
        assert "write_file" in tool_names, (
            f"write_file missing from tools: {sorted(tool_names)}"
        )
        assert "run" in tool_names, (
            f"'run' (bash) missing from tools: {sorted(tool_names)}"
        )
        print("       [OK] Essential tools verified")

        # 3. Write a test file via MCP
        print("[3/6] Writing test file via MCP...")
        test_content = "Hello from MCP integration test! 你好，MCP！"
        test_path = os.path.join(tmpdir, "test.txt")
        result = await provider.call_tool("write_file", {
            "path": test_path,
            "content": test_content,
        })
        assert result.success, f"write_file failed: {result.error}"
        print(f"       [OK] {result.content[:100]}")

        # 4. Read it back via MCP
        print("[4/6] Reading test file via MCP...")
        result = await provider.call_tool("read_file", {"path": test_path})
        assert result.success, f"read_file failed: {result.error}"
        assert test_content in result.content, (
            f"Expected '{test_content}' in read result, got: {result.content[:200]}"
        )
        print(f"       [OK] Content matches")

        # 5. Run a bash command
        print("[5/6] Running bash command via MCP...")
        result = await provider.call_tool("run", {
            "command": "echo hello_from_bash && pwd",
        })
        assert result.success, f"bash run failed: {result.error}"
        assert "hello_from_bash" in result.content, (
            f"Expected 'hello_from_bash' in output, got: {result.content[:200]}"
        )
        print(f"       [OK] bash command succeeded")

        # 6. Shutdown
        print("[6/6] Shutting down MCP servers...")
        await provider.shutdown()
        print("       [OK] Shutdown completed")

        print("\n=== ALL MCP INTEGRATION TESTS PASSED ===")

    except AssertionError:
        print("\n=== INTEGRATION TEST FAILED ===")
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"Cleaned up: {tmpdir}")


if __name__ == "__main__":
    asyncio.run(_run_mcp_filesystem_read_write())


def test_mcp_filesystem_read_write() -> None:
    asyncio.run(_run_mcp_filesystem_read_write())
