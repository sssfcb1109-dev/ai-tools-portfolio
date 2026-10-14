"""設定。.env があればそこから読み、なければ環境変数を使う。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
WORK_DIR = BASE_DIR / "work"
for d in (UPLOAD_DIR, OUTPUT_DIR, WORK_DIR):
    d.mkdir(exist_ok=True)


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"

# 音声を何秒ごとに分割するか（無音位置を探して実際の切れ目は前後にずれる）
CHUNK_SEC = int(os.environ.get("CHUNK_SEC", "600"))
# 分割点を探すときに目標時刻から前後何秒まで無音を探すか
SPLIT_SEARCH_WINDOW_SEC = 60

CLAUDE_TIMEOUT_SEC = int(os.environ.get("CLAUDE_TIMEOUT_SEC", "600"))
PORT = int(os.environ.get("PORT", "8000"))
