"""대체 상자(서버 안 격리 프로세스) — 키가 없어도 소리상세가 끝까지 돈다는 회귀 테스트."""
import io

from PIL import Image

from app import fetcher, job_box, sandbox_runner

PROBE = (
    "import os\n"
    "print('HAS_DAYTONA=%s' % (os.getenv('DAYTONA_API_KEY') is not None))\n"
    "print('HAS_GEMINI=%s' % (os.getenv('GEMINI_API_KEY') is not None))\n"
)


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (240, 240, 240)).save(buf, "PNG")
    return buf.getvalue()


def test_local_box_hides_secrets_from_child(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "dtn_test_placeholder")
    monkeypatch.setenv("GEMINI_API_KEY", "test_placeholder")
    box = job_box.LocalBox()
    try:
        box.upload(PROBE.encode(), f"{box.home}/probe.py")
        run = box.run_job([f"{box.home}/probe.py"], 30)
        assert run.exit_code == 0, run.result
        assert "HAS_DAYTONA=False" in run.result and "HAS_GEMINI=False" in run.result
    finally:
        box.close()


def test_collect_page_falls_back_to_local_box(monkeypatch):
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    html = ("<html><head><title>T</title></head><body><p>" + "가격 9,900원 " * 5 +
            '</p><img src="/detail_01.png" alt=""></body></html>').encode("utf-8")
    image = _png(900, 2000)
    monkeypatch.setattr(fetcher, "fetch_bytes",
                        lambda url, referer=None, limit=None: html if url.endswith("/p/1") else image)
    steps = list(sandbox_runner.collect_page("https://shop.example.com/p/1"))
    page = steps[-1]
    assert page["type"] == "page" and page["where"] == "local"
    assert page["downloaded"] == 1 and len(page["tiles_b64"]) >= 1
    assert page["sandbox_id"].startswith("local-")


def test_tile_names_from_child_cannot_escape_out_dir():
    names, _ = sandbox_runner._clean_tiles(
        {"tiles": ["../../../../proc/self/environ", "/etc/passwd", "tile_00.jpg", "tile_00.jpg", "tile_1.jpg"],
         "errors": ["x" * 500]})
    assert names == ["tile_00.jpg"]


def test_page_fields_from_child_are_whitelisted_and_capped():
    page = sandbox_runner._clean_page(
        {"title": "T", "wanted": ["https://a/%d.png" % i for i in range(50)] + ["file:///etc/passwd", 7],
         "image_count": "9", "tiles_b64": ["forged"], "sandbox_id": "forged"}, "https://shop/p")
    assert len(page["wanted"]) == 12 and all(w.startswith("https://") for w in page["wanted"])
    assert page["image_count"] == 0 and "tiles_b64" not in page and "sandbox_id" not in page


def test_local_box_caps_output_and_times_out():
    box = job_box.LocalBox()
    try:
        box.upload(b"import sys\nsys.stdout.write('A' * 5_000_000)\n", f"{box.home}/flood.py")
        flood = box.run_job([f"{box.home}/flood.py"], 30)
        assert len(flood.result) <= job_box.OUTPUT_READ_BYTES
        box.upload(b"import time\ntime.sleep(30)\n", f"{box.home}/slow.py")
        slow = box.run_job([f"{box.home}/slow.py"], 2)
        assert slow.exit_code == 124
    finally:
        root = box.root
        box.close()
    assert not root.exists()


def _jpeg(shade: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (shade, shade, shade)).save(buf, "JPEG")
    return buf.getvalue()


def test_camera_frames_are_never_decoded_in_the_server_process(monkeypatch):
    from app import eye_session, frame_job

    def forbidden(*_args, **_kwargs):
        raise AssertionError("서버 본체에서 프레임을 디코딩했다")

    monkeypatch.setattr(frame_job, "analyze", forbidden)
    result = eye_session.check_frame("attacker-chosen-id", _jpeg(128))
    assert result["where"] == "local" and result["usable"] is True and result["sandbox_id"] == ""


def test_local_session_keeps_previous_frame_in_memory_only(monkeypatch):
    from app import eye_session

    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    info = eye_session.start()
    try:
        first = eye_session.check_frame(info["session_id"], _jpeg(120))
        second = eye_session.check_frame(info["session_id"], _jpeg(120))
        assert first["changed"] is True and second["changed"] is False
    finally:
        assert eye_session.end(info["session_id"]) is True


def test_local_box_download_rejects_outside_and_oversized_files(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    box = job_box.LocalBox()
    try:
        box.upload(b"y" * 100, f"{box.home}/big.bin")
        try:
            box.download(str(outside), 10)
            raise AssertionError("상자 밖 파일을 읽었다")
        except ValueError:
            pass
        try:
            box.download(f"{box.home}/big.bin", 10)
            raise AssertionError("상한보다 큰 파일을 읽었다")
        except ValueError:
            pass
        assert box.download(f"{box.home}/big.bin", 100) == b"y" * 100
    finally:
        box.close()


def test_client_ip_uses_the_hop_cloud_run_appends():
    from types import SimpleNamespace

    from app import limits

    request = SimpleNamespace(headers={"x-forwarded-for": "1.2.3.4, 203.0.113.9"}, client=None)
    assert limits.client_ip(request) == "203.0.113.9"
