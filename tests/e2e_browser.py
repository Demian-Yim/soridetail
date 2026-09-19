"""실브라우저 회귀 검사 — 화면을 고칠 때마다 돌린다. (pytest 수집 대상 아님: 서버·크롬·실제 API 키가 필요하다)

    python tests/e2e_browser.py [http://127.0.0.1:8001] [스크린샷 폴더]

가짜 카메라를 붙인 크롬으로 입장 게이트 → 소리 안내 → 앱 두 모드 → 목소리 고르기를 실제로 눌러 본다.
문법 검사와 id 존재 확인만으로는 "눌러도 아무 일도 안 일어나는" 결함을 못 잡는다 — 2026-09-19 에 그런 결함 5건이 통과됐었다.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001"
SHOTS = Path(sys.argv[2]) if len(sys.argv) > 2 else None
PHONE = {"width": 390, "height": 844}
CHROME_ARGS = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
               "--autoplay-policy=no-user-gesture-required"]
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(("PASS " if ok else "FAIL ") + label)


def overflow(page: Page) -> int:
    return page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")


def shot(page: Page, name: str, full: bool = False) -> None:
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full)


def watch(page: Page) -> dict:
    seen = {"tts": [], "api": [], "errors": [], "bad": []}
    page.on("request", lambda r: seen["tts"].append(json.loads(r.post_data or "{}")) if r.url.endswith("/api/tts") else None)
    page.on("request", lambda r: seen["api"].append(r.url.split("/api/")[-1]) if "/api/eye/" in r.url else None)
    page.on("pageerror", lambda e: seen["errors"].append(str(e)))
    page.on("response", lambda r: seen["bad"].append(f"{r.status} {r.url}") if r.status >= 400 else None)
    return seen


def landing(browser) -> None:
    page = browser.new_context(viewport=PHONE).new_page(); seen = watch(page)
    page.goto(f"{BASE}/?view=gate"); page.wait_for_timeout(600)
    check(page.evaluate("typeof Voice") == "object", "랜딩: 음성 재생기 로드됨")
    check(page.is_visible("#gate") and page.is_visible("#btnVoice") and page.is_visible("#btnVisual"), "랜딩: 입장 게이트 두 갈래 보임")
    shot(page, "landing_gate")
    page.mouse.click(195, 12); page.wait_for_timeout(1500)
    check(len(seen["tts"]) >= 1, "랜딩: 빈 곳 첫 터치에 환영 음성 요청")
    page.click("#btnVoice"); page.wait_for_timeout(1500)
    check(page.is_visible("#voice") and not page.is_visible("#gate"), "랜딩: 소리로 안내받기 클릭 → 소리 안내 뷰")
    check(page.evaluate("localStorage.getItem('heyvision-ui')") == "voice", "랜딩: 선택 저장(voice)")
    check(overflow(page) == 0, "랜딩 소리 뷰: 가로 넘침 0")
    shot(page, "landing_voice")
    before = len(seen["tts"]); page.locator("#voice >> text=이 부분 듣기").first.click(); page.wait_for_timeout(1500)
    check(len(seen["tts"]) > before, "랜딩: 이 부분 듣기 → 음성 요청")
    href = page.locator("#voice a[href^='/eye']").first.get_attribute("href")
    check(href == "/eye?ui=voice", f"랜딩: 소리 뷰 시작 링크 = {href}")
    page.goto(f"{BASE}/?view=gate"); page.wait_for_timeout(400); page.keyboard.press("2"); page.wait_for_timeout(800)
    check(page.is_visible("#visual"), "랜딩: 숫자 2 키 → 화면 보기 뷰")
    check(overflow(page) == 0, "랜딩 화면 뷰(390px): 가로 넘침 0")
    check(page.locator("#visual a[href='/eye?ui=visual']").count() >= 1, "랜딩: 화면 뷰에 지금 써보기 링크")
    check(page.locator("#contactBtn").count() == 1, "랜딩: 도입 문의 버튼 존재")
    shot(page, "landing_visual_mobile", full=True)
    check(not seen["errors"], f"랜딩: 스크립트 오류 0 {seen['errors'][:2]}")
    check(not seen["bad"], f"랜딩: 4xx·5xx 응답 0 {seen['bad'][:2]}")
    wide = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
    wide.goto(f"{BASE}/?view=visual"); wide.wait_for_timeout(800)
    check(overflow(wide) == 0, "랜딩 화면 뷰(1280px): 가로 넘침 0")
    shot(wide, "landing_visual_desktop", full=True)


def app_mode(browser, ui: str, start: str, now: str, end: str) -> None:
    page = browser.new_context(viewport=PHONE, permissions=["camera", "microphone"]).new_page(); seen = watch(page)
    page.goto(f"{BASE}/eye?ui={ui}"); page.wait_for_timeout(800)
    check(page.get_attribute("body", "data-ui") == ui, f"앱 {ui}: 모드 적용")
    check(overflow(page) == 0, f"앱 {ui}: 가로 넘침 0")
    if ui == "voice":
        box = page.locator("#voiceModeCycleBtn").bounding_box()
        check(box and box["y"] + box["height"] <= PHONE["height"], "앱 voice: 주 버튼+보조 2개가 한 화면 안")
    skip = page.locator("button:visible", has_text="건너뛰기")
    if ui == "visual":
        check(skip.count() == 1, "앱 visual: 첫 방문 안내 창이 뜬다")
    else:
        check(skip.count() == 0, "앱 voice: 첫 방문 안내 창이 화면을 가리지 않는다")
    if skip.count():
        skip.first.click(); page.wait_for_timeout(400)
    shot(page, f"app_{ui}_before")
    page.click(start); page.wait_for_timeout(700)
    if ui == "voice":   # 준비 중에 다시 누르면 말로 알려줘야 한다
        before = len(seen["tts"]); page.click(start); page.wait_for_timeout(900)
        waiting = [t.get("text", "") for t in seen["tts"][before:]]
        check(any("준비하고" in text for text in waiting), "앱 voice: 준비 중 다시 누르면 '준비하고 있습니다' 음성")
    page.wait_for_function("document.getElementById('video').videoWidth > 0", timeout=45000)
    page.wait_for_function(f"document.querySelector('{now}').textContent.includes('지금 알려줘') && !document.querySelector('{now}').hidden", timeout=45000)
    page.wait_for_timeout(800)
    check(page.inner_text("#voiceStatus" if ui == "voice" else "#visualStatus").strip() == "", f"앱 {ui}: 준비가 끝나면 '준비 중' 문구가 사라진다")
    check("start" in " ".join(seen["api"]), f"앱 {ui}: 시작 → 세션 샌드박스 요청")
    if ui == "visual":
        check(not page.is_visible("#visualCtaWrap"), "앱 visual: 카메라 켠 뒤 자리표시 아이콘 사라짐")
    looks = sum("look" in call for call in seen["api"]); page.click(now)
    for _ in range(40):  # 판독은 보통 4초, 길면 20초 — 조건이 맞는 즉시 다음으로
        page.wait_for_timeout(500)
        if sum("look" in call for call in seen["api"]) > looks and len(seen["tts"]) > 0:
            break
    check(sum("look" in call for call in seen["api"]) > looks, f"앱 {ui}: 지금 알려줘 → 장면 판독 요청")
    page.wait_for_timeout(2500)
    shot(page, f"app_{ui}_after")
    check(page.locator("#voicePicker").count() == 1, f"앱 {ui}: 목소리 고르기 자리 존재")
    page.click(end); page.wait_for_timeout(2500)
    check(any("end" in call for call in seen["api"]), f"앱 {ui}: 끝내기 → 샌드박스 삭제 요청")
    check(not seen["errors"], f"앱 {ui}: 스크립트 오류 0 {seen['errors'][:2]}")
    check(not [b for b in seen["bad"] if "/api/" in b], f"앱 {ui}: API 오류 0 {seen['bad'][:2]}")


def picker(browser) -> None:
    page = browser.new_context(viewport=PHONE).new_page(); seen = watch(page)
    page.goto(f"{BASE}/voices"); page.wait_for_selector(".hv-picker-item input", timeout=20000)
    check(page.locator("input[type=radio]").count() == 10, "목소리: 프리셋 6 + 빠르기 4")
    page.locator("input[value=calm]").check(); page.wait_for_timeout(1200)
    check(page.evaluate("localStorage.getItem('heyvision-voice-preset')") == "calm", "목소리: 선택 저장")
    check(seen["tts"] and seen["tts"][-1].get("preset") == "calm", "목소리: 고른 목소리로 미리듣기 요청")
    check(overflow(page) == 0 and not seen["errors"], "목소리: 가로 넘침 0 · 오류 0")


def main() -> int:
    stages = (
        ("랜딩", landing),
        ("앱 소리 모드", lambda browser: app_mode(browser, "voice", "#voiceMainBtn", "#voiceMainBtn", "#voiceEndBtn")),
        ("앱 보기 모드", lambda browser: app_mode(browser, "visual", "#visualCameraCta", "#visualNowBtn", "#visualEndBtn")),
        ("목소리 고르기", picker),
    )
    with sync_playwright() as p:
        for name, run in stages:
            # 단계마다 새 브라우저. 크롬의 가짜 카메라는 한 번 놓으면 브라우저 전체에서 사라져(장치 수 0)
            # 다음 단계의 카메라 요청이 NotFoundError 로 거절된다 — 앱이 아니라 검사 도구의 한계 (2026-09-19 실측).
            browser = p.chromium.launch(channel="chrome", headless=True, args=CHROME_ARGS)
            try:
                run(browser)
            except Exception as exc:  # 한 단계가 멈춰도 나머지는 끝까지 본다
                check(False, f"{name}: 중단 — {str(exc).splitlines()[0][:140]}")
            finally:
                browser.close()
    failed = [label for ok, label in results if not ok]
    print(f"\nSUMMARY {len(results) - len(failed)}/{len(results)} passed" + (f" · FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
