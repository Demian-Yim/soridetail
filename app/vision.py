"""비전 AI 호출 — 상세 이미지 타일을 읽어 시각장애인용 음성 안내문으로 바꾼다.

키가 있는 쪽을 자동 선택한다 (위에서부터 우선):
  VISION_BASE_URL   — OpenAI 호환 주소 (Nosana GPU 에 올린 오픈 비전 모델 등)
  ANTHROPIC_API_KEY — Claude
  GEMINI_API_KEY    — Gemini (무료 등급 가능)
  OPENAI_API_KEY    — OpenAI
"""
import os

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


_gemini_model_cache: list[str] = []


def _gemini_model(api_key: str) -> str:
    """GEMINI_MODEL 미지정 시 계정에서 쓸 수 있는 최신 flash 모델을 1회 조회해 고른다."""
    if os.getenv("GEMINI_MODEL"):
        return os.environ["GEMINI_MODEL"]
    if not _gemini_model_cache:
        res = httpx.get(f"{GEMINI_API_ROOT}/models", params={"pageSize": 200},
                        headers={"x-goog-api-key": api_key}, timeout=30)
        res.raise_for_status()
        skip = ("lite", "image", "tts", "live", "audio", "embedding", "preview", "exp")
        names = sorted(
            (m["name"].split("/")[-1] for m in res.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])
             and "flash" in m["name"] and not any(word in m["name"] for word in skip)),
            reverse=True,
        )
        _gemini_model_cache.append(names[0] if names else "gemini-2.5-flash")
    return _gemini_model_cache[0]


def _call_gemini(system: str, text: str, tiles_b64: list[str]) -> str:
    api_key = os.environ["GEMINI_API_KEY"]
    parts = [{"text": text}]
    parts += [{"inline_data": {"mime_type": "image/jpeg", "data": tile}} for tile in tiles_b64]
    res = httpx.post(
        f"{GEMINI_API_ROOT}/models/{_gemini_model(api_key)}:generateContent",
        headers={"x-goog-api-key": api_key},
        json={"system_instruction": {"parts": [{"text": system}]},
              "contents": [{"role": "user", "parts": parts}],
              "generationConfig": {"maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS}},
        timeout=REQUEST_TIMEOUT,
    )
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


def _call(system: str, text: str, tiles_b64: list[str]) -> str:
    if os.getenv("VISION_BASE_URL"):
        return _call_openai(system, text, tiles_b64)
    if os.getenv("ANTHROPIC_API_KEY"):
        return _call_anthropic(system, text, tiles_b64)
    if os.getenv("GEMINI_API_KEY"):
        return _call_gemini(system, text, tiles_b64)
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
