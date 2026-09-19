"""Daytona 샌드박스 안에서 실행되는 수집 스크립트.

모르는 웹페이지를 우리 서버가 아니라 격리된 샌드박스에서 열어
① 본문 텍스트 ② 상세 이미지를 내려받고 ③ 세로로 긴 이미지를 읽기 좋은 타일로 자른다.
결과는 OUT_DIR/result.json + tile_*.jpg 로 남긴다.

사용: python sandbox_job.py <url> <out_dir>
"""
import hashlib
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MAX_HTML_BYTES = 3_000_000
MAX_IMAGE_BYTES = 12_000_000
MAX_CANDIDATES = 12
MAX_TILES = 10
TILE_WIDTH = 1000
TILE_HEIGHT = 1400
MIN_IMAGE_SIDE = 300
MAX_TEXT_CHARS = 6000
SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "footer", "header"}
SKIP_IMAGE_HINTS = ("icon", "logo", "sprite", "banner", "btn", "badge", "blank", "pixel")


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta = {}
        self.texts = []
        self.images = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SKIP_TAGS and tag != "head":
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key and a.get("content"):
                self.meta[key.lower()] = a["content"].strip()
        if tag == "img":
            src = (a.get("data-src") or a.get("data-original") or a.get("data-lazy-src")
                   or a.get("src") or "")
            if src and not src.startswith("data:"):
                self.images.append({"src": src, "alt": (a.get("alt") or "").strip()})

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and tag != "head" and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
        elif self._skip_depth == 0 and len(text) > 1:
            self.texts.append(text)


def to_ascii_url(url):
    """한글이 섞인 주소도 열 수 있게 ASCII 로 인코딩한다 (이미 인코딩된 % 는 보존)."""
    return quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def fetch(url, referer=None, limit=MAX_HTML_BYTES):
    headers = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
    if referer:
        headers["Referer"] = to_ascii_url(referer)
    req = urllib.request.Request(to_ascii_url(url), headers=headers)
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read(limit), res.headers.get_content_charset()


def decode_html(raw, charset):
    for enc in (charset, "utf-8", "euc-kr", "cp949"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def pick_images(images, base_url):
    seen, picked = set(), []
    for img in images:
        full = urljoin(base_url, img["src"])
        if urlparse(full).scheme not in ("http", "https") or full in seen:
            continue
        seen.add(full)
        if any(h in full.lower() for h in SKIP_IMAGE_HINTS) or full.lower().endswith((".gif", ".svg")):
            continue
        picked.append({"src": full, "alt": img["alt"]})
    return picked


def slice_image(path, out_dir, start_index):
    """세로로 긴 상세 이미지를 비전 모델이 글자를 읽을 수 있는 크기의 타일로 자른다."""
    from PIL import Image

    tiles = []
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        if min(w, h) < MIN_IMAGE_SIDE:
            return tiles
        if w > TILE_WIDTH:
            h = int(h * TILE_WIDTH / w)
            im = im.resize((TILE_WIDTH, h))
            w = TILE_WIDTH
        top = 0
        while top < h and start_index + len(tiles) < MAX_TILES:
            box = (0, top, w, min(top + TILE_HEIGHT, h))
            name = "tile_%02d.jpg" % (start_index + len(tiles))
            im.crop(box).save(os.path.join(out_dir, name), "JPEG", quality=80)
            tiles.append(name)
            top += TILE_HEIGHT
    return tiles


def main(url, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    result = {"url": url, "title": "", "description": "", "text": "",
              "image_alts_missing": 0, "image_count": 0, "tiles": [], "errors": []}

    raw, charset = fetch(url)
    parser = PageParser()
    parser.feed(decode_html(raw, charset))

    result["title"] = parser.meta.get("og:title") or parser.title
    result["description"] = parser.meta.get("og:description") or parser.meta.get("description", "")
    result["text"] = re.sub(r"\s+", " ", " ".join(parser.texts))[:MAX_TEXT_CHARS]

    candidates = pick_images(parser.images, url)
    og_image = parser.meta.get("og:image")
    if og_image:
        candidates.insert(0, {"src": urljoin(url, og_image), "alt": ""})
    result["image_count"] = len(candidates)
    result["image_alts_missing"] = sum(1 for c in candidates if not c["alt"])

    seen_digests = set()
    for i, cand in enumerate(candidates[:MAX_CANDIDATES]):
        if len(result["tiles"]) >= MAX_TILES:
            break
        tmp = os.path.join(out_dir, "src_%02d.bin" % i)
        try:
            data, _ = fetch(cand["src"], referer=url, limit=MAX_IMAGE_BYTES)
            digest = hashlib.md5(data).hexdigest()
            if digest in seen_digests:  # 대표 이미지가 썸네일로 반복되는 경우
                continue
            seen_digests.add(digest)
            with open(tmp, "wb") as f:
                f.write(data)
            result["tiles"] += slice_image(tmp, out_dir, len(result["tiles"]))
        except Exception as exc:  # 이미지 1장 실패가 전체를 막지 않게 한다
            result["errors"].append("%s: %s" % (cand["src"][:80], exc))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    print("JOB_OK tiles=%d text=%d" % (len(result["tiles"]), len(result["text"])))


if __name__ == "__main__":
    try:
        main(sys.argv[1], sys.argv[2])
    except Exception as exc:
        print("JOB_FAIL %s" % exc)
        sys.exit(1)
