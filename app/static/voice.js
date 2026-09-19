// 자연 음성 재생기 — 서버(Supertonic 3)가 합성한 음성을 문장 단위로 이어 재생한다.
// 첫 문장(가장 중요한 안내)을 먼저 합성하고, 서버 음성을 못 쓰면 브라우저 음성으로 내려간다.
// 새 안내는 항상 옛 안내를 끊는다 — 늦게 도착한 정보는 위험하다.
// 목소리(톤 프리셋)와 속도는 기기마다 저장한다. Voice.mountPicker(요소) 로 고르기 화면을 어디에나 붙일 수 있다.
const Voice = (() => {
  const RETRY_AFTER_MS = 20000;   // 서버 음성이 한 번 실패해도 영영 기계음으로 남지 않게, 잠시 뒤 다시 시도한다
  const PRESET_KEY = "heyvision-voice-preset", SPEED_KEY = "heyvision-voice-speed";
  const SAMPLE = "반갑습니다. 헤이 비전입니다. 열두 시 방향 약 세 걸음 앞에 계단이 있습니다.";
  let generation = 0, current = null, serverFailedAt = 0, catalogPromise = null;
  const serverVoiceOk = () => Date.now() - serverFailedAt > RETRY_AFTER_MS;

  function read(key) { try { return localStorage.getItem(key) || ""; } catch { return ""; } }
  function write(key, value) { try { localStorage.setItem(key, value); } catch { /* 사생활 보호 모드 등 — 이번 화면에서만 적용 */ } }

  const choice = () => ({ preset: read(PRESET_KEY), speed: Number(read(SPEED_KEY)) || 1.0 });
  function choose(presetId) { write(PRESET_KEY, presetId); }
  function setSpeed(factor) { write(SPEED_KEY, String(factor)); }

  function catalog() {
    if (!catalogPromise) {
      catalogPromise = fetch("/api/voices").then((res) => { if (!res.ok) throw new Error("voices " + res.status); return res.json(); })
        .catch((err) => { catalogPromise = null; throw err; });
    }
    return catalogPromise;
  }

  function stop() {
    generation += 1;
    if (current) { current.pause(); current = null; }
    if ("speechSynthesis" in window) speechSynthesis.cancel();
  }

  function browserSpeak(text) {
    if (!("speechSynthesis" in window)) return;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "ko-KR"; u.rate = Math.min(2, 1.1 * choice().speed);
    speechSynthesis.speak(u);
  }

  async function fetchClip(sentence, how) {
    const res = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ text: sentence, preset: how.preset, speed: how.speed }) });
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

  // override: { preset, speed } — 미리듣기처럼 저장값과 다른 목소리로 말할 때만 넘긴다
  async function speak(text, override) {
    if (!text) return;
    stop();
    const mine = generation, how = { ...choice(), ...(override || {}) };
    const sentences = (text.match(/[^.!?。\n]+[.!?。]*/g) || [text]).map((s) => s.trim()).filter(Boolean);
    if (!serverVoiceOk()) return browserSpeak(text);
    // 전부 동시에 요청하면 짧은 뒷문장이 먼저 끝나 첫 문장이 밀린다 (2026-09-19 실측 2.9초 → 1.3초).
    let next = fetchClip(sentences[0], how);
    for (let i = 0; i < sentences.length; i += 1) {
      let url;
      try { url = await next; }
      catch { serverFailedAt = Date.now(); if (mine === generation) browserSpeak(sentences.slice(i).join(" ")); return; }
      if (mine !== generation) { URL.revokeObjectURL(url); return; }
      if (i + 1 < sentences.length) { next = fetchClip(sentences[i + 1], how); next.catch(() => {}); }
      await play(url);
      if (mine !== generation) return;
    }
  }

  const preview = (presetId) => speak(SAMPLE, { preset: presetId });

  // 목소리·속도 고르기 화면을 container 안에 그린다. 스크린리더로 조작 가능한 라디오 묶음 + 미리듣기.
  async function mountPicker(container, options) {
    if (!container) return;
    const announce = (options && options.announce) || (() => {});
    container.classList.add("hv-picker");
    container.textContent = "목소리 목록을 불러오는 중입니다.";
    let data;
    try { data = await catalog(); }
    catch { container.textContent = "목소리 목록을 불러오지 못했습니다. 기본 목소리로 안내합니다."; return; }
    const uid = "hvp" + Math.random().toString(36).slice(2, 7);
    container.textContent = "";

    const voices = document.createElement("fieldset"); voices.className = "hv-picker-group";
    const legend = document.createElement("legend"); legend.textContent = "목소리"; voices.append(legend);
    for (const item of data.presets) {
      const row = document.createElement("div"); row.className = "hv-picker-item";
      const label = document.createElement("label");
      const radio = document.createElement("input"); radio.type = "radio"; radio.name = uid + "-voice"; radio.value = item.id;
      radio.checked = (choice().preset || data.default) === item.id;
      radio.addEventListener("change", () => { choose(item.id); announce(item.name + "로 바꿨습니다."); preview(item.id); });
      const text = document.createElement("span"); text.className = "hv-picker-name"; text.textContent = item.name;
      const note = document.createElement("span"); note.className = "hv-picker-note"; note.textContent = item.note;
      const words = document.createElement("span"); words.className = "hv-picker-text"; words.append(text, note);
      label.append(radio, words);   // 글 묶음을 따로 둬야 긴 이름이 라디오 아래로 떨어지지 않는다
      const listen = document.createElement("button"); listen.type = "button"; listen.className = "hv-picker-listen";
      listen.textContent = "들어보기"; listen.setAttribute("aria-label", item.name + " 들어보기");
      listen.addEventListener("click", () => preview(item.id));
      row.append(label, listen); voices.append(row);
    }

    const speeds = document.createElement("fieldset"); speeds.className = "hv-picker-group hv-picker-speed";
    const speedLegend = document.createElement("legend"); speedLegend.textContent = "말 빠르기"; speeds.append(speedLegend);
    for (const step of data.speeds) {
      const label = document.createElement("label"); label.className = "hv-picker-item";
      const radio = document.createElement("input"); radio.type = "radio"; radio.name = uid + "-speed"; radio.value = String(step.factor);
      radio.checked = Math.abs(choice().speed - step.factor) < 0.01;
      radio.addEventListener("change", () => { setSpeed(step.factor); speak("말 빠르기를 " + step.name + "로 바꿨습니다."); });
      const text = document.createElement("span"); text.className = "hv-picker-name"; text.textContent = step.name;
      label.append(radio, text); speeds.append(label);
    }
    container.append(voices, speeds);
  }

  return { speak, stop, preview, choice, choose, setSpeed, catalog, mountPicker };
})();
