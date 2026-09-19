"""눈앞 보기 세션 — 카메라 세션 1개 = 일회용 Daytona 샌드박스 1대.

시각장애인의 카메라는 집 안·약봉투·우편물·카드까지 비춘다. 그래서 프레임을 우리 서버에 쌓지 않고
세션 전용 샌드박스 안에서만 점검하고(app/frame_job.py), 세션이 끝나면 샌드박스째 지운다.
요청마다 샌드박스를 새로 만들면 느리므로 세션 동안 한 대를 계속 쓴다.

DAYTONA_API_KEY 가 없으면 같은 점검 코드를 로컬에서 돌린다(where="local") — 데모가 멈추지 않게.
"""
import json
import os
import shlex
import tempfile
import time
from pathlib import Path

from app import frame_job
from app.sandbox_runner import _client

JOB_SOURCE = Path(__file__).resolve().parent / "frame_job.py"
LOCAL_SESSION = "local"
LOCAL_DIR = Path(tempfile.gettempdir()) / "sori_eye"
FRAME_TIMEOUT_SECONDS = 20
SETUP_TIMEOUT_SECONDS = 90
IDLE_SECONDS = 600       # 끝내기 없이 탭을 닫은 세션 — 10분 동안 안 쓰면 샌드박스를 지운다 (요금·할당량 누수 방지)
MAX_SESSIONS = 20        # 동시에 살아 있는 샌드박스 상한

_sessions: dict[str, dict] = {}


def sweep(now: float | None = None) -> int:
    """오래 안 쓴 세션을 지운다. 지운 개수를 돌려준다."""
    now = time.time() if now is None else now
    stale = [sid for sid, item in _sessions.items() if now - item.get("used", now) > IDLE_SECONDS]
    for session_id in stale:
        try:
            end(session_id)
        except Exception as exc:
            print(f"[eye_session] 유휴 세션 삭제 실패 {session_id}: {exc}")
    return len(stale)


def start() -> dict:
    started = time.time()
    sweep(started)
    if len(_sessions) >= MAX_SESSIONS:
        raise RuntimeError("지금 이용자가 많습니다. 잠시 뒤 다시 시도해 주세요.")
    if not os.getenv("DAYTONA_API_KEY"):
        _reset_local()
        return {"session_id": LOCAL_SESSION, "where": "local", "seconds": 0.0}

    sandbox = _client().create()
    try:
        work_dir = sandbox.get_user_home_dir().rstrip("/") + "/eye"
        sandbox.process.exec(f"mkdir -p {shlex.quote(work_dir)}")
        sandbox.fs.upload_file(JOB_SOURCE.read_bytes(), f"{work_dir}/frame_job.py")
        sandbox.process.exec("python -c 'import PIL' 2>/dev/null || pip install -q pillow",
                             timeout=SETUP_TIMEOUT_SECONDS)
    except Exception:
        sandbox.delete()
        raise
    _sessions[sandbox.id] = {"sandbox": sandbox, "work_dir": work_dir, "used": time.time()}
    return {"session_id": sandbox.id, "where": "daytona", "seconds": round(time.time() - started, 1)}


def check_frame(session_id: str, jpeg: bytes) -> dict:
    """프레임 1장을 점검한다. 세션을 못 찾으면(서버 재시작 등) 로컬 점검으로 내려간다."""
    started = time.time()
    session = _sessions.get(session_id)
    if session:
        session["used"] = started
    result = _check_in_sandbox(session, jpeg) if session else _check_locally(jpeg)
    return {**result,
            "where": "daytona" if session else "local",
            "sandbox_id": session_id if session else "",
            "check_ms": int((time.time() - started) * 1000)}


def end(session_id: str) -> bool:
    session = _sessions.pop(session_id, None)
    if not session:
        return False
    session["sandbox"].delete()
    return True


def end_all() -> None:
    for session_id in list(_sessions):
        try:
            end(session_id)
        except Exception as exc:  # 종료 중 1대 실패가 나머지 삭제를 막지 않게
            print(f"[eye_session] 삭제 실패 {session_id}: {exc}")


def _check_in_sandbox(session: dict, jpeg: bytes) -> dict:
    sandbox, work_dir = session["sandbox"], session["work_dir"]
    sandbox.fs.upload_file(jpeg, f"{work_dir}/frame.jpg")
    run = sandbox.process.exec(
        f"cd {shlex.quote(work_dir)} && python frame_job.py frame.jpg prev.jpg",
        timeout=FRAME_TIMEOUT_SECONDS,
    )
    for line in (run.result or "").splitlines():
        if line.startswith("FRAME_OK "):
            return json.loads(line[len("FRAME_OK "):])
    raise RuntimeError(f"프레임 점검 실패: {(run.result or '').strip()[-200:]}")


def _check_locally(jpeg: bytes) -> dict:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    frame_path, prev_path = LOCAL_DIR / "frame.jpg", LOCAL_DIR / "prev.jpg"
    frame_path.write_bytes(jpeg)
    try:
        result = frame_job.analyze(str(frame_path), str(prev_path))
        frame_path.replace(prev_path)
        return result
    finally:
        frame_path.unlink(missing_ok=True)


def _reset_local() -> None:
    (LOCAL_DIR / "prev.jpg").unlink(missing_ok=True)
