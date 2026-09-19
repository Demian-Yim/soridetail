"""비전 AI 호출 — 상세 이미지 타일을 읽어 시각장애인용 음성 안내문으로 바꾼다.

키가 있는 쪽을 자동 선택한다 (위에서부터 우선):
  VISION_BASE_URL   — OpenAI 호환 주소 (Nosana GPU 에 올린 오픈 비전 모델 등)
  ANTHROPIC_API_KEY — Claude
  GEMINI_API_KEY    — Gemini (무료 등급 가능)
  OPENAI_API_KEY    — OpenAI
"""
import os
import re

import httpx

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MAX_OUTPUT_TOKENS = 6000  # 사고 토큰이 출력 한도에 포함되는 모델 대비
REQUEST_TIMEOUT = 120
MAX_OUTPUT_TOKENS = 1500

SYSTEM_PROMPT = """당신은 시각장애인 쇼핑 도우미입니다. 상품 페이지의 텍스트와 상세 이미지 조각을 받아,
화면을 볼 수 없는 사람이 '귀로 듣고' 구매 판단을 할 수 있게 안내문을 씁니다.

규칙:
1. 음성으로 읽힐 글입니다. 기호·표·마크다운·이모지를 쓰지 말고 짧은 문장으로 쓰세요.
2. 순서: 한 문장 요약 → 가격과 구성 → 핵심 특징 → 크기·용량·성분·알레르기 등 사실 정보 → 주의사항·유통기한·배송·교환.
3. 이미지 속 글자에서 읽은 숫자(가격·용량·성분·날짜)는 정확히 옮기세요. 안 보이거나 흐리면 추측하지 말고 "이미지에서 확인되지 않습니다"라고 말하세요.
4. 색·모양·사용 장면처럼 눈으로만 알 수 있는 정보를 한두 문장으로 묘사하세요.
5. 광고 문구는 줄이고 사실을 앞세우세요. 전체 900자 이내.
6. 상품 페이지가 아니면 그 페이지가 무엇인지와 핵심 내용을 같은 방식으로 안내하세요."""

ASK_PROMPT = """당신은 시각장애인 쇼핑 도우미입니다. 아래 '안내 내용'만 근거로 질문에 두세 문장으로 답하세요.
음성으로 읽히므로 기호 없이 쓰고, 근거가 없으면 "안내된 내용에서는 확인되지 않습니다"라고 답하세요."""


def provider_name() -> str:
    if os.getenv("VISION_BASE_URL"):
        return f"custom:{os.getenv('VISION_MODEL', '')}"
    if os.getenv("ANTHROPIC_API_KEY"):
        return f"anthropic:{ANTHROPIC_MODEL}"
    if os.getenv("GEMINI_API_KEY"):
        return f"gemini:{os.getenv('GEMINI_MODEL') or 'auto'}"
    if os.getenv("OPENAI_API_KEY"):
        return f"openai:{OPENAI_MODEL}"
    return "none"


def _call_anthropic(system: str, text: str, tiles_b64: list[str]) -> str:
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": tile}}
        for tile in tiles_b64
    ]
    content.append({"type": "text", "text": text})
    res = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        json={"model": ANTHROPIC_MODEL, "max_tokens": MAX_OUTPUT_TOKENS, "system": system,
              "messages": [{"role": "user", "content": content}]},
        timeout=REQUEST_TIMEOUT,
    )
    res.raise_for_status()
    return "".join(block.get("text", "") for block in res.json()["content"]).strip()


GEMINI_FAST_PREFERRED = "gemini-3.6-flash"  # 2026-09-19 실측: 실사 1장 2.7초, 콜아웃 규약 준수
GEMINI_RETRY_STATUS = (404, 429, 503)       # 할당량 없음·혼잡·종료된 모델이면 다음 후보로 넘어간다
GEMINI_VERSION = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash$")

_gemini_candidates_cache: list[str] = []
_gemini_working: dict[bool, str] = {}


def _gemini_candidates(api_key: str) -> list[str]:
    """쓸 수 있는 flash 모델을 '버전 숫자' 내림차순으로 돌려준다.

    이름 문자열로 정렬하면 gemini-omni-* 가 gemini-3.x 보다 위에 와서 무료 할당량 0인 모델이 뽑힌다(429).
    """
    if os.getenv("GEMINI_MODEL"):
        return [os.environ["GEMINI_MODEL"]]
    if not _gemini_candidates_cache:
        res = httpx.get(f"{GEMINI_API_ROOT}/models", params={"pageSize": 200},
                        headers={"x-goog-api-key": api_key}, timeout=30)
        res.raise_for_status()
        versioned = [
            (tuple(int(n) for n in match.group(1).split(".")), match.group(0))  # 3.10 > 3.6 이 되도록 숫자 튜플로
            for m in res.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
            and (match := GEMINI_VERSION.match(m["name"].split("/")[-1]))
        ]
        names = [name for _, name in sorted(versioned, reverse=True)]
        _gemini_candidates_cache.extend(names or ["gemini-2.5-flash"])
    return list(_gemini_candidates_cache)


def _gemini_order(api_key: str, fast: bool) -> list[str]:
    candidates = _gemini_candidates(api_key)
    first = [_gemini_working[fast]] if fast in _gemini_working else []
    if fast and GEMINI_FAST_PREFERRED in candidates:
        first.append(GEMINI_FAST_PREFERRED)
    return list(dict.fromkeys([*first, *candidates]))


def _call_gemini(system: str, text: str, tiles_b64: list[str], fast: bool = False) -> str:
    api_key = os.environ["GEMINI_API_KEY"]
    parts = [{"text": text}]
    parts += [{"inline_data": {"mime_type": "image/jpeg", "data": tile}} for tile in tiles_b64]
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS}}
    # 실시간 안내는 속도가 생명 — 사고(thinking)를 끈다. 모델이 거부(400)하면 기본 설정으로 다시 보낸다.
    fast_body = {**body, "generationConfig": {**body["generationConfig"], "thinkingConfig": {"thinkingBudget": 0}}}
    headers = {"x-goog-api-key": api_key}
    res = None
    for model in _gemini_order(api_key, fast):
        url = f"{GEMINI_API_ROOT}/models/{model}:generateContent"
        res = httpx.post(url, headers=headers, json=fast_body if fast else body, timeout=REQUEST_TIMEOUT)
        if fast and res.status_code == 400:
            res = httpx.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
        if res.status_code not in GEMINI_RETRY_STATUS:
            if res.is_success:
                _gemini_working[fast] = model
            break
        print(f"[vision] {model} {res.status_code} - 다음 모델로 넘어갑니다.")
    res.raise_for_status()
    candidate = res.json()["candidates"][0]
    return "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", [])).strip()


def _call_openai(system: str, text: str, tiles_b64: list[str]) -> str:
    content = [{"type": "text", "text": text}]
    content += [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{tile}", "detail": "high"}}
        for tile in tiles_b64
    ]
    # VISION_BASE_URL 이 있으면 OpenAI 호환 서버(Nosana GPU 등)로 보낸다
    base_url = os.getenv("VISION_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    api_key = os.getenv("VISION_API_KEY") or os.getenv("OPENAI_API_KEY", "none")
    model = os.getenv("VISION_MODEL", OPENAI_MODEL) if os.getenv("VISION_BASE_URL") else OPENAI_MODEL
    res = httpx.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "max_tokens": MAX_OUTPUT_TOKENS,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": content}]},
        timeout=REQUEST_TIMEOUT,
    )
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"].strip()


def _call(system: str, text: str, tiles_b64: list[str], fast: bool = False) -> str:
    if os.getenv("VISION_BASE_URL"):
        return _call_openai(system, text, tiles_b64)
    if os.getenv("ANTHROPIC_API_KEY"):
        return _call_anthropic(system, text, tiles_b64)
    if os.getenv("GEMINI_API_KEY"):
        return _call_gemini(system, text, tiles_b64, fast=fast)
    if os.getenv("OPENAI_API_KEY"):
        return _call_openai(system, text, tiles_b64)
    raise RuntimeError("비전 AI 키가 없습니다. .env 에 GEMINI_API_KEY 를 넣어 주세요.")


def describe_page(page: dict) -> str:
    text = (
        f"페이지 주소: {page['url']}\n제목: {page['title']}\n설명: {page['description']}\n"
        f"이미지 {page['image_count']}장 중 대체텍스트 없는 이미지 {page['image_alts_missing']}장.\n"
        f"페이지 본문 텍스트:\n{page['text']}\n\n"
        f"첨부한 이미지 {len(page['tiles_b64'])}장은 상세 이미지를 위에서 아래 순서로 자른 조각입니다."
    )
    return _call(SYSTEM_PROMPT, text, page["tiles_b64"])


def answer_question(question: str, context: str) -> str:
    return _call(ASK_PROMPT, f"안내 내용:\n{context}\n\n질문: {question}", [])


# ───────── 눈앞 보기 (실시간 카메라 안내) ─────────

GUIDE_PROMPT = """당신은 시각장애인 곁에서 십 년 넘게 함께 걸어온 안내자입니다.
사용자가 정면을 향해 든 카메라의 사진 한 장을 받습니다. 본 것을 전부 묘사하지 마세요.
지금 이 사람에게 필요한 것만 골라, 가장 중요한 것부터, 짧게 말합니다.

규칙:
1. 우선순위: 첫째 위험(계단, 턱, 단차, 구멍, 젖은 바닥, 머리 높이 장애물, 다가오는 사람이나 차량).
   둘째 길과 방향(문, 통로, 엘리베이터, 계단, 출구, 빈 자리). 셋째 사용자가 요청한 것. 넷째 그 밖의 것.
   위험이 보이면 위험부터 한 문장으로 말하세요.
2. 방향은 시계 방향으로 말합니다(열두 시가 정면). 거리는 걸음 수나 미터로, 어림이므로 "약"을 붙입니다.
   "저기", "이쪽", "저 앞"처럼 가리키는 말은 쓰지 않습니다.
3. 한 번에 두 문장, 육십 자 이내. 읽기 요청만 예외입니다: 글자를 있는 그대로 읽되
   제목, 금액, 날짜, 경고문부터 삼백 자 이내로 읽습니다.
4. 색, 분위기, 장식은 물어볼 때만 말합니다.
5. 사람은 인원, 위치, 향한 방향, 하고 있는 일만 말합니다. 누구인지 추측하지 않고 외모를 평가하지 않습니다.
6. 안 보이거나 확신이 없으면 추측하지 말고 "확실하지 않습니다"라고 말한 뒤,
   카메라를 어떻게 움직이면 좋을지 한마디 덧붙입니다(위로, 아래로, 왼쪽으로, 오른쪽으로, 한 걸음 뒤로).
7. 직전 안내와 달라진 것이 없으면 정확히 "변화 없음" 네 글자만 출력합니다.
8. 음성으로 읽힙니다. 기호, 마크다운, 이모지를 쓰지 않습니다.
9. 당신은 보조입니다. "안전합니다", "건너도 됩니다", "위험 요소는 없습니다" 같은 보증을 하지 않습니다.
   사진에 안 보였을 뿐 위험이 없다는 뜻이 아니므로, 위험이 안 보이면 위험에 대해서는 아무 말도 하지 않습니다.
   신호등과 차량 통행은 판단해 주지 않고 "직접 확인이 필요합니다"라고 말합니다.
10. 사용자가 말로 요청한 문장이 있으면 의도를 먼저 파악합니다. 찾기(어디, 찾아줘), 읽기(읽어줘, 뭐라고 써 있어),
    사람(누가 있어, 줄 서 있어), 둘러보기(앞에 뭐 있어) 중 무엇인지 보고 그 의도에 맞는 답만 합니다."""

LOOK_MODES = {
    "look": "둘러보기: 지금 앞의 상황을 우선순위대로 알려 주세요.",
    "find": "찾기: 사용자가 찾는 대상의 방향과 거리를 알려 주세요. 안 보이면 카메라를 어느 쪽으로 돌릴지 말해 주세요.",
    "read": "읽기: 화면 속 글자를 읽어 주세요. 표지판, 안내문, 포장지, 키오스크나 기기 화면이 대상입니다.",
    "people": "사람: 앞에 있는 사람의 인원, 위치, 향한 방향, 하는 일을 알려 주세요.",
    "watch": "계속 보기: 직전 안내 이후 새로 생긴 위험이나 변화만 말하세요. 없으면 변화 없음.",
}
NO_CHANGE = "변화 없음"


def look(frame_b64: str, mode: str, question: str = "", last_callout: str = "") -> str:
    lines = [LOOK_MODES.get(mode, LOOK_MODES["look"])]
    if question:
        lines.append(f"사용자가 말로 요청한 문장: {question}")
    if last_callout and mode == "watch":  # 직접 물었을 때는 같은 장면이어도 반드시 답한다
        lines.append(f"직전 안내: {last_callout}")
    return _call(GUIDE_PROMPT, "\n".join(lines), [frame_b64], fast=True)
