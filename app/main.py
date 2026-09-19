"""소리상세 (SoriDetail) — 이미지로만 된 상품 상세페이지를 시각장애인에게 읽어주는 서비스.

흐름: 주소 입력 → Daytona 샌드박스가 페이지 수집·이미지 타일링 → 비전 AI가 읽기 → 음성 안내.
실행: uvicorn app.main:app --port 8000
"""
import base64
import binascii
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import eye_session, vision, voice
from app.sandbox_runner import collect_page

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR.parent / ".env"


def reload_env() -> None:
    """.env 를 저장만 하면 서버 재시작 없이 키가 반영되게 한다."""
    load_dotenv(ENV_PATH, override=True)


reload_env()

app = FastAPI(title="소리상세 SoriDetail")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class ReadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    context: str = Field(min_length=1, max_length=20000)


def validate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="http 또는 https 로 시작하는 주소를 입력해 주세요.")
    return url.strip()


def event(kind: str, **data) -> str:
    return json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n"


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
def health():
    reload_env()
    return {"daytona_key": bool(os.getenv("DAYTONA_API_KEY")), "vision": vision.provider_name()}


@app.post("/api/read")
def read_page(req: ReadRequest):
    """진행 상황을 한 줄씩(NDJSON) 흘려보낸다 — 화면을 못 보는 사용자가 기다리는 동안 음성으로 안내받도록."""
    url = validate_url(req.url)
    reload_env()

    def stream():
        try:
            page = None
            for step in collect_page(url):
                if step["type"] == "progress":
                    yield event("progress", message=step["message"])
                else:
                    page = step
            yield event("progress", message="인공지능이 상세 이미지를 읽고 있습니다.")
            answer = vision.describe_page(page)
            yield event(
                "result",
                title=page["title"],
                answer=answer,
                stats={
                    "images": page["image_count"],
                    "images_without_alt": page["image_alts_missing"],
                    "tiles_read": len(page["tiles_b64"]),
                    "sandbox_id": page["sandbox_id"],
                    "sandbox_seconds": page["sandbox_seconds"],
                },
            )
        except Exception as exc:  # 사용자에게는 쉬운 말로, 서버 로그에는 원문으로
            print(f"[read_page] {type(exc).__name__}: {exc}")
            yield event("error", message=f"읽는 중 문제가 생겼습니다. {exc}")

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/ask")
def ask(req: AskRequest):
    try:
        return {"answer": vision.answer_question(req.question, req.context)}
    except Exception as exc:
        print(f"[ask] {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=502, detail="답변을 만드는 중 문제가 생겼습니다.") from exc


# ───────── 눈앞 보기 (실시간 카메라 안내) ─────────

MAX_FRAME_BYTES = 3_000_000
JPEG_MAGIC = bytes.fromhex("ffd8ff")


class LookRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    image_b64: str = Field(min_length=100, max_length=4_200_000)
    mode: Literal["look", "find", "read", "people", "watch", "self"] = "look"
    question: str = Field(default="", max_length=300)
    last_callout: str = Field(default="", max_length=600)


class SessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)


def decode_frame(image_b64: str) -> bytes:
    try:
        jpeg = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="사진을 읽지 못했습니다.") from exc
    if len(jpeg) > MAX_FRAME_BYTES or not jpeg.startswith(JPEG_MAGIC):
        raise HTTPException(status_code=400, detail="JPEG 사진만, 3메가바이트 이하로 보낼 수 있습니다.")
    return jpeg


@app.get("/eye")
def eye_page():
    return FileResponse(BASE_DIR / "static" / "eye.html")


@app.get("/sw.js")
def service_worker():
    """서비스워커는 자기 경로 아래만 다룰 수 있어 /static 이 아니라 루트에서 내준다. 없어도 앱은 그대로 동작한다."""
    path = BASE_DIR / "static" / "sw.js"
    if not path.exists():
        raise HTTPException(status_code=404, detail="서비스워커가 없습니다.")
    return FileResponse(path, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.post("/api/eye/start")
def eye_start():
    reload_env()
    try:
        return eye_session.start()
    except Exception as exc:
        print(f"[eye_start] {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=502, detail=f"샌드박스를 준비하지 못했습니다. {exc}") from exc


@app.post("/api/eye/look")
def eye_look(req: LookRequest):
    """프레임 점검(샌드박스)과 장면 판독(비전 AI)을 동시에 돌린다. 계속 보기에서는 점검을 먼저 하고 변화가 없으면 침묵한다."""
    jpeg = decode_frame(req.image_b64)
    reload_env()
    try:
        if req.mode == "watch":
            quality = eye_session.check_frame(req.session_id, jpeg)
            if not quality["usable"] or not quality["changed"]:
                return {"callout": " ".join(quality["hints"]), "silent": not quality["hints"], "quality": quality}
            callout = vision.look(req.image_b64, req.mode, req.question, req.last_callout)
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                quality_job = pool.submit(eye_session.check_frame, req.session_id, jpeg)
                callout_job = pool.submit(vision.look, req.image_b64, req.mode, req.question, req.last_callout)
                quality, callout = quality_job.result(), callout_job.result()
            if not quality["usable"]:
                callout = ""
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[eye_look] {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=502, detail=f"장면을 읽는 중 문제가 생겼습니다. {exc}") from exc

    silent = callout.strip() == vision.NO_CHANGE and not quality["hints"]
    spoken = "" if callout.strip() == vision.NO_CHANGE else callout
    return {"callout": " ".join([*quality["hints"], spoken]).strip(), "silent": silent, "quality": quality}


@app.post("/api/eye/end")
def eye_end(req: SessionRequest):
    try:
        return {"deleted": eye_session.end(req.session_id)}
    except Exception as exc:
        print(f"[eye_end] {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=502, detail="샌드박스를 지우지 못했습니다.") from exc


@app.on_event("shutdown")
def delete_all_sandboxes():
    eye_session.end_all()


# ───────── 자연 음성 (Supertonic 3, 로컬·무료) ─────────

class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=voice.MAX_TEXT_CHARS)
    voice: str = Field(default=voice.DEFAULT_VOICE, max_length=4)


@app.post("/api/tts")
def speak_text(req: SpeakRequest):
    try:
        return Response(content=voice.synthesize_wav(req.text, req.voice), media_type="audio/wav")
    except voice.VoiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        print(f"[tts] {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=502, detail="음성을 만들지 못했습니다.") from exc


@app.on_event("startup")
def warm_up_voice():
    threading.Thread(target=voice.warm_up, daemon=True).start()
