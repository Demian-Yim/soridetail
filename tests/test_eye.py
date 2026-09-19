"""눈앞 보기 — 키 없이 도는 단위 테스트 (프레임 점검·입력 검증·모델 후보 정렬)."""
import base64
import io

import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageFilter

from app import frame_job, vision
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

    def unavailable(text, voice_name):
        raise voice.VoiceUnavailable("없음")

    monkeypatch.setattr(main.voice, "synthesize_wav", unavailable)
    client = TestClient(main.app)
    assert client.post("/api/tts", json={"text": "안내"}).status_code == 503
    assert client.post("/api/tts", json={"text": ""}).status_code == 422
