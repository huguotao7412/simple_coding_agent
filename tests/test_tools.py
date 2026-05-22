import asyncio
import os
import tempfile
import pytest
from core.tools.read import ReadTool


@pytest.fixture
def ws():
    with tempfile.TemporaryDirectory() as d:
        yield d


class TestReadTool:
    def test_read_entire_file(self, ws):
        p = os.path.join(ws, "f.txt")
        with open(p, "w") as f:
            f.write("a\nb\nc\n")
        r = asyncio.run(ReadTool().execute(file_path=p, workspace_dir=ws))
        assert r.success
        assert "a" in r.content
        assert "b" in r.content

    def test_read_offset_limit(self, ws):
        p = os.path.join(ws, "f.txt")
        with open(p, "w") as f:
            f.write("1\n2\n3\n4\n5\n")
        r = asyncio.run(ReadTool().execute(file_path=p, workspace_dir=ws, offset=2, limit=2))
        assert r.success
        lines = r.content.strip().split("\n")
        assert len(lines) == 2

    def test_read_nonexistent(self, ws):
        r = asyncio.run(ReadTool().execute(file_path=os.path.join(ws, "nope.txt"), workspace_dir=ws))
        assert not r.success

    def test_read_escapes_workspace(self, ws):
        r = asyncio.run(ReadTool().execute(file_path="/etc/passwd", workspace_dir=ws))
        assert not r.success


from core.tools.write import WriteTool


class TestWriteTool:
    def test_write_new_file(self, ws):
        p = os.path.join(ws, "new.txt")
        r = asyncio.run(WriteTool().execute(file_path=p, content="hello world", workspace_dir=ws))
        assert r.success
        assert os.path.exists(p)
        with open(p) as f:
            assert f.read() == "hello world"

    def test_write_overwrites(self, ws):
        p = os.path.join(ws, "f.txt")
        with open(p, "w") as f:
            f.write("old")
        r = asyncio.run(WriteTool().execute(file_path=p, content="new", workspace_dir=ws))
        assert r.success
        with open(p) as f:
            assert f.read() == "new"

    def test_write_escapes_workspace(self, ws):
        r = asyncio.run(WriteTool().execute(file_path="/etc/hacked", content="x", workspace_dir=ws))
        assert not r.success
