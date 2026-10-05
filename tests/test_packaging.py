"""Packaging: the frozen server binary has to be able to become a bot worker.

Workers are spawned with ``sys.executable`` (bot_worker.py). In a PyInstaller
bundle that is the server executable itself, so the packaged desktop app used
to hand the worker argv to the server, argparse rejected it, and every bot
died during boot with "worker exited during boot" -- invisible to the dev walk
and to the whole Python suite, which only ever run unfrozen.

These tests pin the dispatch in main._run_frozen_worker().
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main

APP_DIR = Path(__file__).resolve().parent.parent


def _worker_script(tmp_path: Path) -> Path:
    """A stand-in worker that records the argv it was handed."""
    script = tmp_path / "bot_worker.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(tmp_path / 'argv.json')!r}, 'w').write(json.dumps(sys.argv))\n"
    )
    return script


def test_unfrozen_server_never_re_dispatches(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    script = _worker_script(tmp_path)
    monkeypatch.setattr(sys, "argv", ["server", "-X", "utf8", str(script), "sb", "tag", "dsp"])

    assert main._run_frozen_worker() is False
    assert not (tmp_path / "argv.json").exists(), "dev mode must not run the worker inline"


def test_frozen_server_runs_the_worker_spawn(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    script = _worker_script(tmp_path)
    monkeypatch.setattr(sys, "argv", ["server", "-X", "utf8", str(script), "sb", "tag", "dsp"])

    assert main._run_frozen_worker() is True
    # bot_worker.main() reads argv[1:] as sandbox/tag/display, so the exe name
    # and interpreter flags must be gone but the three worker arguments kept.
    assert json.loads((tmp_path / "argv.json").read_text()) == [str(script), "sb", "tag", "dsp"]


def test_frozen_server_still_starts_normally(monkeypatch, tmp_path):
    """Its own argv must reach argparse, not be mistaken for a worker spawn."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _worker_script(tmp_path)
    monkeypatch.setattr(sys, "argv", ["server", "--port", "8799", "--no-browser"])

    assert main._run_frozen_worker() is False
    assert not (tmp_path / "argv.json").exists()


def test_frozen_worker_spawn_needs_the_script_on_disk(monkeypatch, tmp_path):
    """A missing worker script must not swallow the spawn and hang the server."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", [
        "server", "-X", "utf8", str(tmp_path / "bot_worker.py"), "sb", "tag", "dsp"
    ])

    assert main._run_frozen_worker() is False


def test_worker_script_is_shipped_with_the_bundle():
    """The dispatch is useless unless the spec ships bot_worker.py as a file."""
    spec = (APP_DIR / "ScriptPlayground-server.spec").read_text(encoding="utf-8")
    assert '"bot_worker.py"' in spec, "spec must ship bot_worker.py as data"