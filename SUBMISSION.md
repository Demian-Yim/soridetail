# 제출 폼에 붙여넣을 문구 (복사해서 쓰세요)

> 폼 실제 항목을 확인하면 이 파일을 맞춰 고칩니다. 아래는 해커톤 제출 폼에 흔히 나오는 항목 기준 초안입니다.

---

## Project name
```
SoriDetail (소리상세)
```

## One-line description
```
An AI that reads image-only Korean product detail pages aloud for blind shoppers, using a Daytona sandbox to decode untrusted pages and images in isolation.
```

## Repository
```
https://github.com/Demian-Yim/soridetail
```

## Team
```
Solo — Demian Yim (FLOW : AX디자인연구소)
```

---

## Problem (English)
```
Korean online shops publish product details as one very long IMAGE: price options,
ingredients, allergens, sizes, expiry dates and return rules are all pixels, not text.
Screen readers cannot read images, so a blind shopper hears "image, image, image" and
cannot decide whether to buy — or whether the product is even safe for them.

This is not a rare edge case. On the page we demo, 9 of 11 images had no alt text.
Korea has roughly 250,000 registered people with visual disabilities, and the same
barrier blocks anyone using a screen reader.
```

## Solution (English)
```
SoriDetail turns an image-only product page into something you can listen to.

1. The user pastes or SPEAKS a product URL and presses one large button.
2. The server fetches the raw bytes; a Daytona sandbox parses the untrusted HTML
   and decodes/slices the detail images into tiles a vision model can read.
3. The vision model writes a short, fact-first guide designed for LISTENING:
   summary, price, features, size/ingredients/allergens, then cautions.
4. The browser speaks it aloud automatically, and the user can ask follow-up
   questions by voice ("Does it contain peanuts?").

Every progress step is spoken, because a blind user cannot see a loading spinner.
The model is instructed never to guess a number it cannot read, and to say
"not confirmed in the image" instead — a wrong allergen reading is worse than none.
```

## How we used Daytona (English)
```
Daytona is the security boundary of this product, not a checkbox.

Our service parses ARBITRARY HTML and decodes ARBITRARY images supplied by users.
That is the dangerous part: image decoders (Pillow/libjpeg/zlib) have a long CVE
history and one decompression bomb can kill a process. So our app server never
decodes a byte it did not produce.

  server  (app/fetcher.py)     fetches bytes only, SSRF-guarded. Never parses.
  sandbox (app/sandbox_job.py) parses HTML and decodes/tiles images.
                               Contains NO network code - a test enforces this.

Code: app/sandbox_runner.py
  Daytona.create()            -> one disposable machine per request
  fs.upload_file()            -> job + raw bytes in
  process.exec(... parse)     -> parse untrusted HTML in isolation
  process.exec(... tile)      -> decode untrusted images in isolation
  fs.download_file()          -> JSON + JPEG tiles out
  sandbox.delete()            -> always, in a finally block

We first tried putting the FETCHING in the sandbox. Daytona's tier-based network
policy blocks sandbox egress, which forced this split - and the split is strictly
safer: hostile bytes are decoded in an environment with ZERO network reach, so a
successful exploit has nowhere to exfiltrate to, and the machine is destroyed
seconds later.

The UI shows the live sandbox id and elapsed time, so the judges can see it working.
Measured: 29.7s end to end, 11 images found, 9 with no alt text, 10 tiles read.
```

## Tech stack
```
Python, FastAPI, Daytona SDK, Gemini vision API, Web Speech API (speech in/out), Pillow
```

## What's next
```
- Headless browser inside the sandbox for JavaScript-rendered marketplaces
- Run the vision model on Nosana GPU (OpenAI-compatible endpoint already wired in app/vision.py)
- Browser extension so it works on any shop without copying a URL
```

---

# 한국어 발표 대본 (3분)

**0:00 문제 (40초)**
"한국 쇼핑몰 상세페이지는 글이 아니라 긴 이미지 한 장입니다. 스크린리더는 이미지를 못 읽습니다."
→ 스크린리더가 "이미지, 이미지, 이미지"만 읽는 장면 재생
"오늘 데모할 이 페이지, 이미지 11장 중 9장에 대체텍스트가 없습니다. 시각장애인은 성분도 알레르기도 가격도 알 수 없습니다."

**0:40 데모 (90초)**
주소 붙여넣기 → "읽어줘" 누름
"지금 화면을 못 보는 사용자를 위해 진행 상황도 음성으로 나갑니다."
→ 안내문 음성 재생
→ "말로 질문하기" 누르고 "땅콩 들어 있어?" 질문

**2:10 Daytona (40초)**
"위험한 건 접속이 아니라 해석입니다. 남의 이미지를 디코딩하는 코드는 취약점 이력이 길고,
압축 폭탄 한 장이면 서버가 멈춥니다. 그래서 저희 서버는 자기가 만들지 않은 바이트를
단 하나도 해석하지 않습니다. 해석은 전부 Daytona 샌드박스 안에서 합니다.
그 샌드박스는 인터넷이 완전히 차단돼 있습니다. 악성 이미지로 코드가 실행돼도
데이터를 밖으로 빼낼 곳이 없고, 몇 초 뒤 그 컴퓨터는 사라집니다.
화면에 보이는 이 ID가 방금 만들어졌다 삭제된 컴퓨터입니다."

**2:50 마무리 (10초)**
"이미지를 텍스트로 바꾸는 게 아니라, 볼 수 없는 사람이 살 수 있게 만드는 겁니다."
