"""서버 쪽 다운로드 — 바이트만 가져오고, 절대 해석(파싱·디코딩)하지 않는다.

해석은 전부 Daytona 샌드박스가 맡는다. 여기서 하는 일은 SSRF 차단과 바이트 수집뿐이다.
"""
import ipaddress
import socket
import time
from urllib.parse import quote, urlparse

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MAX_HTML_BYTES = 3_000_000
MAX_IMAGE_BYTES = 12_000_000
TIMEOUT = 20
TOTAL_SECONDS = 30   # 한 번 내려받기 전체 시간 상한 — 아주 느리게 흘려보내는 서버로 스레드·메모리를 붙잡지 못하게


class UnsafeUrlError(ValueError):
    """사설망·루프백 등 내부 주소를 가리키는 URL."""


def to_ascii_url(url: str) -> str:
    """한글이 섞인 주소도 열 수 있게 ASCII 로 인코딩한다 (이미 인코딩된 % 는 보존)."""
    return quote(url, safe=":/?#[]@!$&'()*+,;=%~")


MAX_REDIRECTS = 5


def _is_public(ip) -> bool:
    return ip.is_global and not ip.is_multicast


def assert_public_url(url: str) -> None:
    """SSRF 차단 — 주소가 내부망을 가리키면 거부한다. 리다이렉트마다 다시 부른다."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeUrlError("http 또는 https 주소만 열 수 있습니다.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError("주소를 찾을 수 없습니다.") from exc
    for info in infos:
        if not _is_public(ipaddress.ip_address(info[4][0])):
            raise UnsafeUrlError("내부 네트워크 주소는 열 수 없습니다.")


def _assert_public_peer(res: httpx.Response, strict: bool) -> None:
    """실제로 연결된 상대 주소도 확인한다(검사와 연결 사이에 DNS 가 바뀌는 공격 대비). 확인할 수 없으면 막는다."""
    stream = res.extensions.get("network_stream")
    peer = stream.get_extra_info("server_addr") if stream is not None else None
    if not peer:
        if strict:
            raise UnsafeUrlError("연결 상대를 확인할 수 없습니다.")
        return
    if not _is_public(ipaddress.ip_address(peer[0])):
        raise UnsafeUrlError("내부 네트워크 주소는 열 수 없습니다.")


def fetch_bytes(url: str, referer: str | None = None, limit: int = MAX_HTML_BYTES,
                transport: httpx.BaseTransport | None = None, deadline: float | None = None) -> bytes:
    """deadline 은 time.monotonic() 기준 절대 시각. 없으면 지금부터 TOTAL_SECONDS."""
    deadline = min(deadline or float("inf"), time.monotonic() + TOTAL_SECONDS)
    headers = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
    if referer:
        headers["Referer"] = to_ascii_url(referer)
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=transport) as client:
        current = to_ascii_url(url)
        for _hop in range(MAX_REDIRECTS + 1):
            if time.monotonic() > deadline:
                raise UnsafeUrlError("내려받는 데 시간이 너무 오래 걸립니다.")
            assert_public_url(current)
            with client.stream("GET", current, headers=headers) as res:
                _assert_public_peer(res, strict=transport is None)
                if res.is_redirect:
                    location = res.headers.get("location", "")
                    current = to_ascii_url(str(res.url.join(location)))
                    continue
                res.raise_for_status()
                chunks, total = [], 0
                for chunk in res.iter_bytes():
                    if time.monotonic() > deadline:
                        raise UnsafeUrlError("내려받는 데 시간이 너무 오래 걸립니다.")
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= limit:
                        break
                return b"".join(chunks)[:limit]
    raise UnsafeUrlError("리다이렉트가 너무 많습니다.")
