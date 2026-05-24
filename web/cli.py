"""sca-web CLI entry point. Invokes streamlit run on web/main.py."""

from __future__ import annotations

import sys
from pathlib import Path


def main():
    from streamlit.web import cli as stcli

    app_path = Path(__file__).parent / "main.py"
    sys.argv = ["streamlit", "run", str(app_path)] + sys.argv[1:]
    sys.exit(stcli.main())
