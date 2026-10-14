"""Startup Radar: collect new YC/a16z videos and startup news, then have Claude
triage and summarize them. Runs on the Claude subscription via `claude -p`
(never an API key).

Usage:
    python radar.py              # run now
    python radar.py --scheduled  # run only inside config.schedule.allowed_hours
"""
import argparse
import calendar
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import feedparser
import requests
import trafilatura
from youtube_transcript_api import YouTubeTranscriptApi

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
LIM = CFG["limits"]
CLAUDE = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StartupRadar/1.0"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CATEGORIES = ["funding", "tech", "interview", "analysis", "learning"]

LOG_FILE = None


def log(msg):
    line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- collection

def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    if not t:
        return dt.datetime.now(dt.timezone.utc)
    return dt.datetime.fromtimestamp(calendar.timegm(t), dt.timezone.utc)


def collect(seen):
    hours = LIM["first_run_hours"] if not seen else LIM["max_age_hours"]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    videos, articles = {}, {}
    for src in CFG["sources"]:
        try:
            feed = feedparser.parse(src["url"], request_headers=UA)
        except Exception as ex:  # one broken feed must not stop the run
            log(f"feed error {src['name']}: {ex}")
            continue
        for e in feed.entries:
            if src["kind"] == "video":
                iid = "yt:" + e.get("yt_videoid", e.link)
            else:
                iid = "web:" + e.link.split("?")[0].rstrip("/")
            if iid in seen or iid in videos or iid in articles:
                continue
            published = entry_time(e)
            if published < cutoff:
                seen[iid] = now_iso()
                continue
            item = {
                "id": iid,
                "kind": src["kind"],
                "source": src["name"],
                "url": e.link,
                "title": e.title,
                "published": published.isoformat(timespec="seconds"),
                "description": (e.get("summary") or "")[:3000],
            }
            if src["kind"] == "video":
                item["video_id"] = e.get("yt_videoid")
                item["thumbnail"] = f"https://i.ytimg.com/vi/{item['video_id']}/mqdefault.jpg"
                videos[iid] = item
            else:
                if e.get("content"):
                    item["rss_body"] = e.content[0].get("value", "")
                articles[iid] = item
    newest = lambda d: sorted(d.values(), key=lambda x: x["published"], reverse=True)
    return newest(videos), newest(articles)


def fetch_article_text(item):
    text = None
    try:
        r = requests.get(item["url"], headers=UA, timeout=30)
        if r.ok:
            text = trafilatura.extract(r.text)
    except Exception as ex:
        log(f"article fetch failed {item['url']}: {ex}")
    if not text and item.get("rss_body"):
        text = trafilatura.extract(item["rss_body"]) or item["rss_body"]
    text = text or item["description"]
    return text[: LIM["article_chars"]]


def fetch_transcript(video_id):
    """Return (text with [mm:ss] markers every ~60s, duration seconds)."""
    t = YouTubeTranscriptApi().fetch(video_id, languages=["en", "ja"])
    parts, next_mark = [], 0
    for s in t.snippets:
        if s.start >= next_mark:
            m, sec = divmod(int(s.start), 60)
            parts.append(f"\n[{m:02d}:{sec:02d}] ")
            next_mark = s.start + 60
        parts.append(s.text.replace("\n", " ") + " ")
    duration = t.snippets[-1].start + t.snippets[-1].duration if t.snippets else 0
    return "".join(parts).strip(), duration


# ---------------------------------------------------------------- claude

class Usage:
    def __init__(self):
        self.calls = []

    def add(self, label, res, seconds):
        # modelUsage covers every model the call touched, including the Haiku helper
        # Claude Code uses to run WebSearch; top-level `usage` covers only the main model.
        models = {}
        for name, m in (res.get("modelUsage") or {}).items():
            models[name] = {
                "input": m.get("inputTokens", 0),
                "cache_create": m.get("cacheCreationInputTokens", 0),
                "cache_read": m.get("cacheReadInputTokens", 0),
                "output": m.get("outputTokens", 0),
                "web_searches": m.get("webSearchRequests", 0),
            }
        call = {"label": label, "models": models}
        for k in ("input", "cache_create", "cache_read", "output", "web_searches"):
            call[k] = sum(m[k] for m in models.values())
        call.update(
            turns=res.get("num_turns", 0),
            api_equiv_usd=round(res.get("total_cost_usd", 0) or 0, 4),
            seconds=round(seconds, 1),
            ok=not res.get("is_error", True),
        )
        self.calls.append(call)


def system_prompt(task, feedback_block=""):
    prompt = (ROOT / "prompts" / f"{task}.md").read_text(encoding="utf-8")
    context = (ROOT / "context.md").read_text(encoding="utf-8")
    return f"{prompt}\n\n---\n\n{context}{feedback_block}"


def run_claude(usage, label, task, user_prompt, schema, model, tools="", effort=None, feedback_block=""):
    """Run one headless Claude call and return its structured output (or None)."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(system_prompt(task, feedback_block))
        sys_file = f.name
    cmd = [
        CLAUDE, "-p",
        "--model", model,
        "--tools", tools,
        "--strict-mcp-config",
        "--setting-sources", "",
        "--system-prompt-file", sys_file,
        "--no-session-persistence",
        "--output-format", "json",
        "--permission-prompts", "none",
        "--json-schema", json.dumps(schema),
    ]
    if tools:
        cmd += ["--allowedTools", tools]
    if effort:
        cmd += ["--effort", effort]
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        for attempt in (1, 2):
            start = time.time()
            p = subprocess.run(cmd, input=user_prompt, capture_output=True, text=True,
                               encoding="utf-8", cwd=str(DATA), env=env,
                               timeout=1200, creationflags=NO_WINDOW)
            try:
                res = json.loads(p.stdout)
            except json.JSONDecodeError:
                log(f"{label}: unparseable output (attempt {attempt}): {p.stdout[:300]} {p.stderr[:300]}")
                continue
            usage.add(label, res, time.time() - start)
            out = res.get("structured_output")
            if out and not res.get("is_error"):
                return out
            log(f"{label}: no structured output (attempt {attempt}): {str(res.get('result'))[:300]}")
        return None
    finally:
        os.unlink(sys_file)


IMPACT = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "premise": {"type": "string", "pattern": "^前提[0-9]+$"},
            "direction": {"type": "string", "enum": ["support", "contradict"]},
            "note": {"type": "string"},
        },
        "required": ["premise", "direction", "note"],
    },
}
COMMON = {
    "category": {"type": "string", "enum": CATEGORIES},
    "title_ja": {"type": "string"},
    "summary_ja": {"type": "string"},
    "relevance": {"type": "integer", "minimum": 1, "maximum": 5},
    "trend": {"type": "integer", "minimum": 1, "maximum": 3},
    "relevance_reason": {"type": "string"},
    "companies": {"type": "array", "items": {"type": "string"}},
    "watchlist_hit": {"type": "boolean"},
    "premise_impacts": IMPACT,
    "detail_md": {"type": "string"},
}
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, **COMMON},
        "required": ["id", *COMMON],
    }}},
    "required": ["items"],
}
VIDEO_SCHEMA = {
    "type": "object",
    "properties": {**COMMON, "key_moments": {"type": "array", "items": {
        "type": "object",
        "properties": {"t": {"type": "string"}, "seconds": {"type": "integer"}, "point": {"type": "string"}},
        "required": ["t", "seconds", "point"],
    }}},
    "required": [*COMMON, "key_moments"],
}
DEEP_KEYS = ["relevance", "relevance_reason", "summary_ja", "companies", "premise_impacts", "detail_md"]
DEEP_SCHEMA = {
    "type": "object",
    "properties": {k: COMMON[k] for k in DEEP_KEYS},
    "required": DEEP_KEYS,
}


def feedback_block(items_by_id):
    """User corrections, fed back so future ratings follow the user's judgment."""
    fb = load_json(DATA / "feedback.json", {})
    rows = []
    for iid, f in sorted(fb.items(), key=lambda kv: kv[1].get("updated", ""), reverse=True):
        changed = ("relevance" in f and f["relevance"] != f.get("ai_relevance")) or \
                  ("trend" in f and f["trend"] != f.get("ai_trend"))
        if not changed and not f.get("note"):
            continue
        title = f.get("title") or items_by_id.get(iid, {}).get("title_ja", iid)
        line = f"- 「{title}」: AIの評価 ★{f.get('ai_relevance')}🔥{f.get('ai_trend')}"
        if changed:
            line += f" → ユーザーの修正 ★{f.get('relevance', f.get('ai_relevance'))}🔥{f.get('trend', f.get('ai_trend'))}"
        if f.get("note"):
            line += f"／メモ: {f['note']}"
        rows.append(line)
        if len(rows) >= LIM["feedback_examples"]:
            break
    if not rows:
        return ""
    return ("\n\n# ユーザーによる評価の修正例\n\n読み手が過去にAIの評価を修正した例です。"
            "この傾向に合わせて★と🔥をつけてください。\n\n" + "\n".join(rows) + "\n")


# ---------------------------------------------------------------- pipeline

def base_record(item, run_id):
    rec = {k: item[k] for k in ("id", "kind", "source", "url", "title", "published")}
    if item.get("thumbnail"):
        rec["thumbnail"] = item["thumbnail"]
    rec.update(run_id=run_id, added=now_iso(), deep=False, key_moments=[])
    return rec


def triage(usage, articles, run_id, fb_block):
    out = []
    size = LIM["triage_batch_size"]
    for i in range(0, len(articles), size):
        batch = articles[i:i + size]
        blocks = []
        for a in batch:
            blocks.append(f"<article id=\"{a['id']}\">\nsource: {a['source']}\ntitle: {a['title']}\n"
                          f"published: {a['published']}\n\n{a['text']}\n</article>")
        prompt = f"次の{len(batch)}件の記事を評価してください。\n\n" + "\n\n".join(blocks)
        res = run_claude(usage, f"triage {i // size + 1}", "triage", prompt, TRIAGE_SCHEMA,
                         CFG["models"]["triage"], feedback_block=fb_block)
        if not res:
            continue
        by_id = {r["id"]: r for r in res["items"]}
        for a in batch:
            r = by_id.get(a["id"])
            if not r:
                log(f"triage skipped {a['id']}")
                continue
            rec = base_record(a, run_id)
            rec.update({k: r[k] for k in COMMON})
            rec["article_text"] = a["text"]
            out.append(rec)
    return out


def summarize_video(usage, v, run_id, fb_block):
    prompt = (f"channel: {v['source']}\ntitle: {v['title']}\npublished: {v['published']}\n"
              f"description:\n{v['description']}\n\ntranscript:\n{v['transcript']}")
    res = run_claude(usage, f"video {v['video_id']}", "video", prompt, VIDEO_SCHEMA,
                     CFG["models"]["video"], effort=CFG["effort"].get("video"), feedback_block=fb_block)
    if not res:
        return None
    rec = base_record(v, run_id)
    rec.update({k: res[k] for k in COMMON})
    rec["key_moments"] = res.get("key_moments", [])
    rec["duration"] = int(v["duration"])
    return rec


def deep_dive(usage, rec):
    prompt = (f"初期判定: ★{rec['relevance']} 🔥{rec['trend']}（{rec['relevance_reason']}）\n"
              f"title: {rec['title']}\nsource: {rec['source']}\nurl: {rec['url']}\n"
              f"published: {rec['published']}\n\n本文:\n{rec.get('article_text', '')}")
    res = run_claude(usage, f"deep {rec['id'][-40:]}", "deep", prompt, DEEP_SCHEMA,
                     CFG["models"]["deep"], tools="WebSearch,WebFetch", effort=CFG["effort"].get("deep"))
    if res:
        rec.update({k: res[k] for k in DEEP_KEYS})
        rec["deep"] = True


def run():
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    started = time.time()
    usage = Usage()
    seen = load_json(DATA / "seen.json", {})
    items = load_json(DATA / "items.json", [])
    items_by_id = {it["id"]: it for it in items}
    fb_block = feedback_block(items_by_id)

    videos, articles = collect(seen)
    log(f"new: {len(videos)} videos, {len(articles)} articles")
    videos = videos[: LIM["max_videos_per_run"]]
    articles = articles[: LIM["max_articles_per_run"]]

    # Videos: transcripts, skipping Shorts. A video whose transcript fails falls back
    # to triage on its description so it still shows up.
    ready_videos = []
    for v in videos:
        try:
            v["transcript"], v["duration"] = fetch_transcript(v["video_id"])
        except Exception as ex:
            log(f"transcript failed {v['video_id']}: {type(ex).__name__}")
            v["text"] = "（字幕を取得できなかったため、動画の説明文のみ）\n" + v["description"]
            articles.append(v)
            continue
        if v["duration"] < LIM["min_video_seconds"]:
            log(f"skip short {v['video_id']} ({int(v['duration'])}s)")
            seen[v["id"]] = now_iso()
            continue
        ready_videos.append(v)

    for a in articles:
        if "text" not in a:
            a["text"] = fetch_article_text(a)

    new_records = triage(usage, articles, run_id, fb_block)
    for v in ready_videos:
        rec = summarize_video(usage, v, run_id, fb_block)
        if rec:
            new_records.append(rec)

    # Deep analysis for ★4-5 articles, watchlist hits first.
    candidates = [r for r in new_records if r["kind"] == "article" and r["relevance"] >= 4]
    candidates.sort(key=lambda r: (not r["watchlist_hit"], -r["relevance"], -r["trend"]))
    for rec in candidates[: LIM["max_deep_per_run"]]:
        deep_dive(usage, rec)

    for rec in new_records:
        rec.pop("article_text", None)
        items_by_id[rec["id"]] = rec
        seen[rec["id"]] = now_iso()
    save_json(DATA / "items.json", sorted(items_by_id.values(), key=lambda r: r["published"], reverse=True))
    save_json(DATA / "seen.json", seen)

    calls = usage.calls
    total = lambda k: sum(c[k] for c in calls)
    run_rec = {
        "run_id": run_id,
        "started": dt.datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "seconds": round(time.time() - started),
        "counts": {
            "articles": sum(r["kind"] == "article" for r in new_records),
            "videos": sum(r["kind"] == "video" for r in new_records),
            "deep": sum(r["deep"] for r in new_records),
        },
        "totals": {k: total(k) for k in ("input", "cache_create", "cache_read", "output", "web_searches", "api_equiv_usd")},
        "by_model": {},
        "calls": calls,
        "failed_calls": sum(not c["ok"] for c in calls),
    }
    for c in calls:
        for name, mu in c["models"].items():
            m = run_rec["by_model"].setdefault(name, {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0})
            for k in m:
                m[k] += mu[k]
    runs = load_json(DATA / "usage.json", [])
    runs.append(run_rec)
    save_json(DATA / "usage.json", runs[-200:])
    t = run_rec["totals"]
    log(f"done: {run_rec['counts']} | input {t['input']:,} cache_create {t['cache_create']:,} "
        f"cache_read {t['cache_read']:,} output {t['output']:,} | {run_rec['seconds']}s")


def main():
    global LOG_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--scheduled", action="store_true")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    LOG_FILE = LOGS / f"run-{dt.datetime.now():%Y%m%d}.log"

    if args.scheduled:
        lo, hi = CFG["schedule"]["allowed_hours"]
        if not (lo <= dt.datetime.now().hour < hi):
            log("outside allowed hours; skipped so the daytime usage window is not consumed")
            return

    lock = DATA / ".lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 3 * 3600:
        log("another run is in progress; exiting")
        return
    lock.write_text(str(os.getpid()))
    try:
        run()
    except Exception:
        log("run failed:\n" + traceback.format_exc())
        raise
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
