"""눈앞 보기 세션 — 카메라 세션 1개 = 일회용 Daytona 샌드박스 1대.

시각장애인의 카메라는 집 안·약봉투·우편물·카드까지 비춘다. 프레임 점검(밝기·흔들림·장면 변화,
app/frame_job.py)은 서버 본체에서 하지 않는다. 세션 전용 샌드박스 안에서 하고, 끝나면 샌드박스째 지운다.

샌드박스를 못 만들면(키 만료·장애) 같은 점검을 job_box 의 격리 프로세스로 돌린다.
이때도 프레임을 디스크에 남기지 않는다. 직전 프레임은 세션별로 메모리에만 두고, 점검할 때마다
새 격리 상자에 넣었다가 상자째 지운다. 모르는 세션 번호는 거절한다(SessionNotFound → 화면이 세션을 새로 연다).
세션 샌드박스도 인터넷을 막고 만든다. pillow 가 없으면 그 샌드박스를 버리고 격리 프로세스로 내려간다.
"""
import json
import math
import secrets
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from app import job_box

JOB_SOURCE = Path(__file__).resolve().parent / "frame_job.py"
FRAME_TIMEOUT_SECONDS = 10
SETUP_TIMEOUT_SECONDS = 90
IDLE_SECONDS = 600       # 끝내기 없이 탭을 닫은 세션 — 10분 동안 안 쓰면 지운다 (요금·할당량 누수 방지)
MAX_SESSIONS = 20
MAX_HINTS = 3
SHUTDOWN_SECONDS = 8

_sessions: dict[str, dict] = {}
_pending = 0          # 만드는 중인 세션 수 — 상한 검사와 생성 사이에 다른 요청이 끼어들지 않게
_lock = threading.Lock()


class SessionNotFound(LookupError):
    """끝났거나(서버 재시작 포함) 처음부터 없던 세션."""


def sweep(now: float | None = None) -> int:
    """오래 안 쓴 세션을 지운다. 지운 개수를 돌려준다."""
    now = time.time() if now is None else now
    with _lock:
        stale = [sid for sid, item in _sessions.items() if now - item.get("used", now) > IDLE_SECONDS]
    return sum(1 for session_id in stale if _safe_end(session_id))


def start() -> dict:
    global _pending
    started = time.time()
    sweep(started)
    with _lock:
        if len(_sessions) + _pending >= MAX_SESSIONS:
            raise RuntimeError("지금 이용자가 많습니다. 잠시 뒤 다시 시도해 주세요.")
        _pending += 1
    try:
        session = _open_sandbox_session() if job_box.daytona_allowed() else None
        session_id = session["sandbox"].id if session else "local-" + secrets.token_urlsafe(12)
        session = session or {"sandbox": None, "prev": None}
        with _lock:
            _sessions[session_id] = {**session, "used": time.time()}
    finally:
        with _lock:
            _pending -= 1
    where = "daytona" if session["sandbox"] else "local"
    return {"session_id": session_id, "where": where, "seconds": round(time.time() - started, 1)}


def _open_sandbox_session() -> dict | None:
    """세션 전용 샌드박스. 못 만들거나 준비가 안 되면 None — 격리 프로세스 점검으로 내려간다."""
    try:
        sandbox = job_box.create_sandbox()
    except Exception as exc:  # 키 만료·할당량·장애
        job_box.daytona_failed(exc)
        print(f"[eye_session] 샌드박스 생성 실패 → 격리 프로세스 점검으로 대체: {type(exc).__name__}: {exc}")
        return None
    try:
        work_dir = sandbox.get_user_home_dir().rstrip("/") + "/eye"
        sandbox.process.exec(f"mkdir -p {shlex.quote(work_dir)}")
        sandbox.fs.upload_file(JOB_SOURCE.read_bytes(), f"{work_dir}/frame_job.py")
        if sandbox.process.exec("python -c 'import PIL'", timeout=SETUP_TIMEOUT_SECONDS).exit_code != 0:
            raise RuntimeError("샌드박스에 pillow 가 없습니다(인터넷을 막았으므로 설치하지 않는다).")
    except Exception as exc:
        job_box.delete_quietly(sandbox)
        job_box.daytona_failed(exc)
        print(f"[eye_session] 샌드박스 준비 실패 → 격리 프로세스 점검으로 대체: {type(exc).__name__}: {exc}")
        return None
    job_box.record_error(None)
    return {"sandbox": sandbox, "work_dir": work_dir}


def check_frame(session_id: str, jpeg: bytes) -> dict:
    """프레임 1장을 점검한다. 세션이 없거나 로컬 세션이면 격리 프로세스에서 점검한다."""
    started = time.time()
    with _lock:
        session = _sessions.get(session_id)
        if session:
            session["used"] = started
    if not session:
        raise SessionNotFound("세션이 끝났습니다. 다시 시작해 주세요.")
    if session.get("sandbox"):
        result, where = _check_in_sandbox(session, jpeg), "daytona"
    else:
        result, where = _check_in_local_box(jpeg, session.get("prev")), "local"
        session["prev"] = jpeg      # 메모리에만. 세션이 끝나면 함께 사라진다
    return {**result, "where": where, "check_ms": int((time.time() - started) * 1000)}


def end(session_id: str) -> bool:
    with _lock:
        session = _sessions.pop(session_id, None)
    if not session:
        return False
    sandbox = session.get("sandbox")
    if sandbox is not None:
        try:
            sandbox.delete()
        except Exception:
            with _lock:   # 지우지 못하면 다시 넣어 두고, 다음 sweep 에서 재시도한다
                _sessions[session_id] = {**session, "used": 0.0}
            raise
    return True


def end_all() -> None:
    with _lock:
        ids = list(_sessions)
    with ThreadPoolExecutor(max_workers=8) as pool:
        wait([pool.submit(_safe_end, sid) for sid in ids], timeout=SHUTDOWN_SECONDS)


def _safe_end(session_id: str) -> bool:
    try:
        return end(session_id)
    except Exception as exc:
        print(f"[eye_session] 세션 정리 실패 {session_id}: {type(exc).__name__}: {exc}")
        return False


def _check_in_sandbox(session: dict, jpeg: bytes) -> dict:
    sandbox, work_dir = session["sandbox"], session["work_dir"]
    sandbox.fs.upload_file(jpeg, f"{work_dir}/frame.jpg")
    inner = f"cd {shlex.quote(work_dir)} && python frame_job.py frame.jpg prev.jpg 2>&1 | head -c 8000"
    run = sandbox.process.exec("sh -c " + shlex.quote(inner), timeout=FRAME_TIMEOUT_SECONDS)
    return _parse_result((run.result or "")[:8000])


def _check_in_local_box(jpeg: bytes, prev: bytes | None) -> dict:
    box = job_box.LocalBox()
    try:
        box.upload(JOB_SOURCE.read_bytes(), f"{box.home}/frame_job.py")
        box.upload(jpeg, f"{box.home}/frame.jpg")
        if prev:
            box.upload(prev, f"{box.home}/prev.jpg")
        run = box.run_job([f"{box.home}/frame_job.py", f"{box.home}/frame.jpg", f"{box.home}/prev.jpg"],
                          FRAME_TIMEOUT_SECONDS)
        return _parse_result(run.result)
    finally:
        box.close()


def _parse_result(output: str) -> dict:
    """점검 결과도 신뢰할 수 없는 해석기의 출력이다. 쓰는 값만, 정해진 형태로 꺼낸다."""
    for line in output.splitlines():
        if not line.startswith("FRAME_OK "):
            continue
        raw = json.loads(line[len("FRAME_OK "):])
        if not isinstance(raw, dict):
            break
        hints = raw.get("hints", [])
        hints = [h[:120] for h in hints if isinstance(h, str)][:MAX_HINTS] if isinstance(hints, list) else []

        def number(key: str):
            value = raw.get(key)
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            return value if ok else None

        return {"usable": raw.get("usable") is True, "changed": raw.get("changed") is not False,
                "hints": hints, "luma": number("luma"), "sharpness": number("sharpness"),
                "change": number("change")}
    print(f"[eye_session] 프레임 점검 실패: {output.strip()[-300:]}")
    raise RuntimeError("프레임을 점검하지 못했습니다.")
