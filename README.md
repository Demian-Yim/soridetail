# 소리상세 (SoriDetail) — an AI that reads image-only product pages aloud for blind shoppers

Built at **Daytona HackSprint Seoul** (2026-09-19) · Solo · FLOW : AX디자인연구소

## The problem

Korean online shops publish product details (price options, ingredients, allergens, sizes, expiry, return rules) as **one very long image**. Screen readers cannot read images, so a blind shopper hears "image, image, image" and cannot make a purchase decision alone. On the demo page below, **9 of 11 images had no alt text**.

## What it does

1. The user pastes (or speaks) a product URL and presses one big button.
2. The server downloads the raw bytes (SSRF-guarded) and hands them to a **Daytona sandbox**, which parses the untrusted HTML and decodes/slices the detail images into readable tiles.
3. A vision model reads the tiles and writes a short, fact-first guide **designed to be listened to** (price → features → size/ingredients/allergens → cautions).
4. The browser reads it aloud automatically. The user can then ask follow-up questions by voice ("Does it contain peanuts?").

Every progress step is announced by voice, because a blind user cannot see a spinner.

## Where Daytona is used (code-level)

| File | What happens |
|---|---|
| [app/sandbox_runner.py](app/sandbox_runner.py) | `Daytona.create()` → `fs.upload_file()` → `process.exec(parse)` → `process.exec(tile)` → `fs.download_file()` → `sandbox.delete()` in a `finally`. One request = one disposable sandbox. |
| [app/sandbox_job.py](app/sandbox_job.py) | Runs **inside** the sandbox. Contains **no network code at all** (a test enforces this). It only parses untrusted HTML and decodes/tiles untrusted images with Pillow. |
| [app/fetcher.py](app/fetcher.py) | Runs on the server. Only *fetches bytes* — never parses them — and refuses private/loopback/link-local addresses. |

**The split is the security design.** Parsing hostile input is the dangerous part: image decoders (Pillow / libjpeg / zlib) have a long CVE history, and a single decompression bomb can take down a process. So the app server never decodes a byte it did not produce. All parsing happens in a throwaway Daytona sandbox that, on our tier, has **no outbound internet at all** — so even a successful exploit in the decoder has nowhere to send anything, and the machine is destroyed seconds later.

This is a deliberate reversal of the obvious design. We first put the *fetching* in the sandbox; Daytona's tier-based network policy blocks sandbox egress, which pushed us to a split that turned out to be strictly safer.

## Architecture

```
Browser (voice in / voice out, high-contrast, keyboard-only friendly)
   │  POST /api/read  (NDJSON progress stream)
FastAPI
   ├── fetcher.py  : bytes only, SSRF-guarded              (trusted side)
   ├── Daytona SDK : parse HTML → tile images, no network   (untrusted side)
   └── Vision model: Gemini / Claude / OpenAI-compatible (e.g. a Nosana GPU)
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

Measured end to end on a real product page: **29.7 s**, 11 images found, **9 with no alt text**, 10 tiles read. The model reported price, volume, shipping and return terms correctly, and explicitly said the ingredient list and expiry date were "not confirmed in the image" rather than inventing them.
