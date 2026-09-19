"""호출 상한 — 공개 주소에서 남의 호출로 API 키 요금이 새지 않게 막는 마지막 울타리.

로그인이 없는 시범 서비스라 누구나 주소만 알면 부를 수 있다. 그래서
  ① 접속 주소(IP)별 분당·하루 상한  ② 서비스 전체의 하루 상한  두 겹을 둔다.
상한은 메모리에만 둔다 — 서버가 1대(Cloud Run max-instances=1)일 때 정확하고, 재시작하면 초기화된다.
값은 환경변수로 바꿀 수 있다: LIMIT_<이름>_PER_MIN / _PER_DAY / _TOTAL_PER_DAY
"""
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

MINUTE, DAY = 60.0, 86400.0
# (분당/IP, 하루/IP, 하루/전체) — 혼자 쓰는 데는 넉넉하고, 악용하기엔 빠듯하게
DEFAULTS = {
    "look": (20, 400, 3000),    # 장면 판독 = 비전 AI 호출 (비용의 대부분)
    "start": (6, 60, 600),      # 세션 시작 = 샌드박스 생성
    "read": (4, 40, 300),       # 상세페이지 읽기 = 샌드박스 + 비전 AI 여러 장
    "ask": (20, 300, 3000),
    "tts": (120, 3000, 40000),  # 음성 합성 = 서버 CPU
}
MESSAGES = {
    "minute": "요청이 너무 잦습니다. 잠시 뒤 다시 눌러 주세요.",
    "day": "오늘 사용할 수 있는 횟수를 모두 썼습니다. 내일 다시 이용해 주세요.",
    "total": "오늘은 이용자가 많아 서비스가 하루 한도에 도달했습니다. 내일 다시 이용해 주세요.",
}

_lock = threading.Lock()
_recent: dict[tuple[str, str], deque] = defaultdict(deque)   # (이름, ip) → 최근 1분의 시각들
_daily: dict[tuple[str, str], list] = {}                       # (이름, ip 또는 "*") → [날짜 시작 시각, 횟수]


def limits_for(name: str) -> tuple[int, int, int]:
    per_min, per_day, total = DEFAULTS[name]
    key = f"LIMIT_{name.upper()}"
    return (int(os.getenv(f"{key}_PER_MIN", per_min)), int(os.getenv(f"{key}_PER_DAY", per_day)),
            int(os.getenv(f"{key}_TOTAL_PER_DAY", total)))


def client_ip(request: Request) -> str:
    """Cloud Run·터널 뒤에서는 실제 접속 주소가 X-Forwarded-For 의 첫 값에 온다."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def _daily_count(key: tuple[str, str], now: float) -> list:
    entry = _daily.get(key)
    if entry is None or now - entry[0] >= DAY:
        entry = [now, 0]
        _daily[key] = entry
    return entry


def check(name: str, request: Request, now: float | None = None) -> None:
    """상한을 넘으면 429 를 던지고, 아니면 1회를 기록한다."""
    now = time.time() if now is None else now
    per_min, per_day, total = limits_for(name)
    ip = client_ip(request)
    with _lock:
        recent = _recent[(name, ip)]
        while recent and now - recent[0] >= MINUTE:
            recent.popleft()
        mine, everyone = _daily_count((name, ip), now), _daily_count((name, "*"), now)
        if len(recent) >= per_min:
            raise HTTPException(status_code=429, detail=MESSAGES["minute"])
        if mine[1] >= per_day:
            raise HTTPException(status_code=429, detail=MESSAGES["day"])
        if everyone[1] >= total:
            raise HTTPException(status_code=429, detail=MESSAGES["total"])
        recent.append(now)
        mine[1] += 1
        everyone[1] += 1


def reset() -> None:
    """테스트용."""
    with _lock:
        _recent.clear()
        _daily.clear()
