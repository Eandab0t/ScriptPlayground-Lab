"""Vendor DiscordEmbeder's build into the playground and inject the bridge.

    python scripts/vendor_embeder.py /path/to/DiscordEmbeder

Copies dist/index.html -> embeder/index.html and appends the bridge fragment
before </body>. Build the upstream checkout first and pass its path explicitly.
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _source_commit(source: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip()


def vendor(source: Path) -> None:
    dist_html = source / "dist" / "index.html"
    if not dist_html.exists():
        sys.exit(f"no build found at {dist_html} — run `npm run build` in {source}")
    text = dist_html.read_text(encoding="utf-8")
    fragment = (ROOT / "embeder" / "bridge-inject.fragment.html").read_text(encoding="utf-8")
    if "</body>" not in text:
        sys.exit("upstream index.html has no </body> — layout changed?")
    text = text.replace("</body>", fragment + "</body>", 1)
    out = ROOT / "embeder" / "index.html"
    out.write_text(text, encoding="utf-8")
    marker = ROOT / "embeder" / "VENDORED_FROM.txt"
    marker.write_text(
        f"source: {source}\n"
        f"commit: {_source_commit(source)}\n"
        f"vended_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )
    print(f"vendored {dist_html} -> {out} ({len(text):,} bytes, bridge injected)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="path to the DiscordEmbeder checkout")
    vendor(parser.parse_args().source)
