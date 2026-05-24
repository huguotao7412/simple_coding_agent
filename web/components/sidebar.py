from __future__ import annotations

import streamlit as st
from pathlib import Path

EXCLUDE_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".idea", ".pytest_cache"}


def render_sidebar(workspace_root: str, current_project: str) -> str | None:
    """Render sidebar: project switcher + file tree. Returns selected file path."""

    st.sidebar.title("SCA Web")

    # --- Project switcher ---
    root = Path(workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    projects = sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
    if not projects:
        projects = [current_project]
        (root / current_project).mkdir(exist_ok=True)

    selected_project = st.sidebar.selectbox(
        "项目",
        options=projects,
        index=projects.index(current_project) if current_project in projects else 0,
        key="project_selector",
    )

    # --- File tree ---
    st.sidebar.divider()
    st.sidebar.subheader("文件")
    project_dir = root / current_project
    return _render_file_tree(project_dir)


def _render_file_tree(project_dir: Path) -> str | None:
    """Recursively render file tree grouped by top-level dir. Returns selected file path."""
    if not project_dir.exists():
        st.sidebar.info("项目目录不存在")
        return None

    all_files = sorted(
        [
            f for f in project_dir.rglob("*")
            if f.is_file() and not (set(f.parts) & EXCLUDE_DIRS)
        ],
        key=lambda f: (f.suffix != ".py", str(f)),
    )

    if not all_files:
        st.sidebar.info("目录为空")
        return None

    groups: dict[str, list[Path]] = {}
    for f in all_files:
        rel = f.relative_to(project_dir)
        group = str(rel.parts[0]) if len(rel.parts) > 1 else "(根目录)"
        groups.setdefault(group, []).append(f)

    selected_file: str | None = None
    for group_name, files in sorted(groups.items()):
        with st.sidebar.expander(group_name, expanded=len(groups) <= 3):
            for f in files:
                rel_path = str(f.relative_to(project_dir))
                if st.button(
                    rel_path,
                    key=f"file_{rel_path}",
                    use_container_width=True,
                ):
                    selected_file = str(f)

    return selected_file
