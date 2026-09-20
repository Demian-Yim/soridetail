"""눈앞 보기 — 키 없이 도는 단위 테스트 (프레임 점검·입력 검증·모델 후보 정렬)."""
import base64
import io

import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageFilter

from app import eye_session, frame_job, vision
from app.main import decode_frame


def save(tmp_path, name: str, image: Image.Image) -> str:
    path = tmp_path / name
    image.save(path, "JPEG")
    return str(path)


def busy_scene(offset: int = 0) -> Image.Image:
    image = Image.new("RGB", (640, 480), (200, 200, 200))
    draw = ImageDraw.Draw(image)
    for i in range(0, 640, 40):
        draw.rectangle([i + offset, 100, i + offset + 18, 380], fill=(20, 20, 20))
    return image


def test_dark_frame_is_unusable_and_explains_why(tmp_path):
    result = frame_job.analyze(save(tmp_path, "dark.jpg", Image.new("RGB", (640, 480), (4, 4, 4))))
    assert result["usable"] is False
    assert result["hints"] == [frame_job.HINT_DARK]


def test_blurred_frame_gets_a_hold_still_hint(tmp_path):
    blurred = busy_scene().filter(ImageFilter.GaussianBlur(25))
    assert frame_job.HINT_BLUR in frame_job.analyze(save(tmp_path, "blur.jpg", blurred))["hints"]
    assert frame_job.analyze(save(tmp_path, "sharp.jpg", busy_scene()))["hints"] == []


def test_same_scene_is_not_a_change_but_new_scene_is(tmp_path):
    first = save(tmp_path, "a.jpg", busy_scene())
    assert frame_job.analyze(first)["changed"] is True  # 첫 장면은 항상 말한다
    assert frame_job.analyze(save(tmp_path, "b.jpg", busy_scene()), first)["changed"] is False
    other = Image.new("RGB", (640, 480), (30, 120, 220))
    assert frame_job.analyze(save(tmp_path, "c.jpg", other), first)["changed"] is True


def test_decode_frame_rejects_non_jpeg_and_bad_base64():
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, "JPEG")
    assert decode_frame(base64.b64encode(buffer.getvalue()).decode()).startswith(bytes.fromhex("ffd8ff"))
    for bad in (base64.b64encode(b"GIF89a" + b"0" * 200).decode(), "%%%not-base64%%%" * 10):
        with pytest.raises(HTTPException):
            decode_frame(bad)


def test_gemini_candidates_sort_by_version_and_skip_omni(monkeypatch):
    """회귀: 이름순 정렬이 할당량 0인 gemini-omni-* 를 골라 429 가 났다 (2026-09-19)."""
    names = ["gemini-2.5-flash", "gemini-omni-1.1-flash", "gemini-3.6-flash", "gemini-3.10-flash",
             "gemini-3.6-flash-lite", "gemini-flash-latest", "gemini-3.1-flash-image"]

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"models": [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in names]}

    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(vision, "_gemini_candidates_cache", [])
    monkeypatch.setattr(vision.httpx, "get", lambda *a, **k: FakeResponse())
    assert vision._gemini_candidates("k") == ["gemini-3.10-flash", "gemini-3.6-flash", "gemini-2.5-flash"]


def test_look_only_mentions_last_callout_in_watch_mode(monkeypatch):
    seen = []
    monkeypatch.setattr(vision, "_call", lambda system, text, tiles, fast=False: seen.append(text) or "ok")
    vision.look("x", "look", last_callout="아까 안내")
    vision.look("x", "watch", last_callout="아까 안내")
    assert "아까 안내" not in seen[0] and "아까 안내" in seen[1]


def test_tts_falls_back_cleanly_when_voice_engine_is_missing(monkeypatch):
    """자연 음성을 못 쓰면 503 — 화면(voice.js)은 이 신호로 브라우저 음성으로 내려간다."""
    from fastapi.testclient import TestClient

    from app import main, voice

    def unavailable(text, voice_name, speed):
        raise voice.VoiceUnavailable("없음")

    monkeypatch.setattr(main.voice, "synthesize_wav", unavailable)
    client = TestClient(main.app)
    assert client.post("/api/tts", json={"text": "안내"}).status_code == 503
    assert client.post("/api/tts", json={"text": ""}).status_code == 422


def test_voice_presets_resolve_to_a_real_voice_and_a_clamped_speed():
    from app import voice

    assert voice.preset("nope")["id"] == voice.preset()["id"]           # 모르는 프리셋은 기본값으로
    assert voice.resolve("brisk") == ("F4", 1.1)
    assert voice.resolve("bright", factor=1.4)[1] == round(1.15 * 1.4, 2)  # 속도 배율은 프리셋 기본 속도에 곱한다
    assert voice.resolve("bright", factor=9)[1] == voice.MAX_SPEED        # 한도를 넘지 않는다
    assert voice.resolve(voice="ZZ")[0] == voice.preset()["voice"]        # 없는 음성 이름은 무시
    assert all(item["voice"] in voice.VOICES for item in voice.PRESETS)
    assert len({item["id"] for item in voice.PRESETS}) == len(voice.PRESETS)


def test_rate_limit_blocks_a_burst_per_ip_but_not_other_ips(monkeypatch):
    """공개 주소에서 한 사람이 몰아쳐도 키 요금이 새지 않아야 하고, 그 때문에 다른 사람이 막히면 안 된다."""
    from types import SimpleNamespace

    from app import limits

    def visitor(ip: str):
        return SimpleNamespace(headers={"x-forwarded-for": f"{ip}, 10.0.0.1"}, client=None)

    limits.reset()
    monkeypatch.setenv("LIMIT_LOOK_PER_MIN", "3")
    for second in range(3):
        limits.check("look", visitor("1.1.1.1"), now=1000.0 + second)
    with pytest.raises(HTTPException) as blocked:
        limits.check("look", visitor("1.1.1.1"), now=1003.0)
    assert blocked.value.status_code == 429 and blocked.value.detail == limits.MESSAGES["minute"]
    limits.check("look", visitor("2.2.2.2"), now=1003.0)     # 다른 사람은 영향 없음
    limits.check("look", visitor("1.1.1.1"), now=1061.0)     # 1분 지나면 풀린다
    limits.reset()


def test_rate_limit_daily_total_cap_protects_the_whole_service(monkeypatch):
    from types import SimpleNamespace

    from app import limits

    limits.reset()
    monkeypatch.setenv("LIMIT_LOOK_TOTAL_PER_DAY", "2")
    for n in range(2):
        limits.check("look", SimpleNamespace(headers={"x-forwarded-for": f"9.9.9.{n}"}, client=None), now=5000.0)
    with pytest.raises(HTTPException) as blocked:
        limits.check("look", SimpleNamespace(headers={"x-forwarded-for": "9.9.9.7"}, client=None), now=5001.0)
    assert blocked.value.detail == limits.MESSAGES["total"]
    limits.check("look", SimpleNamespace(headers={"x-forwarded-for": "9.9.9.7"}, client=None), now=5000.0 + limits.DAY + 1)
    limits.reset()


def test_idle_sessions_are_swept_so_abandoned_sandboxes_do_not_keep_billing(monkeypatch):
    from app import eye_session

    deleted = []

    class FakeSandbox:
        def __init__(self, name): self.name = name
        def delete(self): deleted.append(self.name)

    monkeypatch.setattr(eye_session, "_sessions", {
        "old": {"sandbox": FakeSandbox("old"), "work_dir": "/x", "used": 1000.0},
        "fresh": {"sandbox": FakeSandbox("fresh"), "work_dir": "/x", "used": 1000.0 + eye_session.IDLE_SECONDS},
    })
    assert eye_session.sweep(now=1000.0 + eye_session.IDLE_SECONDS + 5) == 1
    assert deleted == ["old"] and list(eye_session._sessions) == ["fresh"]


def test_start_falls_back_to_local_when_sandbox_creation_fails(monkeypatch):
    """키가 만료돼 샌드박스를 못 만들어도 502 대신 서버 안 점검으로 내려가고, 그 사실을 숨기지 않는다."""
    monkeypatch.setenv("DAYTONA_API_KEY", "dtn_expired")

    class BrokenClient:
        def create(self):
            raise RuntimeError("Invalid credentials")

    monkeypatch.setattr(eye_session, "_client", lambda: BrokenClient())
    info = eye_session.start()
    assert info["where"] == "local" and info["session_id"] == eye_session.LOCAL_SESSION
    assert "note" in info
    assert "Invalid credentials" in eye_session.last_sandbox_error
