"""네트워크·API 키 없이 돌아가는 핵심 로직 테스트."""
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import sandbox_job  # noqa: E402
from app import fetcher, main, vision  # noqa: E402

HTML = """<html><head><title>독도 토너</title>
<meta property="og:title" content="1025 독도 토너 200ml">
<script>var x = "무시";</script></head>
<body><nav>메뉴 무시</nav><p>가격 17,000원</p>
<img src="/img/detail_01.jpg"><img data-src="https://cdn.example.com/a.jpg" alt="제품 사진">
<img src="/img/logo.png"><img src="data:image/png;base64,xx"><img src="/img/detail_01.jpg"></body></html>"""


def test_parser_extracts_text_meta_and_skips_script_nav():
    parser = sandbox_job.PageParser()
    parser.feed(HTML)
    assert parser.meta["og:title"] == "1025 독도 토너 200ml"
    assert "가격 17,000원" in parser.texts
    assert not any("무시" in t for t in parser.texts)


def test_pick_images_resolves_dedupes_and_filters():
    parser = sandbox_job.PageParser()
    parser.feed(HTML)
    picked = sandbox_job.pick_images(parser.images, "https://shop.example.com/product/1")
    assert [p["src"] for p in picked] == [
        "https://shop.example.com/img/detail_01.jpg",
        "https://cdn.example.com/a.jpg",
    ]
    assert picked[1]["alt"] == "제품 사진"


def test_to_ascii_url_encodes_korean_and_keeps_existing_percent():
    out = fetcher.to_ascii_url("https://a.com/상품/%EC%A0%90?q=1")
    assert out.isascii() and "%EC%A0%90" in out and "%25" not in out and out.endswith("?q=1")


def test_slice_image_tiles_tall_image_and_respects_cap(tmp_path):
    src = tmp_path / "tall.png"
    Image.new("RGB", (2000, 9000), "white").save(src)
    tiles = sandbox_job.slice_image(str(src), str(tmp_path), 0)
    # 2000x9000 → 1000x4500 으로 축소 → 1400 높이 타일 4장
    assert tiles == ["tile_00.jpg", "tile_01.jpg", "tile_02.jpg", "tile_03.jpg"]
    with Image.open(tmp_path / "tile_00.jpg") as im:
        assert im.size == (1000, 1400)
    assert sandbox_job.slice_image(str(src), str(tmp_path), sandbox_job.MAX_TILES) == []


def test_slice_image_skips_small_icons(tmp_path):
    src = tmp_path / "icon.png"
    Image.new("RGB", (120, 120), "white").save(src)
    assert sandbox_job.slice_image(str(src), str(tmp_path), 0) == []


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "javascript:alert(1)", "ftp://a.com/x", "https://"])
def test_validate_url_rejects_non_http(bad):
    with pytest.raises(HTTPException):
        main.validate_url(bad)


def test_validate_url_accepts_https_and_event_is_ndjson():
    assert main.validate_url(" https://a.com/x ") == "https://a.com/x"
    line = main.event("progress", message="준비")
    assert line.endswith("\n") and json.loads(line) == {"type": "progress", "message": "준비"}


def test_vision_provider_priority(monkeypatch):
    for key in ("VISION_BASE_URL", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    assert vision.provider_name() == "none"
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert vision.provider_name() == "gemini:auto"  # 잔액 없는 OpenAI 보다 Gemini 우선


@pytest.mark.parametrize("internal", [
    "http://127.0.0.1:8000/admin", "http://localhost/x", "http://169.254.169.254/latest/meta-data/",
])
def test_fetcher_blocks_ssrf_targets(internal):
    """서버가 직접 받아오므로 내부망 주소 차단이 필수다."""
    with pytest.raises(fetcher.UnsafeUrlError):
        fetcher.assert_public_url(internal)


def test_sandbox_job_has_no_network_code():
    """샌드박스 스크립트는 네트워크를 쓰지 않아야 한다 (해석 전용)."""
    source = (ROOT / "app" / "sandbox_job.py").read_text(encoding="utf-8")
    for banned in ("urllib.request", "httpx", "socket", "requests"):
        assert banned not in source


def test_parse_mode_lists_wanted_images(tmp_path):
    html = tmp_path / "p.html"
    html.write_bytes(HTML.encode("utf-8"))
    out = tmp_path / "out"
    sandbox_job.parse_mode(str(html), "https://shop.example.com/product/1", str(out))
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result["title"] == "1025 독도 토너 200ml"
    assert result["wanted"] == [
        "https://shop.example.com/img/detail_01.jpg",
        "https://cdn.example.com/a.jpg",
    ]
    assert result["image_alts_missing"] == 1


def test_tile_mode_dedupes_identical_images(tmp_path):
    imgs, out = tmp_path / "imgs", tmp_path / "out"
    imgs.mkdir()
    Image.new("RGB", (1000, 1400), "white").save(imgs / "img_00.bin", "PNG")
    Image.new("RGB", (1000, 1400), "white").save(imgs / "img_01.bin", "PNG")  # 같은 내용
    sandbox_job.tile_mode(str(imgs), str(out))
    tiles = json.loads((out / "tiles.json").read_text(encoding="utf-8"))
    assert tiles["tiles"] == ["tile_00.jpg"] and tiles["errors"] == []
