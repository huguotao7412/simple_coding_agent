from __future__ import annotations

import difflib


def render_diff_html(old_text: str, new_text: str, file_path: str) -> str:
    """Return HTML string with colored unified diff between old_text and new_text.

    Green background for additions (+), red for deletions (-),
    blue for hunk headers (@@). Truncated to 200 diff lines max.
    """
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=file_path,
            tofile=file_path,
            lineterm="",
        )
    )

    colored: list[str] = []
    for line in diff_lines[:200]:
        escaped = _escape_html(line)
        if line.startswith("+"):
            colored.append(
                f'<span style="background:#1a3a1a;display:block">{escaped}</span>'
            )
        elif line.startswith("-"):
            colored.append(
                f'<span style="background:#3a1a1a;display:block">{escaped}</span>'
            )
        elif line.startswith("@@"):
            colored.append(
                f'<span style="color:#58a6ff;display:block">{escaped}</span>'
            )
        else:
            colored.append(escaped)

    return "".join(colored)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
