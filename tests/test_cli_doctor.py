from __future__ import annotations

from cli.main import main


def test_doctor_is_read_only_and_reports_baseline(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SCA_CONFIG_HOME", str(tmp_path / "config"))
    before = set(tmp_path.iterdir())

    exit_code = main(["--dir", str(tmp_path), "doctor"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Supported MCP: mcp<2,>=1.28" in output or "Supported MCP: mcp>=1.28,<2" in output
    assert "Local baseline tools: healthy" in output
    assert "Optional MCP servers: not started" in output
    assert set(tmp_path.iterdir()) == before
