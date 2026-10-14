"""Local viewer for Startup Radar: http://127.0.0.1:8765/

Serves viewer.html and a small JSON API. User ratings, notes and read flags go to
data/feedback.json, which radar.py feeds back into the next run's ratings.
"""
import datetime as dt
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PORT = 8765


def load(name, default):
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(name, obj):
    path = DATA / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send(200, (ROOT / "viewer.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/data":
            payload = {
                "items": load("items.json", []),
                "feedback": load("feedback.json", {}),
                "usage": load("usage.json", []),
            }
            self.send(200, json.dumps(payload, ensure_ascii=False))
        else:
            self.send(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path != "/api/feedback":
            return self.send(404, '{"error":"not found"}')
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        iid = body.get("id")
        items = {it["id"]: it for it in load("items.json", [])}
        if iid not in items:
            return self.send(400, '{"error":"unknown id"}')
        fb = load("feedback.json", {})
        entry = fb.setdefault(iid, {})
        item = items[iid]
        # Keep the AI's original rating next to the user's so radar.py can learn the gap.
        entry.setdefault("ai_relevance", item.get("relevance"))
        entry.setdefault("ai_trend", item.get("trend"))
        entry["title"] = item.get("title_ja") or item.get("title")
        for key in ("relevance", "trend", "note", "read", "saved"):
            if key in body:
                entry[key] = body[key]
        entry["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        save("feedback.json", fb)
        self.send(200, json.dumps(entry, ensure_ascii=False))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
