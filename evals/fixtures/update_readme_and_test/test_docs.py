from pathlib import Path


def test_readme_documents_name_option():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "--name" in readme
    assert "python app.py --name Ada" in readme
