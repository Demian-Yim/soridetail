"""Daytona 샌드박스 안에서만 실행되는 해석 스크립트 — 네트워크를 쓰지 않는다.

신뢰할 수 없는 바이트(남의 HTML·이미지)를 해석하는 위험한 일을 전부 여기서 한다.
샌드박스는 외부 인터넷이 차단돼 있어, 설령 악성 이미지로 코드 실행이 일어나도
바깥으로 데이터를 빼낼 수 없다.

  parse <html_path> <base_url> <out_dir>   HTML 해석 → result.json (텍스트·이미지 주소 목록)
  tile  <img_dir> <out_dir>                이미지 디코딩 → tile_*.jpg (세로로 긴 이미지 분할)
"""
import hashlib
import json
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

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


def decode_html(raw, charset=None):
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


def parse_mode(html_path, base_url, out_dir):
    """신뢰할 수 없는 HTML 을 샌드박스 안에서 해석한다."""
    os.makedirs(out_dir, exist_ok=True)
    raw = open(html_path, "rb").read()
    parser = PageParser()
    parser.feed(decode_html(raw, None))

    candidates = pick_images(parser.images, base_url)
    og_image = parser.meta.get("og:image")
    if og_image:
        candidates.insert(0, {"src": urljoin(base_url, og_image), "alt": ""})

    result = {
        "url": base_url,
        "title": parser.meta.get("og:title") or parser.title,
        "description": parser.meta.get("og:description") or parser.meta.get("description", ""),
        "text": re.sub(r"\s+", " ", " ".join(parser.texts))[:MAX_TEXT_CHARS],
        "image_count": len(candidates),
        "image_alts_missing": sum(1 for c in candidates if not c["alt"]),
        "wanted": [c["src"] for c in candidates[:MAX_CANDIDATES]],
    }
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    print("PARSE_OK images=%d text=%d" % (result["image_count"], len(result["text"])))


def tile_mode(img_dir, out_dir):
    """신뢰할 수 없는 이미지를 샌드박스 안에서 디코딩·분할한다."""
    os.makedirs(out_dir, exist_ok=True)
    tiles, errors, seen = [], [], set()
    for name in sorted(os.listdir(img_dir)):
        if len(tiles) >= MAX_TILES:
            break
        path = os.path.join(img_dir, name)
        try:
            digest = hashlib.md5(open(path, "rb").read()).hexdigest()
            if digest in seen:  # 대표 이미지가 썸네일로 반복되는 경우
                continue
            seen.add(digest)
            tiles += slice_image(path, out_dir, len(tiles))
        except Exception as exc:  # 이미지 1장 실패가 전체를 막지 않게 한다
            errors.append("%s: %s" % (name, exc))
    with open(os.path.join(out_dir, "tiles.json"), "w", encoding="utf-8") as f:
        json.dump({"tiles": tiles, "errors": errors}, f, ensure_ascii=False)
    print("TILE_OK tiles=%d" % len(tiles))


if __name__ == "__main__":
    try:
        if sys.argv[1] == "parse":
            parse_mode(sys.argv[2], sys.argv[3], sys.argv[4])
        else:
            tile_mode(sys.argv[2], sys.argv[3])
    except Exception as exc:
        print("JOB_FAIL %s" % exc)
        sys.exit(1)
