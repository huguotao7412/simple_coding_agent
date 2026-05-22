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


from core.tools.edit import EditTool


class TestEditTool:
    def test_search_replace_single(self, ws):
        p = os.path.join(ws, "f.py")
        with open(p, "w") as f:
            f.write("def foo():\n    pass\n")
        r = asyncio.run(EditTool().execute(file_path=p, old_string="pass", new_string="return 1", workspace_dir=ws))
        assert r.success
        with open(p) as f:
            assert "return 1" in f.read()

    def test_search_replace_multiple_without_flag_fails(self, ws):
        p = os.path.join(ws, "f.py")
        with open(p, "w") as f:
            f.write("x = 1\nx = 2\n")
        r = asyncio.run(EditTool().execute(file_path=p, old_string="x =", new_string="y =", workspace_dir=ws))
        assert not r.success

    def test_search_replace_all(self, ws):
        p = os.path.join(ws, "f.py")
        with open(p, "w") as f:
            f.write("x = 1\nx = 2\n")
        r = asyncio.run(EditTool().execute(file_path=p, old_string="x =", new_string="y =", replace_all=True, workspace_dir=ws))
        assert r.success
        with open(p) as f:
            content = f.read()
            assert "x =" not in content
            assert content.count("y =") == 2

    def test_line_range_replace(self, ws):
        p = os.path.join(ws, "f.py")
        with open(p, "w") as f:
            f.write("line0\nline1\nline2\nline3\n")
        r = asyncio.run(EditTool().execute(file_path=p, start_line=1, end_line=2, new_string="replaced\n", workspace_dir=ws))
        assert r.success
        with open(p) as f:
            lines = f.readlines()
            assert lines[0] == "line0\n"
            assert lines[1] == "replaced\n"
            assert lines[2] == "line3\n"

    def test_edit_escapes_workspace(self, ws):
        r = asyncio.run(EditTool().execute(file_path="/etc/hosts", old_string="x", new_string="y", workspace_dir=ws))
        assert not r.success
