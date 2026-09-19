"""서버 쪽 다운로드 — 바이트만 가져오고, 절대 해석(파싱·디코딩)하지 않는다.

해석은 전부 Daytona 샌드박스가 맡는다. 여기서 하는 일은 SSRF 차단과 바이트 수집뿐이다.
"""
import ipaddress
import socket
from urllib.parse import quote, urlparse

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MAX_HTML_BYTES = 3_000_000
MAX_IMAGE_BYTES = 12_000_000
TIMEOUT = 20


class UnsafeUrlError(ValueError):
    """사설망·루프백 등 내부 주소를 가리키는 URL."""


def to_ascii_url(url: str) -> str:
    """한글이 섞인 주소도 열 수 있게 ASCII 로 인코딩한다 (이미 인코딩된 % 는 보존)."""
    return quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def assert_public_url(url: str) -> None:
    """SSRF 차단 — 사용자가 넣은 주소가 내부망을 가리키면 거부한다."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeUrlError("http 또는 https 주소만 열 수 있습니다.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError("주소를 찾을 수 없습니다.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise UnsafeUrlError("내부 네트워크 주소는 열 수 없습니다.")


def fetch_bytes(url: str, referer: str | None = None, limit: int = MAX_HTML_BYTES) -> bytes:
    assert_public_url(url)
    headers = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
    if referer:
        headers["Referer"] = to_ascii_url(referer)
    with httpx.stream("GET", to_ascii_url(url), headers=headers, timeout=TIMEOUT,
                      follow_redirects=True) as res:
        res.raise_for_status()
        chunks, total = [], 0
        for chunk in res.iter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= limit:
                break
        return b"".join(chunks)[:limit]
