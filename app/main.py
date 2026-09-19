"""소리상세 (SoriDetail) — 이미지로만 된 상품 상세페이지를 시각장애인에게 읽어주는 서비스.

흐름: 주소 입력 → Daytona 샌드박스가 페이지 수집·이미지 타일링 → 비전 AI가 읽기 → 음성 안내.
실행: uvicorn app.main:app --port 8000
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import vision
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
