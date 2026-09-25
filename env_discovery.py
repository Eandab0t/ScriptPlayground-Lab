"""Project configuration discovery: read a bot project's dotenv files.

ScriptPlayground inspects `.env`-style files to understand what configuration a
bot project *expects* before running it. Values are never shown for
secret-looking variables; only names, presence, and redaction state reach the
UI. `.env.example` documents the expected schema; `.env` adds live values.
"""
from __future__ import annotations

import re
from pathlib import Path

DOTENV_NAMES = (".env", ".env.example", ".env.local", ".env.development", ".env.production")

_SECRET_PATTERNS = (
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"passphrase", re.IGNORECASE),
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"private[_-]?key", re.IGNORECASE),
    re.compile(r"client[_-]?secret", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
)

# Variables whose values are fine to display: connection targets, ports, flags.
_PUBLIC_HINTS = re.compile(
    r"^(?:PORT|HOST|HTTP_|API_|BASE_URI|BASE_URL|DATABASE_HOST|REDIS_HOST|"
    r"LAVALINK_|DEBUG|TESTMODE|LOG_LEVEL|NODE_ENV|FEATURE_)", re.IGNORECASE
)


def is_secret_name(name: str) -> bool:
    """True when a variable name looks like it carries a credential."""
    if _PUBLIC_HINTS.match(name):
        return False
    return any(pattern.search(name) for pattern in _SECRET_PATTERNS)


_LINE_RE = re.compile(
    r"""^\s*
        (?:export\s+)?
        ([A-Za-z_][A-Za-z0-9_.]*)      # name
        \s*[:=]\s*
        (['\"]?)                       # optional quote
        (.*?)
        \2                             # matching quote
        \s*(?:\#.*)?                   # trailing comment
        $""",
    re.VERBOSE,
)


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse dotenv-style text into a name→raw-value dict (comments ignored)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match is None:
            continue
        out[match.group(1)] = match.group(3)
    return out


def _classify(name: str, present: bool) -> dict:
    return {
        "name": name,
        "present": present,
        "secret": is_secret_name(name),
        "redacted": is_secret_name(name) and present,
    }


def discover(workspace: Path) -> dict:
    """Scan dotenv files and return the configuration-discovery report.

    The report carries variable names and presence only — never secret values.
    """
    workspace = Path(workspace)
    files: list[dict] = []
    names: dict[str, dict] = {}
    for file_name in DOTENV_NAMES:
        path = workspace / file_name
        if not path.is_file():
            continue
        try:
            entries = parse_dotenv(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        # .env.local / .env.development / .env.production override .env; .env
        # overrides .env.example. Keep the most authoritative sighting.
        authority = {".env.example": 0, ".env.production": 1, ".env.development": 1,
                     ".env.local": 2, ".env": 3}
        for name in entries:
            seen = names.get(name)
            if seen is None or authority[file_name] > seen["authority"]:
                names[name] = {
                    "authority": authority[file_name],
                    "source": file_name,
                    "present": file_name != ".env.example",
                }
        files.append({
            "file": file_name,
            "variables": len(entries),
            "path": str(path),
        })

    variables = [_classify(name, info["present"]) for name, info in sorted(names.items())]
    required = sorted(entry["name"] for entry in variables
                      if not entry["present"])  # schema entries from .env.example only
    return {
        "files": files,
        "variables": variables,
        "missing": required,
        "secret_count": sum(1 for entry in variables if entry["redacted"]),
    }


def placeholder_env(runtime_kind: str = "python") -> dict[str, str]:
    """Values ScriptPlayground provides automatically for local simulation."""
    return {
        "DISCORD_TOKEN": "offline-simulated-token",
        "BOT_TOKEN": "offline-simulated-token",
        "DISCORD_BOT_TOKEN": "offline-simulated-token",
        "TOKEN": "offline-simulated-token",
    }
