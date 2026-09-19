// 자연 음성 재생기 — 서버(Supertonic 3)가 합성한 음성을 문장 단위로 이어 재생한다.
// 문장들을 동시에 합성 요청해 첫 문장이 빨리 나오게 하고, 서버 음성을 못 쓰면 브라우저 음성으로 내려간다.
// 새 안내는 항상 옛 안내를 끊는다 — 늦게 도착한 정보는 위험하다.
const Voice = (() => {
  const RETRY_AFTER_MS = 20000;   // 서버 음성이 한 번 실패해도 영영 기계음으로 남지 않게, 잠시 뒤 다시 시도한다
  let generation = 0, current = null, serverFailedAt = 0;
  const serverVoiceOk = () => Date.now() - serverFailedAt > RETRY_AFTER_MS;

  // 목소리는 /voices 에서 직접 들어보고 고른다. 고른 적이 없으면 서버 기본값(따뜻한 여성 음성)을 쓴다.
  function chosenVoice() {
    try { return localStorage.getItem("heyvision-voice") || ""; } catch { return ""; }
  }

  function stop() {
    generation += 1;
    if (current) { current.pause(); current = null; }
    if ("speechSynthesis" in window) speechSynthesis.cancel();
  }

  function browserSpeak(text) {
    if (!("speechSynthesis" in window)) return;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "ko-KR"; u.rate = 1.15;
    speechSynthesis.speak(u);
  }

  async function fetchClip(sentence, voice) {
    const res = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify(voice ? { text: sentence, voice } : { text: sentence }) });
    if (!res.ok) throw new Error("tts " + res.status);
    return URL.createObjectURL(await res.blob());
  }

  function play(url) {
    return new Promise((resolve) => {
      current = new Audio(url);
      current.onended = current.onerror = () => { URL.revokeObjectURL(url); resolve(); };
      current.play().catch(resolve);
    });
  }

  async function speak(text, voiceOverride) {
    if (!text) return;
    stop();
    const mine = generation, voice = voiceOverride || chosenVoice();
    const sentences = (text.match(/[^.!?。\n]+[.!?。]*/g) || [text]).map((s) => s.trim()).filter(Boolean);
    if (!serverVoiceOk()) return browserSpeak(text);
    // 첫 문장(가장 중요한 안내)을 먼저 합성한다. 그 문장이 재생되는 동안 다음 문장을 합성한다.
    // 전부 동시에 요청하면 짧은 뒷문장이 먼저 끝나 첫 문장이 밀린다 (2026-09-19 실측 2.9초 → 1.3초).
    let next = fetchClip(sentences[0], voice);
    for (let i = 0; i < sentences.length; i += 1) {
      let url;
      try { url = await next; }
      catch { serverFailedAt = Date.now(); if (mine === generation) browserSpeak(sentences.slice(i).join(" ")); return; }
      if (mine !== generation) { URL.revokeObjectURL(url); return; }
      if (i + 1 < sentences.length) { next = fetchClip(sentences[i + 1], voice); next.catch(() => {}); }
      await play(url);
      if (mine !== generation) return;
    }
  }

  return { speak, stop };
})();
