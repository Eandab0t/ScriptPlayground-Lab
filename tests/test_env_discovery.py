"""Offline tests for project .env configuration discovery."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import env_discovery


def test_parse_dotenv_basic(tmp_path):
    env = env_discovery.parse_dotenv(
        '# comment\nTOKEN = "abc123"\nexport PORT=8080\nDEBUG=true\nBAD LINE\n'
    )
    assert env == {"TOKEN": "abc123", "PORT": "8080", "DEBUG": "true"}


def test_secret_classification_and_redaction(tmp_path):
    root = tmp_path / "bot"
    root.mkdir()
    (root / ".env").write_text(
        "DISCORD_TOKEN=super-secret-value\nPORT=8169\nDATABASE_URL=postgres://x\n",
        encoding="utf-8",
    )
    report = env_discovery.discover(root)
    by_name = {v["name"]: v for v in report["variables"]}
    assert by_name["DISCORD_TOKEN"]["secret"] is True
    assert by_name["DISCORD_TOKEN"]["redacted"] is True
    assert "super-secret-value" not in str(report)  # value never leaves the file
    assert by_name["PORT"]["secret"] is False       # public hint prefix


def test_example_schema_reports_missing(tmp_path):
    root = tmp_path / "bot2"
    root.mkdir()
    (root / ".env.example").write_text("DISCORD_TOKEN=\nGEMINI_API_KEY=\nPORT=3000\n", encoding="utf-8")
    report = env_discovery.discover(root)
    by_name = {v["name"]: v for v in report["variables"]}
    assert by_name["DISCORD_TOKEN"]["present"] is False
    assert by_name["PORT"]["present"] is False  # example values are schema, not config
    assert set(report["missing"]) == {"DISCORD_TOKEN", "GEMINI_API_KEY", "PORT"}


def test_dotenv_overrides_example(tmp_path):
    root = tmp_path / "bot3"
    root.mkdir()
    (root / ".env.example").write_text("DISCORD_TOKEN=\n", encoding="utf-8")
    (root / ".env").write_text("DISCORD_TOKEN=real-one\n", encoding="utf-8")
    report = env_discovery.discover(root)
    entry = report["variables"][0]
    assert entry["present"] is True and entry["redacted"] is True
    assert report["missing"] == []
    assert "real-one" not in str(report)


def test_no_env_files(tmp_path):
    report = env_discovery.discover(tmp_path)
    assert report == {"files": [], "variables": [], "missing": [], "secret_count": 0}
