#!/usr/bin/env python3
"""The site's public, read-only search over the jokes that have a GIF (used by the Slack command
/cleopatre, in moniprinter's slackmux). Behind nginx at /api/search:

    GET /api/search?q=<words>&limit=<n, 20 by default, 50 at most>
    -> {"query": "...", "results": [{"id": 147, "text": "...", "character": "César", "tags": [...],
        "timecode": 2446, "gif": "https://…/gif/147.gif", "url": "https://…/quotes/147.html"}]}

Best first, by words, and by meaning (tools/gif_search.py) for callers that send the shared key
in an X-Search-Key header (SEARCH_MODEL_KEY, in search.env on the server): the search by meaning
asks a model on OpenRouter, paid per call. Even then it is capped at MODEL_DAILY_LIMIT calls a day
(repeated queries are cached); past that, or without the key, the words alone answer.
An empty query gives no results; "by_meaning" says whether the model could be asked.

Usage: python3 tools/search_api.py --port 8769   (from the repo root)
"""
import argparse
import datetime
import hmac
import http.server
import json
import os
import sys
import threading
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gif_search  # noqa: E402

SITE = "https://domusquietusdomus.douady.paris"
MODEL_DAILY_LIMIT = int(os.environ.get("SEARCH_MODEL_DAILY_LIMIT", "300"))
# Tags that describe the kind of joke (as in prepare.py); the others are characters
CATEGORY_TAGS = {"référence", "visuel", "audio", "cliché", "historique", "astérixme", "latin", "voix-off"}
catalog = gif_search.Catalog()
budget = {"day": None, "used": 0}
budget_lock = threading.Lock()


def model_allowed():
    with budget_lock:
        today = datetime.date.today()
        if budget["day"] != today:
            budget.update(day=today, used=0)
        if budget["used"] >= MODEL_DAILY_LIMIT:
            return False
        budget["used"] += 1
        return True


def has_key(headers):
    """Whether the caller may use the model: the shared key, compared in constant time."""
    key = os.environ.get("SEARCH_MODEL_KEY", "")
    return bool(key) and hmac.compare_digest(headers.get("X-Search-Key", "").encode(), key.encode())


def result(joke_id):
    d = catalog.by_id[joke_id]
    characters = [t for t in d["tags"] if t.lower() not in CATEGORY_TAGS]
    return {"id": int(joke_id) if joke_id.isdigit() else joke_id, "text": d["title"],
            "character": characters[0] if characters else "", "tags": d["tags"], "timecode": d["timecode"],
            "gif": "%s/gif/%s.gif" % (SITE, joke_id), "url": "%s/quotes/%s.html" % (SITE, joke_id)}


class Handler(http.server.BaseHTTPRequestHandler):
    def reply(self, status, data, private=False):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # A keyed answer must not be served to others from a shared cache
        self.send_header("Cache-Control", "private, max-age=300" if private else "public, max-age=300")
        self.send_header("Vary", "X-Search-Key")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/search":
            return self.reply(404, {"error": "not found"})
        query = urllib.parse.parse_qs(url.query)
        q = " ".join(query.get("q", [""])[0].split())[:200]
        try:
            limit = max(1, min(50, int(query.get("limit", ["20"])[0])))
        except ValueError:
            return self.reply(400, {"error": "limit must be a number"})
        keyed = has_key(self.headers)
        try:
            ids = catalog.search(q, limit, use_model=model_allowed if keyed else False, random_when_empty=False)
        except Exception as e:
            print("search failed:", e, file=sys.stderr, flush=True)
            return self.reply(500, {"error": "search failed"})
        self.reply(200, {"query": q, "by_meaning": keyed, "results": [result(i) for i in ids if i in catalog.by_id]},
                   private=keyed)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    catalog.load()
    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
