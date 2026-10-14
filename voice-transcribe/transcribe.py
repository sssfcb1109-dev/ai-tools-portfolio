"""Gemini で文字起こしし、Claude Code CLI で校正する。"""
import base64
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from config import CLAUDE_TIMEOUT_SEC, GEMINI_ENDPOINT, GEMINI_MODEL

# 設定モデルが使えなかったときに上から順に試す候補。
# モデル一覧APIは提供終了済みのモデルも返してくるので、実際に呼んで404なら次へ落とす。
# gemini-3.1-pro-preview は無料枠が 0 なので入れない（課金時に .env で明示指定すれば使える）。
MODEL_PREFERENCE = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

_dead_models: set[str] = set()

_resolved_model: str | None = None


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to the .env file."
        )
    return key


def _request(url: str, payload: dict | None = None, method: str = "GET", timeout: int = 900) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-goog-api-key", _api_key())
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_models() -> list[str]:
    result = _request(f"{GEMINI_ENDPOINT}/models?pageSize=200", timeout=60)
    names = []
    for m in result.get("models", []):
        if "generateContent" in m.get("supportedGenerationMethods", []):
            names.append(m["name"].removeprefix("models/"))
    return names


def resolve_model() -> str:
    """使えるモデルを決める。404 で落ちたモデルは _dead_models に入り、次の候補に回る。"""
    global _resolved_model
    if _resolved_model and _resolved_model not in _dead_models:
        return _resolved_model

    candidates = [GEMINI_MODEL] + [m for m in MODEL_PREFERENCE if m != GEMINI_MODEL]
    candidates = [m for m in candidates if m not in _dead_models]
    if not candidates:
        raise RuntimeError("No usable Gemini model. Check GEMINI_MODEL in .env.")

    try:
        available = set(list_models())
    except Exception:
        _resolved_model = candidates[0]
        return _resolved_model

    for m in candidates:
        if m in available:
            _resolved_model = m
            return _resolved_model

    _resolved_model = candidates[0]
    return _resolved_model


def _retire_model(model: str) -> None:
    """提供終了などで使えないと分かったモデルを候補から外す。"""
    global _resolved_model
    _dead_models.add(model)
    if _resolved_model == model:
        _resolved_model = None


def _build_prompt(glossary: str, previous_tail: str, remove_fillers: bool,
                  index: int, total: int) -> str:
    rules = [
        "音声に含まれる発話をすべて、省略せずに書き起こしてください。",
        "要約・意訳・言い換えは絶対にしないでください。話された内容をそのまま文字にします。",
        "聞き取れた内容に対して、読みやすい位置に句読点と改行を入れてください。",
        "話題が変わるところで段落を分けてください。",
        "書き起こしたテキストだけを出力してください。前置き・後書き・見出し・箇条書き記号は不要です。",
        "音声が無音、または発話が含まれていない場合は、何も出力しないでください。",
    ]
    if remove_fillers:
        rules.append("「えー」「あのー」「まあ」などのフィラーと、意味のない言い淀みは取り除いてください。")
    else:
        rules.append("フィラーや言い直しも、話された通りに残してください。")

    parts = ["これは日本語の音声です。正確に文字起こししてください。", ""]
    parts += [f"- {r}" for r in rules]

    if total > 1:
        parts += ["", f"この音声は長い録音を分割したうちの {index + 1}/{total} 個目です。"]
        if previous_tail:
            parts += [
                "直前のチャンクの末尾は次の通りです。文脈の参考にしてください（この部分は出力に含めないこと）:",
                f"---\n{previous_tail}\n---",
            ]

    if glossary.strip():
        parts += [
            "",
            "この音声には次の固有名詞・専門用語が登場します。表記はこれに合わせてください:",
            glossary.strip(),
        ]

    return "\n".join(parts)


def transcribe_chunk(mp3_path: Path, glossary: str = "", previous_tail: str = "",
                     remove_fillers: bool = False, index: int = 0, total: int = 1,
                     max_retries: int = 8) -> str:
    audio_b64 = base64.b64encode(mp3_path.read_bytes()).decode("ascii")
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": _build_prompt(glossary, previous_tail, remove_fillers, index, total)},
                {"inline_data": {"mime_type": "audio/mp3", "data": audio_b64}},
            ],
        }],
        "generationConfig": {"temperature": 0.0},
    }

    last_error = None
    skip: set[str] = set()  # このチャンクで一時的に避けているモデル（混雑・レート制限）
    fails_on_model = 0
    for attempt in range(max_retries):
        model = _pick_model(skip)
        url = f"{GEMINI_ENDPOINT}/models/{model}:generateContent"
        try:
            result = _request(url, payload, method="POST")
            candidates = result.get("candidates") or []
            if not candidates:
                feedback = result.get("promptFeedback", {})
                raise RuntimeError(f"Empty response: {json.dumps(feedback, ensure_ascii=False)[:300]}")
            candidate = candidates[0]
            text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []))
            if not text.strip() and candidate.get("finishReason") not in (None, "STOP"):
                raise RuntimeError(f"Transcription stopped early (finishReason={candidate.get('finishReason')})")
            return text.strip()

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"Gemini API error {e.code} ({model}): {body[:500]}")
            if e.code == 404 or (e.code == 429 and "limit: 0" in body):
                # 提供終了、または無料枠が 0 のモデル。待っても使えないので候補から外す
                _retire_model(model)
                fails_on_model = 0
                continue
            if e.code in (429, 500, 502, 503, 504):
                # 一時的な混雑・分単位のレート制限。モデルは外さず、待ってから再試行する
                fails_on_model += 1
                if fails_on_model >= 2:
                    skip.add(model)  # 同じモデルで2回続けて失敗したら、このチャンクだけ別モデルへ
                    fails_on_model = 0
                time.sleep(_retry_delay(body, attempt))
                continue
            raise last_error

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise

    raise last_error


def _pick_model(skip: set[str]) -> str:
    """resolve_model() の結果を基本に、skip 中のモデルは避ける。全部 skip なら skip を解除して最初に戻る。"""
    preferred = resolve_model()
    if preferred not in skip:
        return preferred
    candidates = [GEMINI_MODEL] + [m for m in MODEL_PREFERENCE if m != GEMINI_MODEL]
    for m in candidates:
        if m not in skip and m not in _dead_models:
            return m
    skip.clear()
    return preferred


def _retry_delay(body: str, attempt: int) -> float:
    """API が retryDelay を返していればそれに従い、なければ指数バックオフ（最大60秒）。"""
    try:
        for d in json.loads(body).get("error", {}).get("details", []):
            delay = d.get("retryDelay")
            if delay and delay.endswith("s"):
                return min(float(delay[:-1]) + 1, 90)
    except (ValueError, AttributeError):
        pass
    return min(5 * 2 ** attempt, 60)


def _claude_path() -> str:
    path = shutil.which("claude")
    if not path:
        raise RuntimeError("The claude command was not found. Check that Claude Code CLI is installed.")
    return path


def polish(text: str, glossary: str = "") -> str:
    """Claude Code CLI に校正させる。内容は変えず、誤変換と表記だけ直させる。"""
    if not text.strip():
        return text

    instruction = [
        "あなたは日本語の文字起こし原稿の校正者です。",
        "標準入力で渡された音声認識の出力を校正してください。",
        "",
        "ルール:",
        "- 誤変換・同音異義語の間違いを、文脈から判断して正しい表記に直す",
        "- 句読点と改行を読みやすく整える",
        "- 明らかな認識ミスの単語だけを修正する",
        "- 内容の要約・省略・追記は絶対にしない。文の数を減らさない",
        "- 話し言葉の語尾や言い回しはそのまま残す",
        "- 判断がつかない箇所は元のまま残す",
        "",
        "校正後の本文だけを出力してください。説明・前置き・コードブロックは不要です。",
    ]
    if glossary.strip():
        instruction += ["", "以下の固有名詞・専門用語の表記に合わせてください:", glossary.strip()]

    proc = subprocess.run(
        [_claude_path(), "-p", "\n".join(instruction), "--output-format", "text"],
        input=text, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=CLAUDE_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Claude proofreading failed: {(proc.stderr or '').strip()[-400:]}")

    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("Claude proofreading returned nothing.")
    # 校正で極端に短くなっていたら要約されたとみなして元を採用する
    if len(out) < len(text) * 0.5:
        raise RuntimeError("Proofread text was too short, so it was discarded (it may have been summarized).")
    return out
