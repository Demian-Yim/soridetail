# 소리상세 (SoriDetail) — an AI that reads image-only product pages aloud for blind shoppers

Built at **Daytona HackSprint Seoul** (2026-09-19) · Solo · FLOW : AX디자인연구소

> **One engine, two inputs — turning what cannot be seen into what can be heard.**
> **SoriDetail** reads what is *on the screen* (image-only product pages). **Hanmadi (한마디)** reads what is *in front of you* (the phone camera). Open `/` for SoriDetail, `/eye` for Hanmadi.

## Hanmadi (한마디, "just one word") — a camera guide that says only what you need

Built by someone who has guided blind people by voice for over ten years. Today's AI tools *describe everything* ("I see a street with trees…"). A human guide does the opposite: says the one thing that matters, first, in few words — and stays silent otherwise. Hanmadi encodes that judgment.

- **Guide callout protocol** ([app/vision.py](app/vision.py) `GUIDE_PROMPT`): hazards first → way and direction → what you asked. Clock-face directions and steps. Two sentences max. Silence when nothing changed. Never says "it is safe". When unsure: "not sure" + how to move the camera.
- **Rear camera**: look around, find, read, people, keep watching. **Front camera ("my look")**: stains, open buttons, crooked glasses, video-call framing. It never judges appearance, mood or health.
- **Daytona is the privacy boundary.** A blind user's camera sees the inside of their home, medicine bags, mail and cards. One camera session = one disposable sandbox ([app/eye_session.py](app/eye_session.py)). Frames are checked inside it for darkness, blur and scene change ([app/frame_job.py](app/frame_job.py)) — a blind user cannot see that a photo is bad — and the sandbox is deleted when the session ends. In "keep watching", the sandbox decides whether the AI is called at all: no change, no call, no speech.
- **Natural local voice** (Supertonic 3, free, no quota). The first sentence is synthesised first so a hazard is never delayed. Users can hand speech over to VoiceOver/TalkBack instead, so two voices never overlap.
- **Mobile-first PWA**: one 120px thumb-zone button, haptics, wake lock, black/yellow high contrast.
- Measured on real Daytona + Gemini: session sandbox 5.0s, frame check 0.6–1.4s, callout ~3.9s, same scene → silent in 0.7s. It is a *stop-and-understand* tool, **not** an obstacle-avoidance tool, and it does not replace a white cane or guide dog.
- Docs: [portable protocol for Gemini/ChatGPT/Grok video modes](docs/HANMADI-PROTOCOL.md) · [12 scenarios + numeric evaluation plan](docs/SCENARIOS.md) · [listener-first user guide](docs/USER-GUIDE.md)

# SoriDetail

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
