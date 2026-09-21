"""Vendor DiscordEmbeder's build into the playground and inject the bridge.

    python scripts/vendor_embeder.py [source]

Copies dist/index.html -> embeder/index.html and appends the bridge fragment
before </body>. The upstream repo (E:/.../DiscordEmbeder) stays authoritative;
run this again after rebuilding upstream to refresh.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEFAULT_SOURCE = Path(r"E:/Ean server assests/Ean Applications/DiscordEmbeder")


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
    print(f"vendored {dist_html} -> {out} ({len(text):,} bytes, bridge injected)")


if __name__ == "__main__":
    vendor(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE)
