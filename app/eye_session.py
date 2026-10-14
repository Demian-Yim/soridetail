"""눈앞 보기 세션 — 카메라 세션 1개 = 일회용 Daytona 샌드박스 1대.

시각장애인의 카메라는 집 안·약봉투·우편물·카드까지 비춘다. 프레임 점검(밝기·흔들림·장면 변화,
app/frame_job.py)은 서버 본체에서 하지 않는다. 세션 전용 샌드박스 안에서 하고, 끝나면 샌드박스째 지운다.

샌드박스를 못 만들면(키 만료·장애) 같은 점검을 job_box 의 격리 프로세스로 돌린다.
이때도 프레임을 디스크에 남기지 않는다. 직전 프레임은 세션별로 메모리에만 두고, 점검할 때마다
새 격리 상자에 넣었다가 상자째 지운다. 모르는 세션 번호로 와도 같은 격리 경로를 탄다(본체 디코딩 경로 없음).

참고: 세션 샌드박스는 pillow 설치 때문에 네트워크를 연 채로 만든다. 프레임은 사용자 자신의 카메라 영상이다.
"""
import json
import secrets
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from app import job_box

JOB_SOURCE = Path(__file__).resolve().parent / "frame_job.py"
FRAME_TIMEOUT_SECONDS = 20
SETUP_TIMEOUT_SECONDS = 90
IDLE_SECONDS = 600       # 끝내기 없이 탭을 닫은 세션 — 10분 동안 안 쓰면 지운다 (요금·할당량 누수 방지)
MAX_SESSIONS = 20
MAX_HINTS = 3
SHUTDOWN_SECONDS = 8

_sessions: dict[str, dict] = {}
_lock = threading.Lock()


def sweep(now: float | None = None) -> int:
    """오래 안 쓴 세션을 지운다. 지운 개수를 돌려준다."""
    now = time.time() if now is None else now
    with _lock:
        stale = [sid for sid, item in _sessions.items() if now - item.get("used", now) > IDLE_SECONDS]
    return sum(1 for session_id in stale if _safe_end(session_id))


def start() -> dict:
    started = time.time()
    sweep(started)
    with _lock:
        if len(_sessions) >= MAX_SESSIONS:
            raise RuntimeError("지금 이용자가 많습니다. 잠시 뒤 다시 시도해 주세요.")

    if job_box.daytona_allowed():
        try:
            sandbox = job_box.create_sandbox(network=True)
        except Exception as exc:  # 키 만료·할당량·장애 — 502 대신 격리 프로세스 점검으로 내려간다
            job_box.daytona_failed(exc)
            print(f"[eye_session] 샌드박스 생성 실패 → 격리 프로세스 점검으로 대체: {type(exc).__name__}: {exc}")
        else:
            try:
                work_dir = sandbox.get_user_home_dir().rstrip("/") + "/eye"
                sandbox.process.exec(f"mkdir -p {shlex.quote(work_dir)}")
                sandbox.fs.upload_file(JOB_SOURCE.read_bytes(), f"{work_dir}/frame_job.py")
                sandbox.process.exec("python -c 'import PIL' 2>/dev/null || pip install -q pillow",
                                     timeout=SETUP_TIMEOUT_SECONDS)
            except Exception:
                _delete_quietly(sandbox)
                raise
            job_box.record_error(None)
            with _lock:
                _sessions[sandbox.id] = {"sandbox": sandbox, "work_dir": work_dir, "used": time.time()}
            return {"session_id": sandbox.id, "where": "daytona", "seconds": round(time.time() - started, 1)}

    session_id = "local-" + secrets.token_urlsafe(12)
    with _lock:
        _sessions[session_id] = {"sandbox": None, "prev": None, "used": time.time()}
    return {"session_id": session_id, "where": "local", "seconds": round(time.time() - started, 1),
            "note": "샌드박스를 만들지 못해 서버의 격리 프로세스에서 점검합니다."}


def check_frame(session_id: str, jpeg: bytes) -> dict:
    """프레임 1장을 점검한다. 세션이 없거나 로컬 세션이면 격리 프로세스에서 점검한다."""
    started = time.time()
    with _lock:
        session = _sessions.get(session_id)
        if session:
            session["used"] = started
    if session and session.get("sandbox"):
        result, where = _check_in_sandbox(session, jpeg), "daytona"
    else:
        prev = session.get("prev") if session else None
        result, where = _check_in_local_box(jpeg, prev), "local"
        if session:
            session["prev"] = jpeg      # 메모리에만. 세션이 끝나면 함께 사라진다
    return {**result, "where": where, "sandbox_id": session_id if session else "",
            "check_ms": int((time.time() - started) * 1000)}


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


def _delete_quietly(sandbox) -> None:
    try:
        sandbox.delete()
    except Exception as exc:
        print(f"[eye_session] 샌드박스 삭제 실패: {type(exc).__name__}: {exc}")


def _check_in_sandbox(session: dict, jpeg: bytes) -> dict:
    sandbox, work_dir = session["sandbox"], session["work_dir"]
    sandbox.fs.upload_file(jpeg, f"{work_dir}/frame.jpg")
    run = sandbox.process.exec(f"cd {shlex.quote(work_dir)} && python frame_job.py frame.jpg prev.jpg",
                               timeout=FRAME_TIMEOUT_SECONDS)
    return _parse_result(run.result or "")


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
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        return {"usable": raw.get("usable") is True, "changed": raw.get("changed") is not False,
                "hints": hints, "luma": number("luma"), "sharpness": number("sharpness"),
                "change": number("change")}
    print(f"[eye_session] 프레임 점검 실패: {output.strip()[-300:]}")
    raise RuntimeError("프레임을 점검하지 못했습니다.")
