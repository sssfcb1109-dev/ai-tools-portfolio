"""ffmpeg まわり。長い音声を無音位置で分割して mp3 チャンクにする。"""
import re
import shutil
import subprocess
from pathlib import Path

from config import SPLIT_SEARCH_WINDOW_SEC

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def probe_duration(path: Path) -> float:
    proc = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise RuntimeError(f"Could not read the audio duration: {proc.stderr.strip()[:400]}")


def to_wav(src: Path, dst: Path) -> Path:
    """16kHz モノラル wav に正規化する。以降の処理はすべてこれを起点にする。"""
    proc = _run([FFMPEG, "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
                 "-vn", "-sn", "-dn", str(dst)])
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(f"Audio conversion failed: {proc.stderr.strip()[-800:]}")
    return dst


def detect_silences(wav: Path, noise_db: int = -35, min_dur: float = 0.4) -> list[float]:
    """無音区間の中点のリストを返す。ここが分割候補になる。"""
    proc = _run([FFMPEG, "-i", str(wav), "-af",
                 f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"])
    log = proc.stderr or ""
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    midpoints = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        midpoints.append((s + e) / 2 if e is not None else s)
    return midpoints


def plan_splits(duration: float, silences: list[float], chunk_sec: int) -> list[tuple[float, float]]:
    """目標の区切り時刻に一番近い無音を探して分割点にする。見つからなければそこで機械的に切る。"""
    if duration <= chunk_sec:
        return [(0.0, duration)]

    cuts: list[float] = []
    target = float(chunk_sec)
    while target < duration - 30:
        lo, hi = target - SPLIT_SEARCH_WINDOW_SEC, target + SPLIT_SEARCH_WINDOW_SEC
        prev = cuts[-1] if cuts else 0.0
        candidates = [s for s in silences if lo <= s <= hi and s > prev + 30]
        cut = min(candidates, key=lambda s: abs(s - target)) if candidates else target
        if cut > prev + 5:
            cuts.append(cut)
            target = cut + chunk_sec
        else:
            target += chunk_sec

    bounds = [0.0] + cuts + [duration]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def extract_chunk(wav: Path, start: float, end: float, dst: Path) -> Path:
    """指定区間を 64kbps mono mp3 に切り出す。API へインライン送信できるサイズに収めるため。"""
    proc = _run([FFMPEG, "-y", "-i", str(wav), "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                 "-ac", "1", "-ar", "16000", "-b:a", "64k", str(dst)])
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(f"Could not cut an audio chunk: {proc.stderr.strip()[-800:]}")
    return dst


def split_audio(src: Path, work_dir: Path, chunk_sec: int) -> list[tuple[Path, float, float]]:
    """(mp3パス, 開始秒, 終了秒) のリストを返す。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    wav = to_wav(src, work_dir / "normalized.wav")
    duration = probe_duration(wav)

    silences = detect_silences(wav) if duration > chunk_sec else []
    segments = plan_splits(duration, silences, chunk_sec)

    chunks = []
    for i, (start, end) in enumerate(segments):
        dst = work_dir / f"chunk_{i:03d}.mp3"
        extract_chunk(wav, start, end, dst)
        chunks.append((dst, start, end))
    return chunks
