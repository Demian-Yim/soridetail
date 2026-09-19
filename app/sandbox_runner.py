"""Daytona 샌드박스 연동.

역할 분담이 이 서비스의 보안 설계다.
  서버   — 바이트를 가져오기만 한다 (SSRF 검사 후 다운로드). 해석하지 않는다.
  샌드박스 — 남의 HTML·이미지를 해석한다. 인터넷이 차단된 일회용 컴퓨터 안에서.

이미지 디코더(Pillow·libjpeg)는 취약점 이력이 길고, 압축 폭탄 한 장으로도 프로세스를
무너뜨릴 수 있다. 그 일을 우리 서버에서 하지 않는다. 요청 1건 = 샌드박스 1대, 끝나면 삭제.
"""
import base64
import json
import os
import shlex
import time
from pathlib import Path

from daytona import Daytona, DaytonaConfig

from app import fetcher

JOB_SOURCE = Path(__file__).resolve().parent / "sandbox_job.py"
JOB_TIMEOUT_SECONDS = 120


def _client() -> Daytona:
    api_key = os.getenv("DAYTONA_API_KEY")
    if not api_key:
        raise RuntimeError("DAYTONA_API_KEY 가 없습니다. .env 파일에 키를 넣어 주세요.")
    return Daytona(DaytonaConfig(api_key=api_key))


def _run(sandbox, command: str, marker: str) -> str:
    run = sandbox.process.exec(command, timeout=JOB_TIMEOUT_SECONDS)
    if run.exit_code != 0 or marker not in (run.result or ""):
        raise RuntimeError((run.result or "").strip()[-300:] or "샌드박스 작업이 실패했습니다.")
    return run.result


def collect_page(url: str):
    """진행 이벤트를 차례로 내보내고, 마지막에 수집 결과(dict)를 내보낸다."""
    started = time.time()

    yield {"type": "progress", "message": "페이지를 내려받고 있습니다."}
    html = fetcher.fetch_bytes(url)

    yield {"type": "progress", "message": "안전한 샌드박스 컴퓨터를 준비하고 있습니다."}
    sandbox = _client().create()
    try:
        home = sandbox.get_user_home_dir().rstrip("/") + "/sori"
        job, out, imgs = f"{home}/job.py", f"{home}/out", f"{home}/imgs"
        sandbox.process.exec(f"mkdir -p {shlex.quote(out)} {shlex.quote(imgs)}")
        sandbox.fs.upload_file(JOB_SOURCE.read_bytes(), job)
        sandbox.fs.upload_file(html, f"{home}/page.html")

        yield {"type": "progress", "message": "샌드박스 안에서 페이지를 해석하고 있습니다."}
        _run(sandbox, f"python {shlex.quote(job)} parse {shlex.quote(home + '/page.html')} "
                      f"{shlex.quote(url)} {shlex.quote(out)}", "PARSE_OK")
        page = json.loads(sandbox.fs.download_file(f"{out}/result.json").decode("utf-8"))

        yield {"type": "progress", "message": f"상세 이미지 {len(page['wanted'])}장을 내려받고 있습니다."}
        downloaded, errors = 0, []
        for i, src in enumerate(page["wanted"]):
            try:
                data = fetcher.fetch_bytes(src, referer=url, limit=fetcher.MAX_IMAGE_BYTES)
                sandbox.fs.upload_file(data, f"{imgs}/img_{i:02d}.bin")
                downloaded += 1
            except Exception as exc:  # 이미지 1장 실패가 전체를 막지 않게 한다
                errors.append(f"{src[:60]}: {exc}")

        yield {"type": "progress", "message": "샌드박스 안에서 이미지를 읽기 좋게 자르고 있습니다."}
        _run(sandbox, f"python {shlex.quote(job)} tile {shlex.quote(imgs)} {shlex.quote(out)}", "TILE_OK")
        tiles = json.loads(sandbox.fs.download_file(f"{out}/tiles.json").decode("utf-8"))

        yield {
            **page,
            "type": "page",
            "errors": errors + tiles["errors"],
            "downloaded": downloaded,
            "tiles_b64": [
                base64.b64encode(sandbox.fs.download_file(f"{out}/{name}")).decode("ascii")
                for name in tiles["tiles"]
            ],
            "sandbox_id": sandbox.id,
            "sandbox_seconds": round(time.time() - started, 1),
        }
    finally:
        sandbox.delete()
