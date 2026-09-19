# 소리상세 (SoriDetail) — an AI that reads image-only product pages aloud for blind shoppers

Built at **Daytona HackSprint Seoul** (2026-09-19) · Solo · FLOW : AX디자인연구소

## The problem

Korean online shops publish product details (price options, ingredients, allergens, sizes, expiry, return rules) as **one very long image**. Screen readers cannot read images, so a blind shopper hears "image, image, image" and cannot make a purchase decision alone. On the demo page below, **9 of 11 images had no alt text**.

## What it does

1. The user pastes (or speaks) a product URL and presses one big button.
2. A **Daytona sandbox** opens the unknown page, downloads the detail images and slices the tall images into readable tiles.
3. A vision model reads the tiles and writes a short, fact-first guide **designed to be listened to** (price → features → size/ingredients/allergens → cautions).
4. The browser reads it aloud automatically. The user can then ask follow-up questions by voice ("Does it contain peanuts?").

Every progress step is announced by voice, because a blind user cannot see a spinner.

## Where Daytona is used (code-level)

| File | What happens |
|---|---|
| [app/sandbox_runner.py](app/sandbox_runner.py) | `Daytona.create()` → `sandbox.fs.upload_file()` → `sandbox.process.exec()` → `sandbox.fs.download_file()` → `sandbox.delete()`. One request = one disposable sandbox. |
| [app/sandbox_job.py](app/sandbox_job.py) | The script that runs **inside** the sandbox: fetches the untrusted page, downloads images with the right Referer, de-duplicates, and tiles tall images with Pillow. |

**Why a sandbox and not our own server?** The service opens arbitrary URLs supplied by users and parses arbitrary images. Doing that inside a disposable, isolated Daytona sandbox keeps SSRF, malicious files and decompression bombs away from the app server, and each request starts from a clean machine.

## Architecture

```
Browser (voice in / voice out, high-contrast, keyboard-only friendly)
   │  POST /api/read  (NDJSON progress stream)
FastAPI  ── Daytona SDK ──►  Sandbox: fetch page → download images → tile
   │                          ◄── result.json + tile_*.jpg
   └── Vision model (Gemini / Claude / OpenAI-compatible endpoint such as a Nosana GPU)
```

## Run

```bash
pip install -r requirements.txt
# put DAYTONA_API_KEY and GEMINI_API_KEY into .env
python -m uvicorn app.main:app --port 8000
# open http://127.0.0.1:8000
```

Demo URL: `https://roundlab.co.kr/product/1025-독도-토너-200ml/22/category/1/display/2/`

## Accessibility choices

- `aria-live` status region + spoken progress, focus moves to the result heading when done
- 22px+ text, 64px+ touch targets, black/yellow contrast, visible focus ring, skip link
- Speech in (Web Speech API, `ko-KR`) and speech out (`speechSynthesis`) — no screen needed
- The model is told to never guess numbers it cannot read and to say so instead

## Limits (honest)

- Pages rendered only by JavaScript or behind bot protection (e.g. some large marketplaces) are not readable yet — next step is a headless browser inside the sandbox.
- Reads up to 10 tiles per page to keep latency around 30 seconds.
