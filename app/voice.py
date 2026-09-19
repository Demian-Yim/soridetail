"""자연스러운 음성 — Supertonic 3 (로컬 실행·무료·할당량 없음).

브라우저 기본 음성은 기계적이라 오래 듣기 힘들다. 안내자의 목소리는 제품의 절반이다.
서버에서 문장을 합성해 WAV 로 돌려준다. 패키지가 없으면 VoiceUnavailable 을 던지고,
화면은 브라우저 기본 음성으로 내려간다 — 어떤 경우에도 안내가 끊기지 않게.
"""
import os
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

MODEL = "supertonic-3"
DEFAULT_VOICE = os.getenv("GUIDE_VOICE", "M1")
VOICES = ("M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5")
SPEED = float(os.getenv("GUIDE_VOICE_SPEED", "1.15"))  # 안내는 또렷하고 약간 빠르게
MAX_TEXT_CHARS = 400
WARMUP_TEXT = "준비되었습니다."

_lock = threading.Lock()  # 합성 엔진은 한 번에 한 문장씩만
_engine: dict = {}


class VoiceUnavailable(RuntimeError):
    """Supertonic 을 쓸 수 없는 환경 — 화면이 브라우저 음성으로 대체한다."""


def _load() -> dict:
    if not _engine:
        try:
            from supertonic import TTS
        except ImportError as exc:
            raise VoiceUnavailable("supertonic 패키지가 없습니다. pip install -U supertonic") from exc
        _engine["tts"] = TTS(model=MODEL, auto_download=True)
        _engine["styles"] = {}
    return _engine


@lru_cache(maxsize=128)
def synthesize_wav(text: str, voice: str = DEFAULT_VOICE) -> bytes:
    """문장 1개 → WAV 바이트. 같은 안내(진행 멘트 등)는 캐시에서 바로 나간다."""
    text = text.strip()[:MAX_TEXT_CHARS]
    if not text:
        raise ValueError("읽을 글이 없습니다.")
    voice = voice if voice in VOICES else DEFAULT_VOICE
    with _lock:
        engine = _load()
        tts, styles = engine["tts"], engine["styles"]
        if voice not in styles:
            styles[voice] = tts.get_voice_style(voice_name=voice)
        wav, _ = tts.synthesize(text, voice_style=styles[voice], lang="ko", speed=SPEED)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "say.wav"
            tts.save_audio(wav, str(path))
            return path.read_bytes()


def warm_up() -> None:
    """서버가 뜰 때 모델을 미리 올려 첫 안내가 늦지 않게 한다."""
    try:
        synthesize_wav(WARMUP_TEXT)
        print(f"[voice] {MODEL} 준비 완료 (voice={DEFAULT_VOICE}, speed={SPEED})")
    except Exception as exc:
        print(f"[voice] 자연 음성을 쓸 수 없어 브라우저 음성으로 동작합니다: {exc}")
