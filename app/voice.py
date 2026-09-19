"""자연스러운 음성 — Supertonic 3 (로컬 실행·무료·할당량 없음).

브라우저 기본 음성은 기계적이라 오래 듣기 힘들다. 안내자의 목소리는 제품의 절반이다.
서버에서 문장을 합성해 WAV 로 돌려준다. 패키지가 없으면 VoiceUnavailable 을 던지고,
화면은 브라우저 기본 음성으로 내려간다 — 어떤 경우에도 안내가 끊기지 않게.

목소리는 '프리셋'(톤 이름 + 음성 + 기본 속도)으로 고른다. 기본값은 측정으로 정했다.
2026-09-19 같은 문장을 10종으로 합성해 잰 값(음높이 Hz · 억양 폭 Hz · 150~1000Hz 따뜻한 대역 % · 3~6kHz 거친 대역 %):
  F1 191·41·51.6·8.0   F2 223·54·43.1·10.0   F3 179·44·40.2·8.8   F4 192·29·48.7·9.2   F5 160·37·53.6·15.0
  M1 135·37·40.1·13.2  M2  94·43·46.0·10.9   M3 103·18·35.9·16.5  M4 125·34·50.1·12.0  M5  91·16·42.6·14.0
기본 = F1: 거친 대역이 10종 중 가장 낮고(오래 들어도 귀가 덜 피로), 따뜻한 대역은 상위, 억양 폭이 넉넉해 반갑게 들린다.
말이 가장 느린 편이라 속도를 1.15 로 올려 경쾌함을 보정했다. (귀로 들은 평가가 아니라 측정값 기준 — 사용자가 바꿀 수 있다)
"""
import os
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

MODEL = "supertonic-3"
VOICES = ("M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5")
PRESETS = (
    {"id": "bright", "name": "밝고 따뜻한 목소리", "note": "기본값. 오래 들어도 편안한 여성 음성", "voice": "F1", "speed": 1.15},
    {"id": "brisk", "name": "경쾌하고 또렷한 목소리", "note": "말이 빠르고 단정한 여성 음성", "voice": "F4", "speed": 1.1},
    {"id": "lively", "name": "생기 있는 높은 목소리", "note": "억양이 가장 풍부한 여성 음성", "voice": "F2", "speed": 1.1},
    {"id": "calm", "name": "차분한 목소리", "note": "낮고 부드러운 여성 음성", "voice": "F3", "speed": 1.05},
    {"id": "steady", "name": "든든한 남성 목소리", "note": "따뜻하고 안정적인 남성 음성", "voice": "M4", "speed": 1.1},
    {"id": "deep", "name": "낮고 묵직한 남성 목소리", "note": "가장 낮은 남성 음성", "voice": "M2", "speed": 1.1},
)
DEFAULT_PRESET = os.getenv("GUIDE_VOICE_PRESET", "bright")
# 속도 배율 — 스크린리더에 익숙한 사용자는 빠른 말을 선호한다
SPEED_STEPS = ({"id": "slow", "name": "느리게", "factor": 0.85}, {"id": "normal", "name": "보통", "factor": 1.0},
               {"id": "fast", "name": "빠르게", "factor": 1.2}, {"id": "faster", "name": "아주 빠르게", "factor": 1.4})
MIN_SPEED, MAX_SPEED = 0.8, 1.7
MAX_TEXT_CHARS = 400
WARMUP_TEXT = "준비되었습니다."

_lock = threading.Lock()  # 합성 엔진은 한 번에 한 문장씩만
_engine: dict = {}


class VoiceUnavailable(RuntimeError):
    """Supertonic 을 쓸 수 없는 환경 — 화면이 브라우저 음성으로 대체한다."""


def preset(preset_id: str = "") -> dict:
    by_id = {item["id"]: item for item in PRESETS}
    return by_id.get(preset_id) or by_id.get(DEFAULT_PRESET) or PRESETS[0]


DEFAULT_VOICE = preset()["voice"]


def catalog() -> dict:
    """화면의 목소리 고르기에 그대로 내려주는 목록."""
    return {"default": preset()["id"], "presets": list(PRESETS), "speeds": list(SPEED_STEPS)}


def resolve(preset_id: str = "", voice: str = "", factor: float = 1.0) -> tuple[str, float]:
    """(프리셋 또는 음성 이름, 속도 배율) → 실제 합성에 쓸 (음성, 속도)."""
    chosen = preset(preset_id)
    name = voice if voice in VOICES else chosen["voice"]
    base = chosen["speed"] if name == chosen["voice"] else 1.1
    return name, round(min(MAX_SPEED, max(MIN_SPEED, base * factor)), 2)


def _load() -> dict:
    if not _engine:
        try:
            from supertonic import TTS
        except ImportError as exc:
            raise VoiceUnavailable("supertonic 패키지가 없습니다. pip install -U supertonic") from exc
        _engine["tts"] = TTS(model=MODEL, auto_download=True)
        _engine["styles"] = {}
    return _engine


@lru_cache(maxsize=256)
def synthesize_wav(text: str, voice: str = DEFAULT_VOICE, speed: float = 0.0) -> bytes:
    """문장 1개 → WAV 바이트. 같은 안내(진행 멘트 등)는 캐시에서 바로 나간다."""
    text = text.strip()[:MAX_TEXT_CHARS]
    if not text:
        raise ValueError("읽을 글이 없습니다.")
    if voice not in VOICES:
        voice = DEFAULT_VOICE
    if not speed:
        speed = resolve(voice=voice)[1]
    speed = min(MAX_SPEED, max(MIN_SPEED, speed))
    with _lock:
        engine = _load()
        tts, styles = engine["tts"], engine["styles"]
        if voice not in styles:
            styles[voice] = tts.get_voice_style(voice_name=voice)
        wav, _ = tts.synthesize(text, voice_style=styles[voice], lang="ko", speed=speed)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "say.wav"
            tts.save_audio(wav, str(path))
            return path.read_bytes()


def warm_up() -> None:
    """서버가 뜰 때 모델을 미리 올려 첫 안내가 늦지 않게 한다."""
    try:
        name, speed = resolve()
        synthesize_wav(WARMUP_TEXT, name, speed)
        print(f"[voice] {MODEL} 준비 완료 (preset={preset()['id']}, voice={name}, speed={speed})")
    except Exception as exc:
        print(f"[voice] 자연 음성을 쓸 수 없어 브라우저 음성으로 동작합니다: {exc}")
