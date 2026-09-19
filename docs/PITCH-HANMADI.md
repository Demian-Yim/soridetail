# 한마디 — 3분 발표 대본과 제출 문구

> 2026-09-19 · FLOW : AX디자인연구소 · 데모 주소는 발표 직전 current_state.md 의 최신 터널 주소로 확인

## 발표 대본 (3분, 한국어)

**0:00 왜 나인가 (25초)**
"저는 십 년 넘게 시각장애인 곁에서 말로 길을 안내해 왔습니다. 그 일을 하면서 배운 건 하나입니다. 잘 안내하는 사람은 많이 말하지 않습니다. 지금 필요한 한마디만 합니다."

**0:25 문제 (30초)**
"요즘 AI는 카메라를 보고 뭐든 설명합니다. 그런데 전부 설명합니다. '나무가 있는 거리가 보이고 하늘은 맑으며…' 앞이 안 보이는 사람에게 이건 소음입니다. 계단이 세 걸음 앞에 있는데 하늘 이야기를 듣고 있을 수는 없습니다. 빠진 건 눈이 아니라, 무엇을 먼저 말하고 언제 입을 다물지 아는 판단입니다."

**0:55 데모 (80초)** — 폰으로 /eye 열기
1. "지금 알려줘" → 짧은 콜아웃이 자연 음성으로 나온다. "방향은 시계 방향, 거리는 걸음 수. 두 문장을 넘지 않습니다."
2. 같은 곳을 다시 비춘다 → 아무 말도 하지 않는다. "달라진 게 없으면 말하지 않습니다. 이 침묵이 기능입니다."
3. 렌즈를 손으로 가린다 → "화면이 너무 어둡습니다." "시각장애인은 자기가 찍은 사진이 어두운지 볼 수 없습니다. 그래서 먼저 알려줍니다."
4. "읽기" → 안내문이나 화면 글자를 읽는다. 5. "내 모습" → 전면 카메라로 옷매무새·화상통화 구도. "외모나 기분은 평가하지 않습니다. 보이는 사실만 말합니다."

**2:15 Daytona (30초)**
"이 카메라는 집 안, 약봉투, 우편물, 카드까지 비춥니다. 그래서 카메라 세션 하나에 일회용 Daytona 샌드박스 한 대를 씁니다. 어두운지, 흔들렸는지, 장면이 바뀌었는지는 그 안에서만 판정하고, 끝내기를 누르면 샌드박스째 지웁니다. 화면에 보이는 이 아이디가 방금 만들어진 컴퓨터입니다. 계속 보기에서는 이 샌드박스가 AI를 부를지 말지까지 결정합니다."

**2:45 마무리 (15초)**
"같은 엔진으로 화면 속 이미지 상세페이지를 읽어주는 소리상세도 함께 만들었습니다. 눈앞이든 화면 속이든, 보이지 않는 정보를 필요한 한마디로 바꿉니다. 그리고 이 규약은 제미나이와 챗지피티 영상 모드에도 그대로 옮겨 쓸 수 있게 공개했습니다."

## 한계 (질문 받으면 그대로 답할 것)
- 답까지 약 4초. 걸으며 장애물을 피하는 용도가 아니라 멈춰 서서 파악하는 용도. 흰 지팡이·안내견을 대체하지 않는다.
- 횡단보도 신호·차량 통행은 판단하지 않는다. AI는 좌우를 틀릴 수 있다.
- 실시간 스트리밍(Live API)으로 바꾸면 지연이 줄어든다 — 다음 단계.
- 아직 시각장애인 당사자 현장 검증 전. 계획은 docs/SCENARIOS.md (3~5명·2주).

## 제출 폼 문구 (English)

**Project name**: Hanmadi (한마디) + SoriDetail

**One line**: A camera guide for blind users that says only what matters — hazards first, two sentences, silence when nothing changed — with every camera session isolated in a disposable Daytona sandbox.

**Problem**: AI vision tools describe everything. For a blind person that is noise, and a late or buried warning is dangerous. What is missing is not vision but a guide's judgment: what to say first, how briefly, and when to stay silent. Blind users also cannot tell when their own photo is dark or blurred.

**Solution**: A guide callout protocol distilled from ten years of guiding blind people by voice: hazards → direction → request, clock-face directions and steps, two sentences, silence on no change, never "it is safe", "not sure" plus how to move the camera. Rear camera for the surroundings, front camera for a self-check. Natural local voice, or hand-off to the user's own screen reader.

**How we used Daytona**: One camera session = one disposable sandbox. Frames of a blind user's private life are checked only inside it (darkness, blur, scene change) and the sandbox is deleted on exit. In continuous mode the sandbox gates the AI call: no change, no call, no speech. SoriDetail uses a sandbox per request to parse untrusted pages and images.

**Tech**: Python, FastAPI, Daytona SDK, Gemini vision, Supertonic 3 TTS, Web Speech API, PWA.

**Repo**: https://github.com/Demian-Yim/soridetail
