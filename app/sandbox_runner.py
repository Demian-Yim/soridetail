"""Daytona 샌드박스 연동 — 모르는 웹페이지는 우리 서버가 아니라 일회용 샌드박스에서 연다.

요청 1건 = 샌드박스 1대. 끝나면 반드시 삭제한다.
"""
import base64
import json
import os
import shlex
import time
from pathlib import Path

from daytona import Daytona, DaytonaConfig

JOB_SOURCE = Path(__file__).resolve().parent / "sandbox_job.py"
JOB_TIMEOUT_SECONDS = 120


def _client() -> Daytona:
    api_key = os.getenv("DAYTONA_API_KEY")
    if not api_key:
        raise RuntimeError("DAYTONA_API_KEY 가 없습니다. .env 파일에 키를 넣어 주세요.")
    return Daytona(DaytonaConfig(api_key=api_key))


def collect_page(url: str):
    """진행 이벤트를 차례로 내보내고, 마지막에 수집 결과(dict)를 내보낸다."""
    started = time.time()
    yield {"type": "progress", "message": "안전한 샌드박스 컴퓨터를 준비하고 있습니다."}
    daytona = _client()
    sandbox = daytona.create()
    try:
        work_dir = sandbox.get_user_home_dir().rstrip("/") + "/sori"
        job_path = f"{work_dir}/sandbox_job.py"
        out_dir = f"{work_dir}/out"
        sandbox.process.exec(f"mkdir -p {shlex.quote(out_dir)}")
        sandbox.fs.upload_file(JOB_SOURCE.read_bytes(), job_path)

        yield {"type": "progress", "message": "샌드박스 안에서 페이지를 열고 이미지를 모으고 있습니다."}
        command = (
            "python -c 'import PIL' 2>/dev/null || pip install -q pillow; "
            f"python {shlex.quote(job_path)} {shlex.quote(url)} {shlex.quote(out_dir)}"
        )
        run = sandbox.process.exec(command, timeout=JOB_TIMEOUT_SECONDS)
        if run.exit_code != 0 or "JOB_OK" not in (run.result or ""):
            raise RuntimeError(f"페이지를 열지 못했습니다: {(run.result or '').strip()[-300:]}")

        page = json.loads(sandbox.fs.download_file(f"{out_dir}/result.json").decode("utf-8"))
        yield {"type": "progress", "message": f"이미지 조각 {len(page['tiles'])}장을 가져오고 있습니다."}
        tiles_b64 = [
            base64.b64encode(sandbox.fs.download_file(f"{out_dir}/{name}")).decode("ascii")
            for name in page["tiles"]
        ]
        yield {
            **page,
            "type": "page",
            "tiles_b64": tiles_b64,
            "sandbox_id": sandbox.id,
            "sandbox_seconds": round(time.time() - started, 1),
        }
    finally:
        sandbox.delete()
