from __future__ import annotations

import pytest
from web.components.diff import render_diff_html


class TestRenderDiffHtml:
    def test_returns_no_colors_for_identical_texts(self):
        result = render_diff_html("abc", "abc", "file.py")
        assert "background:#1a3a1a" not in result
        assert "background:#3a1a1a" not in result

    def test_shows_added_line_in_green(self):
        result = render_diff_html("line1", "line1\nline2", "file.py")
        assert "background:#1a3a1a" in result
        assert "line2" in result

    def test_shows_removed_line_in_red(self):
        result = render_diff_html("line1\nline2", "line1", "file.py")
        assert "background:#3a1a1a" in result

    def test_escapes_html(self):
        result = render_diff_html("<script>", "<p>safe</p>", "x.html")
        assert "&lt;script&gt;" in result
        assert "&lt;p&gt;safe&lt;/p&gt;" in result

    def test_truncates_long_diff(self):
        lines = [f"line{i}" for i in range(300)]
        old = "\n".join(lines[:150])
        new = "\n".join(lines[150:])
        result = render_diff_html(old, new, "big.py")
        output_lines = result.split("\n")
        assert len(output_lines) < 250

    def test_shows_hunk_header_in_blue(self):
        result = render_diff_html("old", "new", "f.py")
        if "@@" in result:
            assert "color:#58a6ff" in result
