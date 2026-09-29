#!/usr/bin/env python3
"""
Stamp a cache-busting ?v=<version> on every local <script src="…"> / <link href="…">
in site/select.html (data/*.js, js/*.js, css/*.css - never the Google
Fonts <link>, which is external and already versioned by Google).

  python3 tools/site-version.py                        # version = current time (YYYYMMDDHHMM)
  python3 tools/site-version.py --version 202609211530  # explicit version (called this way
                                                          # from plan-floor-assemble.py --all,
                                                          # so the HTML tag matches the assets'
                                                          # own ASSET_VER for that run)

Idempotent: re-running replaces an existing ?v=… instead of appending a second one.
"""
import argparse, re, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig

ROOT = fpconfig.WORK
SELECT_HTML = ROOT / "site" / "select.html"
LOCAL_PREFIXES = ("data/", "js/", "css/")

ATTR_RE = re.compile(r'(src|href)="((?:%s)[^"?]+)(?:\?v=[^"]*)?"' % "|".join(re.escape(p) for p in LOCAL_PREFIXES))


def stamp(html, version):
    n = 0
    def repl(m):
        nonlocal n
        n += 1
        return f'{m.group(1)}="{m.group(2)}?v={version}"'
    return ATTR_RE.sub(repl, html), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", type=str, default=None, help="cache-buster string; default = current time (YYYYMMDDHHMM)")
    ap.add_argument("--file", type=str, default=None, help="override target HTML file (default: site/select.html)")
    args = ap.parse_args()
    version = args.version or time.strftime("%Y%m%d%H%M")
    path = Path(args.file) if args.file else SELECT_HTML

    html = path.read_text(encoding="utf-8")
    stamped, n = stamp(html, version)
    path.write_text(stamped, encoding="utf-8")
    print(f"{path}: проставлена версия {version} на {n} тегов (data/js/css)")


if __name__ == "__main__":
    main()
