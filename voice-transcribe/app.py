"""ボイスメモ文字起こしサーバー。PC からも iPhone の Safari からも同じ画面を使える。"""
import queue
import re
import shutil
import socket
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import transcribe
from audio import split_audio
from config import (BASE_DIR, CHUNK_SEC, GEMINI_API_KEY, GEMINI_MODEL,
                    OUTPUT_DIR, PORT, UPLOAD_DIR, WORK_DIR)

app = FastAPI(title="ボイスメモ文字起こし")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_QUEUE: "queue.Queue[str]" = queue.Queue()

ALLOWED_SUFFIXES = {".m4a", ".mp3", ".wav", ".aac", ".mp4", ".mov", ".caf",
                    ".flac", ".ogg", ".opus", ".webm", ".aiff", ".aif", ".wma"}


def update_job(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def process_job(job_id: str) -> None:
    with JOBS_LOCK:
        job = dict(JOBS[job_id])

    src = Path(job["source_path"])
    work_dir = WORK_DIR / job_id

    try:
        update_job(job_id, status="splitting", message="Analyzing the audio\u2026")
        chunks = split_audio(src, work_dir, CHUNK_SEC)
        total = len(chunks)
        update_job(job_id, total_chunks=total,
                   message=f"Split into {total} part(s). Starting transcription\u2026")

        raw_parts: list[str] = []
        previous_tail = ""
        for i, (chunk_path, start, end) in enumerate(chunks):
            update_job(job_id, status="transcribing", done_chunks=i,
                       message=f"Transcribing\u2026 (part {i + 1} of {total})")
            text = transcribe.transcribe_chunk(
                chunk_path,
                glossary=job["glossary"],
                previous_tail=previous_tail,
                remove_fillers=job["remove_fillers"],
                index=i, total=total,
            )
            raw_parts.append(text)
            previous_tail = text[-300:]

        raw_text = "\n\n".join(p for p in raw_parts if p.strip())
        update_job(job_id, done_chunks=total, raw_text=raw_text)

        final_text = raw_text
        warning = None
        if job["polish"] and raw_text.strip():
            update_job(job_id, status="polishing", message="Claude is proofreading\u2026")
            try:
                final_text = transcribe.polish(raw_text, job["glossary"])
            except Exception as e:
                warning = f"Proofreading was skipped ({e}). Showing the raw transcript."
                final_text = raw_text

        stem = Path(job["filename"]).stem
        safe_stem = "".join(c for c in stem if c not in set(chr(92) + '/:*?"<>|')).strip() or "transcript"
        out_path = OUTPUT_DIR / f"{safe_stem}_{job_id[:8]}.txt"
        out_path.write_text(final_text, encoding="utf-8")

        update_job(job_id, status="done", text=final_text, warning=warning,
                   output_path=str(out_path), chars=len(final_text),
                   message="Done.", finished_at=datetime.now().isoformat(timespec="seconds"))

    except Exception as e:
        update_job(job_id, status="error", message=str(e),
                   detail=traceback.format_exc()[-1500:],
                   finished_at=datetime.now().isoformat(timespec="seconds"))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        src.unlink(missing_ok=True)


def worker() -> None:
    while True:
        job_id = JOB_QUEUE.get()
        try:
            process_job(job_id)
        finally:
            JOB_QUEUE.task_done()


threading.Thread(target=worker, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "static" / "index.html").read_text(encoding="utf-8"))


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@app.get("/api/health")
def health() -> dict:
    return {
        "api_key_set": bool(GEMINI_API_KEY),
        "model": GEMINI_MODEL,
        "chunk_sec": CHUNK_SEC,
        "claude_available": bool(shutil.which("claude")),
        "ffmpeg_available": bool(shutil.which("ffmpeg")),
        "lan_ip": lan_ip(),
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), glossary: str = Form(""),
                 polish: str = Form("true"), remove_fillers: str = Form("false")) -> JSONResponse:
    if not GEMINI_API_KEY:
        raise HTTPException(400, "GEMINI_API_KEY is not set. Check the .env file.")

    filename = file.filename or "recording"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, f"Unsupported file type: {suffix or '(none)'}")

    job_id = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{job_id}{suffix}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    size = dest.stat().st_size
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "That file is empty.")

    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id, "filename": filename, "source_path": str(dest),
            "size": size, "status": "queued", "message": "Waiting in queue\u2026",
            "glossary": glossary, "polish": polish.lower() == "true",
            "remove_fillers": remove_fillers.lower() == "true",
            "total_chunks": 0, "done_chunks": 0, "text": "", "raw_text": "",
            "warning": None, "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    JOB_QUEUE.put(job_id)
    return JSONResponse({"job_id": job_id})


@app.get("/api/job/{job_id}")
def job_status(job_id: str) -> dict:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        return {k: v for k, v in job.items() if k not in ("source_path", "detail")}


@app.get("/api/job/{job_id}/download")
def download(job_id: str) -> FileResponse:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "No result yet.")
    path = Path(job["output_path"])
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)


# 出力ファイル名は「元の名前_ジョブID先頭8桁.txt」。表示用にその接尾辞だけ落とす。
JOB_SUFFIX_RE = re.compile(r"_[0-9a-f]{8}$")
# プロジェクト名はそのままフォルダ名になるので、Windows で使えない文字を落とす。
UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]')


def project_dirs() -> list[Path]:
    """プロジェクト = output/ 直下のサブフォルダ。"""
    return sorted((p for p in OUTPUT_DIR.iterdir() if p.is_dir()),
                  key=lambda p: p.name.lower())


def safe_project(name: str) -> str:
    cleaned = UNSAFE_NAME_RE.sub("", name).strip().strip(".")
    if not cleaned:
        raise HTTPException(400, "That project name can't be used.")
    return cleaned[:60]


def project_dir(name: str) -> Path:
    """既存プロジェクトのフォルダ。空文字なら未分類（output/ 直下）。"""
    if not name:
        return OUTPUT_DIR
    path = OUTPUT_DIR / safe_project(name)
    if not path.is_dir():
        raise HTTPException(404, "Project not found.")
    return path


def history_files() -> list[Path]:
    """未分類とプロジェクト内のテキストを、新しい順にまとめて返す。"""
    files = list(OUTPUT_DIR.glob("*.txt"))
    files += [f for d in project_dirs() for f in d.glob("*.txt")]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def history_entry(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": path.relative_to(OUTPUT_DIR).as_posix(),
        "name": path.name,
        "title": JOB_SUFFIX_RE.sub("", path.stem),
        "project": "" if path.parent == OUTPUT_DIR else path.parent.name,
        "bytes": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def history_path(rel: str) -> Path:
    """相対パスから実ファイルを引く。実在する一覧との突き合わせなので ../ は通らない。"""
    for path in history_files():
        if path.relative_to(OUTPUT_DIR).as_posix() == rel:
            return path
    raise HTTPException(404, "That transcript no longer exists.")


@app.get("/api/history")
def history() -> dict:
    items = [history_entry(p) for p in history_files()]
    counts: dict[str, int] = {}
    for item in items:
        counts[item["project"]] = counts.get(item["project"], 0) + 1
    return {
        "items": items,
        "projects": [{"name": d.name, "count": counts.get(d.name, 0)} for d in project_dirs()],
        "unfiled": counts.get("", 0),
    }


@app.get("/api/history/item")
def history_item(path: str) -> dict:
    target = history_path(path)
    text = target.read_text(encoding="utf-8")
    return {**history_entry(target), "text": text, "chars": len(text)}


@app.get("/api/history/download")
def history_download(path: str) -> FileResponse:
    target = history_path(path)
    return FileResponse(target, media_type="text/plain; charset=utf-8", filename=target.name)


@app.delete("/api/history/item")
def history_delete(path: str) -> dict:
    history_path(path).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/history/move")
def history_move(path: str, project: str = "") -> dict:
    """テキストをプロジェクト間で移動する。project が空なら未分類に戻す。"""
    src = history_path(path)
    dest_dir = project_dir(project)
    if src.parent == dest_dir:
        return history_entry(src)

    dest = dest_dir / src.name
    # 同名が居座っている場合だけ連番を足す（通常はジョブIDが付いているので起きない）
    n = 2
    while dest.exists():
        dest = dest_dir / f"{src.stem}({n}){src.suffix}"
        n += 1
    shutil.move(str(src), str(dest))
    return history_entry(dest)


@app.get("/api/projects")
def projects() -> dict:
    return {"projects": [d.name for d in project_dirs()]}


@app.post("/api/projects")
def project_create(name: str) -> dict:
    safe = safe_project(name)
    (OUTPUT_DIR / safe).mkdir(exist_ok=True)
    return {"name": safe}


@app.delete("/api/projects")
def project_delete(name: str) -> dict:
    """フォルダだけ消す。中のテキストは未分類に戻すので、文字起こしは失われない。"""
    target = project_dir(name)
    if target == OUTPUT_DIR:
        raise HTTPException(400, "Project not found.")

    moved = 0
    for item in list(target.glob("*.txt")):
        dest = OUTPUT_DIR / item.name
        n = 2
        while dest.exists():
            dest = OUTPUT_DIR / f"{item.stem}({n}){item.suffix}"
            n += 1
        shutil.move(str(item), str(dest))
        moved += 1

    leftovers = list(target.iterdir())
    if leftovers:
        raise HTTPException(400, "The project folder still has other files in it.")
    target.rmdir()
    return {"ok": True, "moved": moved}


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


if __name__ == "__main__":
    import sys

    import uvicorn

    sys.stdout.reconfigure(line_buffering=True)
    ip = lan_ip()
    print("\n" + "=" * 58)
    print("  ボイスメモ文字起こし サーバー起動")
    print("=" * 58)
    print(f"  このPCから      : http://localhost:{PORT}")
    print(f"  iPhone / 同じWi-Fi: http://{ip}:{PORT}")
    print(f"  モデル          : {GEMINI_MODEL}")
    print(f"  APIキー         : {'設定済み' if GEMINI_API_KEY else '未設定 (.env を確認)'}")
    print("=" * 58 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
