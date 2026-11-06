#!/usr/bin/env python3
"""Look up Taoyuan Public Library (桃園市立圖書館) holdings for a list of ISBNs.

Usage:
    python3 holdings.py books.json [other_books.json ...] [--refresh] [--out holdings.json]

Each input is {"books": [{"isbn": ..., "title": ...}, ...]}. ISBNs are merged and de-duplicated.

How it works (public, no login):
  1. GET https://webpac.typl.gov.tw/search?...  -> session cookies + csrfToken from __NEXT_DATA__
  2. POST https://webpac.typl.gov.tw/api/HyLibWS/graphql  (header X-CSRF-Token)
       query search(SearchForm{searchField:["ISBN"], searchInput:[isbn]}) -> records; "sid" = marcId
       query getHoldBySearch(HoldForm{marcId, pageNo, limit})            -> one row per copy
  3. Record page: https://webpac.typl.gov.tw/bookDetail/{marcId}

Raw responses are cached in .cache/. Search results are always reused from cache;
holdings (which change daily) are reused too unless --refresh is given.
Requests are throttled to at most 1 per second.
"""
import argparse
import datetime as dt
import hashlib
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

BASE = "https://webpac.typl.gov.tw"
GQL = BASE + "/api/HyLibWS/graphql"
SEARCH_URL = BASE + "/search?searchField=ISBN&searchInput={isbn}"
RECORD_URL = BASE + "/bookDetail/{marc_id}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
AVAILABLE_STATUS = "書在館"
MIN_INTERVAL = 1.1  # seconds between network requests

Q_SEARCH = ("query search($searchForm:SearchForm){ search(Input:$searchForm){ "
            "list{ values{ ref{key value} } } } }")
Q_HOLD = ("query getHoldBySearch($HoldForm:HoldForm){ getHoldBySearch(Input:$HoldForm){ "
          "list{ values{ ref{key value} } } } }")


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.token = None
        self.last = 0.0
        self.requests = 0

    def _wait(self):
        delta = time.time() - self.last
        if delta < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - delta)
        self.last = time.time()
        self.requests += 1

    def init_session(self):
        self._wait()
        req = urllib.request.Request(SEARCH_URL.format(isbn="9786264060875"),
                                     headers={"User-Agent": UA})
        html = self.opener.open(req, timeout=30).read().decode("utf-8", "replace")
        m = re.search(r'"csrfToken":"([^"]+)"', html)
        if not m:
            raise RuntimeError("csrfToken not found in search page")
        self.token = m.group(1)

    def gql(self, query, variables, _retry=True):
        if not self.token:
            self.init_session()
        self._wait()
        body = json.dumps({"variables": variables, "query": query}).encode()
        req = urllib.request.Request(GQL, data=body, headers={
            "User-Agent": UA, "Content-Type": "application/json",
            "X-CSRF-Token": self.token, "Referer": BASE + "/search"})
        try:
            return json.loads(self.opener.open(req, timeout=30).read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 403 and _retry:  # session/CSRF expired
                self.token = None
                return self.gql(query, variables, _retry=False)
            raise


def cached(client, kind, key, query, variables, refresh=False):
    CACHE.mkdir(exist_ok=True)
    h = hashlib.sha1(key.encode()).hexdigest()[:12]
    path = CACHE / f"{kind}-{re.sub(r'[^0-9A-Za-z]', '_', key)[:40]}-{h}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    data = client.gql(query, variables)
    if data.get("errors"):
        raise RuntimeError(f"GraphQL error for {kind} {key}: {data['errors']}")
    path.write_text(json.dumps(data, ensure_ascii=False))
    return data


def rows(data, field):
    out = []
    node = (data.get("data") or {}).get(field) or {}
    for v in ((node.get("list") or {}).get("values") or []):
        out.append({r["key"]: r["value"] for r in v.get("ref", [])})
    return out


def search(client, field, value):
    data = cached(client, "search", f"{field}:{value}", Q_SEARCH, {"searchForm": {
        "searchField": [field], "searchInput": [value], "pageNo": 1, "limit": 20}})
    return rows(data, "search")


def isbn13_to_10(isbn):
    if len(isbn) != 13 or not isbn.startswith("978"):
        return None
    core = isbn[3:12]
    s = sum((10 - i) * int(c) for i, c in enumerate(core))
    check = (11 - s % 11) % 11
    return core + ("X" if check == 10 else str(check))


def norm_title(t):
    return re.sub(r"[\s\W_]+", "", (t or "")).lower()


def find_records(client, isbn, title):
    """Return (records, matched_by)."""
    recs = search(client, "ISBN", isbn)
    if recs:
        return recs, "isbn13"
    i10 = isbn13_to_10(isbn)
    if i10:
        recs = search(client, "ISBN", i10)
        if recs:
            return recs, "isbn10"
    if title:
        nt = norm_title(title)
        recs = [r for r in search(client, "FullText", title)
                if nt and norm_title(r.get("title")).startswith(nt)]
        if recs:
            return recs, "title"
    return [], None


def holdings(client, marc_id, refresh):
    data = cached(client, "hold", str(marc_id), Q_HOLD,
                  {"HoldForm": {"marcId": int(marc_id), "pageNo": 1, "limit": 500}},
                  refresh=refresh)
    return rows(data, "getHoldBySearch")


def load_books(paths):
    books = OrderedDict()
    for p in paths:
        for b in json.loads(Path(p).read_text()).get("books", []):
            isbn = re.sub(r"[^0-9Xx]", "", str(b.get("isbn") or "")).upper()
            if isbn and isbn not in books:
                books[isbn] = b.get("title")
    return books


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("books", nargs="+", help="books.json path(s); missing files are skipped")
    ap.add_argument("--out", default=str(HERE / "holdings.json"))
    ap.add_argument("--refresh", action="store_true", help="re-fetch holdings (status) instead of cache")
    args = ap.parse_args()

    paths = [p for p in args.books if Path(p).exists()]
    for p in set(args.books) - set(paths):
        print(f"skip (not found): {p}", file=sys.stderr)
    books = load_books(paths)
    client = Client()
    items = {}
    for n, (isbn, title) in enumerate(books.items(), 1):
        try:
            recs, how = find_records(client, isbn, title)
            branches = OrderedDict()
            marc_ids = []
            for r in recs:
                mid = r.get("sid")
                if not mid or mid in marc_ids:
                    continue
                marc_ids.append(mid)
                for h in holdings(client, mid, args.refresh):
                    name = h.get("keepSiteLabelName") or h.get("keepSiteName") or "?"
                    b = branches.setdefault(name, {"branch": name, "copies": 0, "available": 0,
                                                   "statuses": {}, "call_numbers": [], "due": []})
                    b["copies"] += 1
                    st = h.get("bookStatusLabelName") or ""
                    b["statuses"][st] = b["statuses"].get(st, 0) + 1
                    if st == AVAILABLE_STATUS:
                        b["available"] += 1
                    cn = (h.get("callNumber") or "").strip()
                    if cn and cn not in b["call_numbers"]:
                        b["call_numbers"].append(cn)
                    if h.get("statusExpire"):
                        b["due"].append(h["statusExpire"])
            for b in branches.values():
                b["due"].sort()
            bl = sorted(branches.values(), key=lambda b: (-b["copies"], b["branch"]))
            items[isbn] = {
                "title": title,
                "found": bool(marc_ids),
                "matched_by": how,
                "marc_ids": marc_ids,
                "record_url": RECORD_URL.format(marc_id=marc_ids[0]) if marc_ids else None,
                "search_url": SEARCH_URL.format(isbn=isbn),
                "branches": bl,
                "total": sum(b["copies"] for b in bl),
                "available": sum(b["available"] for b in bl),
            }
            print(f"[{n}/{len(books)}] {isbn} {how or 'NOT FOUND'} "
                  f"copies={items[isbn]['total']} branches={len(bl)}", file=sys.stderr)
        except Exception as e:  # keep going; record the error
            items[isbn] = {"title": title, "found": False, "error": str(e), "record_url": None,
                           "search_url": SEARCH_URL.format(isbn=isbn), "branches": [], "total": 0}
            print(f"[{n}/{len(books)}] {isbn} ERROR {e}", file=sys.stderr)

    out = {
        "generated": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds"),
        "source": "桃園市立圖書館 WebPAC (webpac.typl.gov.tw) GraphQL API; status is a snapshot",
        "catalog_search": SEARCH_URL.replace("{isbn}", "{isbn}"),
        "record_url_pattern": RECORD_URL.replace("{marc_id}", "{marcId}"),
        "inputs": paths,
        "items": items,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    found = sum(1 for v in items.values() if v.get("found"))
    print(f"done: {found}/{len(items)} found, {client.requests} network requests -> {args.out}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
