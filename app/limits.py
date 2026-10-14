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
_hops_logged = False


def _is_private(value: str) -> bool:
    import ipaddress
    try:
        return ipaddress.ip_address(value).is_private
    except ValueError:
        return False
_recent: dict[tuple[str, str], deque] = defaultdict(deque)   # (이름, ip) → 최근 1분의 시각들
_daily: dict[tuple[str, str], list] = {}                       # (이름, ip 또는 "*") → [날짜 시작 시각, 횟수]


def limits_for(name: str) -> tuple[int, int, int]:
    per_min, per_day, total = DEFAULTS[name]
    key = f"LIMIT_{name.upper()}"
    return (int(os.getenv(f"{key}_PER_MIN", per_min)), int(os.getenv(f"{key}_PER_DAY", per_day)),
            int(os.getenv(f"{key}_TOTAL_PER_DAY", total)))


def client_ip(request: Request) -> str:
    """Cloud Run 은 실제 접속 주소를 X-Forwarded-For 맨 뒤에 붙인다. 앞쪽 값은 사용자가 꾸며 넣을 수 있다."""
    global _hops_logged
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        hops = [h.strip() for h in forwarded.split(",")]
        if not _hops_logged:   # 배포 환경의 헤더 모양을 한 번만 기록한다(주소 자체는 남기지 않는다)
            _hops_logged = True
            print(f"[limits] x-forwarded-for hops={len(hops)} last_private={_is_private(hops[-1])}")
        return _bucket(hops[-1][:64])
    return _bucket(request.client.host) if request.client else "unknown"


def _bucket(value: str) -> str:
    """IPv6 는 한 사람이 /64 안에서 주소를 바꿔 가며 상한을 피할 수 있어 /64 단위로 센다."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return value
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


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
        if len(_daily) > MAX_KEYS:
            _evict(now)


MAX_KEYS = 20_000


def _evict(now: float) -> None:
    """하루 지난 기록·빈 기록을 버린다(주소가 계속 바뀌어도 메모리가 늘지 않게)."""
    for key in [k for k, (since, _n) in _daily.items() if now - since >= DAY and k[1] != "*"]:
        del _daily[key]
    for key in [k for k, q in _recent.items() if not q or now - q[-1] >= MINUTE]:
        del _recent[key]


def reset() -> None:
    """테스트용."""
    with _lock:
        _recent.clear()
        _daily.clear()
