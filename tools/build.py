"""Build the page from books*.json, links.json, holdings.json and template.html (all in this folder).

  python3 build.py                      -> index.html here (Claude artifact version, no <html> wrapper)
  python3 build.py --wrap --out FILE    -> a full HTML document for GitHub Pages
  --built YYYY-MM-DD sets the "整理於" date (default: meta.json's "built", else today).

The same script runs in GitHub Actions from the site repo's tools/ folder (see deploy.py).
"""
import argparse
import datetime
import json
import pathlib

here = pathlib.Path(__file__).parent
ap = argparse.ArgumentParser()
ap.add_argument("--out", default=str(here / "index.html"))
ap.add_argument("--wrap", action="store_true", help="emit a full document with doctype, meta and noindex")
ap.add_argument("--built")
args = ap.parse_args()

books = []
for name in ("books.json", "books-2023-2024.json", "books-0-3.json"):
    if (here / name).exists():
        books += json.loads((here / name).read_text())["books"]
links_path = here / "links.json"
links = {k: v for k, v in json.loads(links_path.read_text()).items() if v} if links_path.exists() else {}
hold_path = here / "holdings.json"
hold = json.loads(hold_path.read_text()) if hold_path.exists() else {}
meta_path = here / "meta.json"
meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
built = args.built or meta.get("built") or datetime.date.today().isoformat()

html = (here / "template.html").read_text()
html = html.replace("__DATA__", json.dumps({"books": books}, ensure_ascii=False))
html = html.replace("__HOLD__", json.dumps({"items": hold.get("items")} if hold else {}, ensure_ascii=False))
html = html.replace("__LINKS__", json.dumps(links))
html = html.replace("__HOLD_DATE__", (hold.get("generated") or "")[:10])
html = html.replace("__BUILT__", built)
if args.wrap:
    html = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow">\n' + html + "\n</html>")
pathlib.Path(args.out).write_text(html)
print(f"{args.out}: {len(books)} entries, holdings for {len(hold.get('items') or {})} ISBNs ({(hold.get('generated') or '')[:16]})")
