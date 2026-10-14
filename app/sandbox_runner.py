"""Daytona 샌드박스 연동.

역할 분담이 이 서비스의 보안 설계다.
  서버   — 바이트를 가져오기만 한다 (SSRF 검사 후 다운로드). 해석하지 않는다.
  샌드박스 — 남의 HTML·이미지를 해석한다. 인터넷이 차단된 일회용 컴퓨터 안에서.

이미지 디코더(Pillow·libjpeg)는 취약점 이력이 길고, 압축 폭탄 한 장으로도 프로세스를
무너뜨릴 수 있다. 그 일을 우리 서버에서 하지 않는다. 요청 1건 = 샌드박스 1대, 끝나면 삭제.
샌드박스를 못 만들면(키 만료·장애) 서버 안 격리 프로세스로 대체한다 — app/job_box.py.
"""
import base64
import json
import re
import time
from pathlib import Path

from app import fetcher, job_box

JOB_SOURCE = Path(__file__).resolve().parent / "sandbox_job.py"
JOB_TIMEOUT_SECONDS = 120

_client = job_box.make_client   # eye_session 이 쓰는 이름 유지

# 상자 안에서 만들어진 파일은 신뢰할 수 없는 해석기의 산출물이다. 이름·개수·크기·값을 모두 검증한다.
TILE_NAME = re.compile(r"tile_\d{2}\.jpg")
MAX_TILES = 10
MAX_WANTED = 12
MAX_JSON_BYTES = 200_000
MAX_TILE_BYTES = 3_000_000
GENERIC_FAIL = "페이지를 해석하지 못했습니다."


def _read_json(box, path: str) -> dict:
    raw = box.download(path)
    if len(raw) > MAX_JSON_BYTES:
        raise RuntimeError(GENERIC_FAIL)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise RuntimeError(GENERIC_FAIL)
    return data


def _clean_page(raw: dict, url: str) -> dict:
    """result.json 에서 쓰는 값만, 정해진 형태로 꺼낸다."""
    def text(key: str, limit: int) -> str:
        value = raw.get(key, "")
        return value[:limit] if isinstance(value, str) else ""

    def count(key: str) -> int:
        value = raw.get(key, 0)
        return value if isinstance(value, int) and 0 <= value <= 10_000 else 0

    wanted = raw.get("wanted", [])
    wanted = [w for w in wanted if isinstance(w, str) and w.startswith(("http://", "https://")) and len(w) <= 2000] \
        if isinstance(wanted, list) else []
    return {"url": url, "title": text("title", 300), "description": text("description", 1000),
            "text": text("text", 6000), "image_count": count("image_count"),
            "image_alts_missing": count("image_alts_missing"), "wanted": wanted[:MAX_WANTED]}


def _clean_tiles(raw: dict) -> tuple[list[str], list[str]]:
    names = raw.get("tiles", [])
    names = [n for n in names if isinstance(n, str) and TILE_NAME.fullmatch(n)] if isinstance(names, list) else []
    errors = raw.get("errors", [])
    errors = [e[:200] for e in errors if isinstance(e, str)][:20] if isinstance(errors, list) else []
    return list(dict.fromkeys(names))[:MAX_TILES], errors


def _read_tile(box, out: str, name: str) -> bytes:
    if box.where == "local":   # 이름 검증에 더해, 실제 경로가 작업 폴더 밖(심볼릭 링크 포함)이면 거부
        path = Path(out) / name
        if path.is_symlink() or not path.resolve().is_relative_to(Path(out).resolve()):
            raise RuntimeError(GENERIC_FAIL)
    data = box.download(f"{out}/{name}")
    if len(data) > MAX_TILE_BYTES or not data.startswith(bytes.fromhex("ffd8ff")):
        raise RuntimeError(GENERIC_FAIL)
    return data


def _run(box, args: list[str], marker: str) -> str:
    run = box.run_job(args, JOB_TIMEOUT_SECONDS)
    if run.exit_code != 0 or marker not in run.result:
        print(f"[sandbox_runner] 작업 실패 ({box.where}, 코드 {run.exit_code}): {run.result.strip()[-500:]}")
        raise RuntimeError(GENERIC_FAIL)   # 자식 출력(임시 경로 등)을 화면에 보내지 않는다
    return run.result


def collect_page(url: str):
    """진행 이벤트를 차례로 내보내고, 마지막에 수집 결과(dict)를 내보낸다."""
    started = time.time()

    yield {"type": "progress", "message": "페이지를 내려받고 있습니다."}
    html = fetcher.fetch_bytes(url)

    yield {"type": "progress", "message": "안전한 샌드박스 컴퓨터를 준비하고 있습니다."}
    box = job_box.open_box()
    try:
        home = box.home
        job, out, imgs = f"{home}/job.py", f"{home}/out", f"{home}/imgs"
        box.mkdirs(out, imgs)
        box.upload(JOB_SOURCE.read_bytes(), job)
        box.upload(html, f"{home}/page.html")

        yield {"type": "progress", "message": "격리된 곳에서 페이지를 해석하고 있습니다."}
        _run(box, [job, "parse", f"{home}/page.html", url, out], "PARSE_OK")
        page = _clean_page(_read_json(box, f"{out}/result.json"), url)

        yield {"type": "progress", "message": f"상세 이미지 {len(page['wanted'])}장을 내려받고 있습니다."}
        downloaded, errors = 0, []
        for i, src in enumerate(page["wanted"]):
            try:
                data = fetcher.fetch_bytes(src, referer=url, limit=fetcher.MAX_IMAGE_BYTES)
                box.upload(data, f"{imgs}/img_{i:02d}.bin")
                downloaded += 1
            except Exception as exc:  # 이미지 1장 실패가 전체를 막지 않게 한다
                errors.append(f"{src[:60]}: {exc}")

        yield {"type": "progress", "message": "격리된 곳에서 이미지를 읽기 좋게 자르고 있습니다."}
        _run(box, [job, "tile", imgs, out], "TILE_OK")
        tile_names, tile_errors = _clean_tiles(_read_json(box, f"{out}/tiles.json"))

        yield {
            **page,
            "type": "page",
            "errors": errors + tile_errors,
            "downloaded": downloaded,
            "tiles_b64": [base64.b64encode(_read_tile(box, out, name)).decode("ascii") for name in tile_names],
            "sandbox_id": box.id,
            "where": box.where,
            "sandbox_seconds": round(time.time() - started, 1),
        }
    finally:
        box.close()
