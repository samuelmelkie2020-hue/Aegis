/* ============ Aegis — front-end logic ============ */
"use strict";

// ---------------------------------------------------------------------------
// Elements
// ---------------------------------------------------------------------------
const chatLog = document.getElementById("chatLog");
const userInput = document.getElementById("userInput");
const sendBtn = document.getElementById("sendBtn");
const stopBtn = document.getElementById("stopBtn");
const micBtn = document.getElementById("micBtn");
const orbLabel = document.getElementById("orbLabel");
const screenImg = document.getElementById("screenImg");
const screenOff = document.getElementById("screenOff");
const refreshScreen = document.getElementById("refreshScreen");
const shotTime = document.getElementById("shotTime");
const liveToggle = document.getElementById("liveToggle");
const liveBadge = document.getElementById("liveBadge");
const liveBadgeText = document.getElementById("liveBadgeText");
const updateCard = document.getElementById("updateCard");
const updateTitle = document.getElementById("updateTitle");
const updateDetail = document.getElementById("updateDetail");
const updateGet = document.getElementById("updateGet");
const updateInstall = document.getElementById("updateInstall");
const updateLater = document.getElementById("updateLater");
const checkUpdates = document.getElementById("checkUpdates");
const safeModeEl = document.getElementById("safeMode");
const voiceRepliesEl = document.getElementById("voiceReplies");
const assistantNameEl = document.getElementById("assistantName");
const composerHint = document.getElementById("composerHint");
const modelDot = document.getElementById("modelDot");
const modelStatus = document.getElementById("modelStatus");

const settingsModal = document.getElementById("settingsModal");
const settingsBtn = document.getElementById("settingsBtn");
const closeSettings = document.getElementById("closeSettings");
const cancelSettings = document.getElementById("cancelSettings");
const saveSettings = document.getElementById("saveSettings");
const setName = document.getElementById("setName");
const setProvider = document.getElementById("setProvider");
const setBaseUrl = document.getElementById("setBaseUrl");
const setApiKey = document.getElementById("setApiKey");
const setModel = document.getElementById("setModel");
const clearApiKeyEl = document.getElementById("clearApiKey");
const keyState = document.getElementById("keyState");

const localPanel = document.getElementById("localPanel");
const localHint = document.getElementById("localHint");
const localSpecs = document.getElementById("localSpecs");
const localCpu = document.getElementById("localCpu");
const localRam = document.getElementById("localRam");
const localGpu = document.getElementById("localGpu");
const localModels = document.getElementById("localModels");
const localSuggest = document.getElementById("localSuggest");
const localSuggestText = document.getElementById("localSuggestText");
const localUse = document.getElementById("localUse");
const localRefresh = document.getElementById("localRefresh");
const localPull = document.getElementById("localPull");
const pullFill = document.getElementById("pullFill");
const pullText = document.getElementById("pullText");
const pullCancel = document.getElementById("pullCancel");
const cloudPanel = document.getElementById("cloudPanel");
const keyField = document.getElementById("keyField");

const heroTemplate = (document.getElementById("welcomeHero") || {}).outerHTML || "";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  history: [],            // [{role, content}] — text-only conversation history
  busy: false,
  turnId: "",             // server-issued id of the running turn (see /api/chat/stop)
  stopping: false,        // Stop pressed; ignore anything still in flight
  live: false,            // live screen view on?
  liveTimer: null,
  liveLoop: null,         // the in-flight poll chain
  liveFails: 0,
  liveTick: 0,
  liveStalls: 0,
  orb: "idle",            // idle | listening | thinking | speaking
  settings: {
    assistant_name: "Aegis",
    safe_mode: true,
    voice_replies: true,
    llm: { base_url: "", model: "", api_key_set: false, api_key_masked: "" },
  },
};

// ---------------------------------------------------------------------------
// Desktop shell
// ---------------------------------------------------------------------------
// In the desktop build (pywebview) the native menu calls these, and a couple of
// browser-only hints are pointless. In the browser build nothing here runs.
const isDesktop = Boolean(window.pywebview && window.pywebview.api);

window.__aegisOpenSettings = () => openSettings();
window.__aegisNewChat = () => document.getElementById("clearChat")?.click();

function initDesktop() {
  if (!window.pywebview || !window.pywebview.api) return;
  document.documentElement.classList.add("desktop");
  // The window's own chrome replaces these hints.
  const hint = document.getElementById("composerHint");
  if (hint) hint.innerHTML = "<kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> new line";
}

window.addEventListener("pywebviewready", initDesktop);

// ---------------------------------------------------------------------------
// API access
// ---------------------------------------------------------------------------
// The token is injected by /config.js for loopback and preview requests only.
// When it is absent the page is being served from somewhere we do not trust, so
// every /api call will (correctly) come back 401.
const AEGIS_TOKEN = window.AEGIS_TOKEN || "";

async function api(path, options = {}) {
  const opts = { credentials: "same-origin", ...options };
  if (AEGIS_TOKEN) {
    opts.headers = { ...(opts.headers || {}), "X-Aegis-Token": AEGIS_TOKEN };
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    throw new Error(
      "Locked. Open the link printed in the Aegis terminal to unlock the app."
    );
  }
  return res;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function mdLite(text) {
  // very small markdown-ish renderer: code blocks, inline code, bold, italics
  let out = esc(text);
  out = out.replace(/```([\s\S]*?)```/g, (_, code) => `<pre><code>${code.trim()}</code></pre>`);
  out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<i>$2</i>");
  return out;
}

function nowTime() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function setOrb(mode) {
  state.orb = mode;
  const labels = { idle: "Standing by", listening: "Listening…", thinking: "Thinking…", speaking: "Speaking…" };
  orbLabel.textContent = labels[mode] || "Standing by";
  // The HUD repeats the state in large type over the orb.
  const big = { idle: "Standby", listening: "Listening", thinking: "Processing", speaking: "Responding" };
  const stateEl = document.getElementById("stateLabel");
  if (stateEl) stateEl.textContent = big[mode] || "Standby";
}

function scrollBottom() {
  chatLog.scrollTop = chatLog.scrollHeight;
}

let toastTimer = null;
function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2400);
}

function updateModelStatus(cfg) {
  const llm = cfg.llm || {};
  const local = isLocalBase(llm.base_url);
  // A local endpoint is only "connected" once a model is chosen; without one the
  // server answers 404 and the honest label is still "demo brain".
  const hasBrain = local ? Boolean(llm.model) : Boolean(llm.api_key_set);
  const label = local
    ? `💻 ${llm.model || "set up a model"} — on this PC`
    : `${llm.model || "AI"} connected`;
  modelDot.classList.toggle("live", hasBrain);
  modelStatus.textContent = hasBrain ? label : "Demo brain";
  document.querySelector(".brand-sub").textContent =
    hasBrain ? `Aegis · ${label}` : "Aegis · demo brain";
}

// ---------------------------------------------------------------------------
// Chat rendering
// ---------------------------------------------------------------------------
function addMessage(role, html) {
  document.getElementById("welcomeHero")?.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const avatar = role === "user" ? "🧑" : "🛡";
  wrap.innerHTML = `
    <div class="avatar">${avatar}</div>
    <div class="msg-body">
      <div class="bubble">${html}</div>
      <div class="msg-meta">
        <span class="msg-time">${nowTime()}</span>
        ${role === "assistant" ? '<button class="copy-btn" title="Copy">📋 copy</button>' : ""}
      </div>
    </div>`;
  chatLog.appendChild(wrap);
  scrollBottom();
  return wrap.querySelector(".bubble");
}

// copy assistant replies to the clipboard
chatLog.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const bubble = btn.closest(".msg-body").querySelector(".bubble");
  try {
    navigator.clipboard.writeText(bubble.innerText);
    toast("Copied to clipboard ✓");
  } catch { /* clipboard may be unavailable */ }
});

function addStatus(text) {
  const pill = document.createElement("div");
  pill.className = "status-pill";
  pill.innerHTML = `<span class="dot"></span><span>${esc(text)}</span>`;
  chatLog.appendChild(pill);
  scrollBottom();
  return pill;
}

const TOOL_ICONS = {
  see_screen: "👁", run_command: "⌨️", open_app: "🚀", type_text: "✍️",
  press_keys: "🎛", click: "🖱", system_info: "📊", list_files: "📂",
  read_file: "📄", write_file: "📝", search_files: "🔎", clipboard: "📋",
};

function addToolCard(name, args) {
  const card = document.createElement("div");
  card.className = "tool-card";
  const argsStr = JSON.stringify(args || {});
  const icon = TOOL_ICONS[name] || "🔧";
  card.innerHTML = `
    <div class="tool-head">
      <div class="tool-icon">${icon}</div>
      <div class="tool-name">${esc(name)}</div>
      <div class="tool-badge"><span class="dot"></span>running</div>
    </div>
    <div class="tool-args">${esc(argsStr.length > 160 ? argsStr.slice(0, 160) + "…" : argsStr)}</div>
    <details><summary>result</summary><div class="tool-result">running…</div></details>`;
  chatLog.appendChild(card);
  window._lastToolCard = card;
  scrollBottom();
  return card.querySelector(".tool-result");
}

function addApprovalCard(data) {
  const card = document.createElement("div");
  card.className = "approval-card";
  card.innerHTML = `
    <h4>🛡 Approval needed</h4>
    <p>Aegis wants to act on your PC:</p>
    <div class="approval-desc">${esc(data.description || data.name)}</div>
    <div class="approval-actions">
      <button class="btn approve">✅ Approve &amp; run</button>
      <button class="btn deny">✖ Deny</button>
    </div>`;
  chatLog.appendChild(card);
  scrollBottom();

  const [approveBtn, denyBtn] = card.querySelectorAll("button");

  // The server executes the action itself, from its own copy of the request.
  // This page only ever sends the approval id, so it cannot change what runs.
  approveBtn.onclick = async () => {
    approveBtn.disabled = denyBtn.disabled = true;
    approveBtn.textContent = "Running…";
    try {
      const res = await api("/api/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approval_id: data.approval_id }),
      });
      const result = await res.json();
      if (!res.ok || result.ok === false) throw new Error(result.result || `Server error ${res.status}`);
      card.querySelector(".approval-desc").textContent = result.result || "(done)";
      card.classList.add("resolved");
      approveBtn.textContent = "✅ Done";
      // Resume the same turn: the server already has the result, so this is a
      // redeem notice carrying only the id — it cannot run the action again.
      await sendMessage(`(approval-result) ${JSON.stringify({ approval_id: data.approval_id })}`, {
        hiddenUser: true,
      });
    } catch (err) {
      approveBtn.textContent = "⚠ Failed";
      card.querySelector(".approval-result-note")?.remove();
      const note = document.createElement("div");
      note.className = "approval-result-note";
      note.setAttribute("role", "alert");
      note.textContent = `Could not run it: ${err.message || err}`;
      card.appendChild(note);
    }
  };

  denyBtn.onclick = async () => {
    approveBtn.disabled = denyBtn.disabled = true;
    denyBtn.textContent = "Denied";
    card.classList.add("resolved");
    try {
      await api("/api/approve/deny", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approval_id: data.approval_id }),
      });
    } catch { /* the card is dead either way */ }
    addMessage("user", "🚫 Action denied.");
    state.history.push({ role: "user", content: "(system) I denied that action. Don't retry it." });
  };
}

function markShot() {
  shotTime.textContent = nowTime();
  if (state.live) return;            // a flash at 2 Hz is a strobe, not a signal
  const frame = document.getElementById("screenFrame");
  frame.classList.remove("flash");
  void frame.offsetWidth;            // restart the animation
  frame.classList.add("flash");
}

function setScreenImage(b64) {
  screenImg.src = `data:image/png;base64,${b64}`;
  screenOff.style.display = "none";
  markShot();
}

// ---------------------------------------------------------------------------
// Chat — send + SSE consume
// ---------------------------------------------------------------------------
// One place toggles "a turn is running": the Send button becomes Stop, so the
// control is where the user is already looking, and the keyboard shortcut below
// stays in sync with what is on screen.
function setBusy(busy) {
  state.busy = busy;
  if (!busy) state.stopping = false;
  sendBtn.classList.toggle("hidden", busy);
  sendBtn.disabled = busy;
  stopBtn.classList.toggle("hidden", !busy);
  stopBtn.disabled = !busy || state.stopping;
  stopBtn.textContent = state.stopping ? "Stopping…" : "Stop";
}

async function stopTurn() {
  if (!state.busy) return;
  // A turn that has not announced its id yet (the request is still in flight)
  // is still stoppable: it will be marked on arrival and stopped immediately.
  state.stopping = true;
  setBusy(true);
  if (!state.turnId) return;  // sendMessage stops it the moment the id arrives
  try {
    await api("/api/chat/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turn_id: state.turnId }),
    });
  } catch (err) {
    toast(`Could not stop the turn: ${err.message || err}`);
    state.stopping = false;
    setBusy(true);
  }
}

async function sendMessage(text, opts = {}) {
  const message = (text ?? userInput.value).trim();
  if (!message || state.busy) return;

  if (!opts.hiddenUser) {
    addMessage("user", mdLite(message));
    userInput.value = "";
    userInput.style.height = "auto";
  }

  state.turnId = "";
  state.stopping = false;
  setBusy(true);
  setOrb("thinking");

  const statusPill = addStatus("Thinking…");
  const bubble = addMessage("assistant", "");
  let fullText = "";

  try {
    const res = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Only the message and history cross the wire. safe_mode and the LLM
      // endpoint are decided by the server (see aegis/server.py).
      body: JSON.stringify({ message, history: state.history }),
    });

    if (!res.ok || !res.body) throw new Error(`Server error ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }

        switch (ev.type) {
          case "turn":
            state.turnId = ev.turn_id || "";
            // Stop was pressed while the request was still being set up.
            if (state.stopping) stopTurn();
            break;

          case "status":
            statusPill.querySelector("span:last-child").textContent = ev.message || "Working…";
            break;

          case "tool_start":
            statusPill.remove();
            addToolCard(ev.name, ev.args);
            break;

          case "tool_result": {
            const card = window._lastToolCard;
            if (card) {
              card.querySelector(".tool-result").textContent = ev.result || "(no output)";
              const badge = card.querySelector(".tool-badge");
              badge.classList.add(ev.ok === false ? "bad" : "ok");
              badge.lastChild.textContent = ev.ok === false ? "error" : "done";
              window._lastToolCard = null;
            }
            break;
          }

          case "approval":
            statusPill.remove();
            addApprovalCard(ev);
            break;

          case "screenshot":
            setScreenImage(ev.data);
            break;

          case "message":
          case "token":
            statusPill.remove();
            fullText += ev.text || "";
            bubble.innerHTML = mdLite(fullText);
            scrollBottom();
            break;

          case "error":
            // Single terminal event: render it in the reply bubble, not a
            // second one, so a failure is never shown twice.
            statusPill.remove();
            fullText = `⚠ ${ev.message || "Unknown error"}`;
            bubble.innerHTML = mdLite(fullText);
            bubble.classList.add("is-error");
            scrollBottom();
            break;

          case "done":
            statusPill.remove();
            if (ev.stopped) {
              // A deliberate stop is not a failure: keep whatever was already
              // said, and say plainly that it stopped.
              const note = ev.timed_out
                ? "⏱ Stopped — that took longer than five minutes."
                : "⏹ Stopped at your request.";
              if (!fullText) bubble.innerHTML = mdLite(note);
              else bubble.innerHTML = mdLite(`${fullText}\n\n_${note}_`);
              bubble.classList.add("is-stopped");
              scrollBottom();
              break;
            }
            if (ev.ok === false) {
              if (!fullText) bubble.innerHTML = mdLite("⚠ That request failed.");
            } else if (ev.text && !fullText) {
              fullText = ev.text;
              bubble.innerHTML = mdLite(fullText);
            }
            break;
        }
      }
    }
  } catch (err) {
    statusPill.remove();
    bubble.innerHTML = mdLite(`⚠ Something went wrong: ${err.message || err}`);
  } finally {
    if (!fullText) bubble.innerHTML = mdLite("(no response)");
    // Record the turn exactly once, and only after it is sent — the server
    // appends the message itself, so pushing it early duplicated every turn.
    if (!opts.hiddenUser) state.history.push({ role: "user", content: message });
    state.history.push({ role: "assistant", content: fullText || "(no response)" });
    setBusy(false);
    state.turnId = "";
    setOrb("idle");
    if (!opts.hiddenUser) userInput.focus();
    scrollBottom();
    if (fullText && voiceRepliesEl.checked) speak(fullText);
  }
}

// ---------------------------------------------------------------------------
// Voice output (TTS)
// ---------------------------------------------------------------------------
function speak(text) {
  try {
    const synth = window.speechSynthesis;
    if (!synth) return;
    synth.cancel();
    // strip markdown/emoji-heavy symbols for cleaner speech
    const clean = text
      .replace(/```[\s\S]*?```/g, " code block omitted. ")
      .replace(/[*_#>`]/g, "")
      .replace(/https?:\/\/\S+/g, "link")
      .slice(0, 1200);
    const u = new SpeechSynthesisUtterance(clean);
    const voices = synth.getVoices();
    const pref = voices.find((v) => v.lang.startsWith(navigator.language || "en") && !v.name.includes("Google")) ||
                 voices.find((v) => v.lang.startsWith("en")) || voices[0];
    if (pref) u.voice = pref;
    u.rate = 1.03;
    u.onstart = () => setOrb("speaking");
    u.onend = () => setOrb(state.busy ? "thinking" : "idle");
    synth.speak(u);
  } catch { /* TTS is best-effort */ }
}

// ---------------------------------------------------------------------------
// Voice input (Web Speech API)
// ---------------------------------------------------------------------------
let recognition = null;
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;

micBtn.addEventListener("click", () => {
  if (!SpeechRec) {
    composerHint.textContent = "Voice input isn't supported in this browser — try Chrome or Edge.";
    return;
  }
  if (recognition && state.orb === "listening") {
    recognition.stop();
    return;
  }
  recognition = new SpeechRec();
  recognition.lang = navigator.language || "en-US";
  recognition.interimResults = true;
  recognition.continuous = false;

  const finalText = { value: "" };
  recognition.onstart = () => {
    micBtn.classList.add("listening");
    micBtn.setAttribute("aria-pressed", "true");
    setOrb("listening");
    composerHint.textContent = "Listening… speak now (click 🎙 again to stop)";
  };
  recognition.onresult = (e) => {
    let interim = "";
    for (const res of e.results) {
      if (res.isFinal) finalText.value += res[0].transcript;
      else interim += res[0].transcript;
    }
    userInput.value = finalText.value + interim;
  };
  recognition.onerror = () => {
    micBtn.classList.remove("listening");
    micBtn.setAttribute("aria-pressed", "false");
    setOrb("idle");
    composerHint.textContent = "Voice input error — type instead.";
  };
  recognition.onend = () => {
    micBtn.classList.remove("listening");
    micBtn.setAttribute("aria-pressed", "false");
    setOrb("idle");
    composerHint.innerHTML = "<kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> new line · voice works in Chrome/Edge";
    const t = userInput.value.trim();
    if (t) sendMessage(t);
  };
  recognition.start();
});

// ---------------------------------------------------------------------------
// Composer
// ---------------------------------------------------------------------------
sendBtn.addEventListener("click", () => sendMessage());
stopBtn.addEventListener("click", () => stopTurn());

// Esc stops the running turn — the same key that cancels anything else.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !state.busy) return;
  if (!settingsModal.classList.contains("hidden")) return;  // the dialog handles Esc
  e.preventDefault();
  stopTurn();
});

userInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
userInput.addEventListener("input", () => {
  userInput.style.height = "auto";
  userInput.style.height = Math.min(userInput.scrollHeight, 170) + "px";
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => sendMessage(chip.dataset.say));
});

// new conversation
document.getElementById("clearChat").addEventListener("click", () => {
  state.history = [];
  chatLog.innerHTML = heroTemplate;
  toast("Fresh conversation started 🧹");
});

// ---------------------------------------------------------------------------
// Screen panel
// ---------------------------------------------------------------------------
// One place assigns the frame so the previous object URL is always revoked;
// leaking them would hold several megabytes of screenshots per minute live.
function showFrame(blob) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    screenImg.onload = () => {
      if (screenImg.dataset.blobUrl) URL.revokeObjectURL(screenImg.dataset.blobUrl);
      screenImg.dataset.blobUrl = url;
      resolve();
    };
    screenImg.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("the frame could not be decoded"));
    };
    screenImg.src = url;
  });
}

async function refreshScreenImage() {
  try {
    const res = await api(`/api/screenshot?t=${Date.now()}`);
    if (!res.ok) throw new Error(`Server error ${res.status}`);
    await showFrame(await res.blob());
    screenOff.style.display = "none";
  } catch (err) {
    screenOff.style.display = "";
    screenOff.textContent = `Screen unavailable — ${err.message || err}`;
  }
}
screenImg.addEventListener("load", () => {
  screenOff.style.display = "none";
  markShot();
});
refreshScreen.addEventListener("click", refreshScreenImage);

// ---------------------------------------------------------------------------
// Live view
//
// One loop, never two: a frame is the current screen, so overlapping requests
// would queue frames that are already out of date — and each one is a full
// screenshot. The loop schedules the next poll only after the previous frame has
// rendered, which makes the browser the rate limiter even before the server
// clamps it.
//
// It also stops itself: on a hidden tab (nothing is being watched, so nothing
// should be captured), after a run of failures, and when the server says its
// time limit was reached. Off is the default and stays the default.
// ---------------------------------------------------------------------------
const LIVE_INTERVAL_MS = 500;      // 2 fps, matching the server's clamp
const LIVE_MAX_FAILURES = 4;       // a vanishing display must not spin forever
const LIVE_STALL_FRAMES = 6;       // ~3 s without a new frame: say so

function setLiveUI(on) {
  liveBadge.classList.remove("stalled");
  liveBadgeText.textContent = "Live";
  liveToggle.setAttribute("aria-pressed", on ? "true" : "false");
  liveToggle.textContent = on ? "⏸" : "⏺";
  liveToggle.title = on
    ? "Stop watching my screen"
    : "Watch my screen continuously (2 frames a second)";
  liveBadge.hidden = !on;
  document.getElementById("screenFrame").classList.toggle("live", on);
  document.getElementById("screenPanelTitle").textContent = on
    ? "Live screen · recording"
    : "Live screen";
}

async function liveFrameOnce() {
  const res = await api(`/api/live/frame?fps=2&t=${Date.now()}`);
  if (!res.ok) {
    // 503 carries the reason (no display, time limit, permissions).
    let detail = `Screen unavailable (${res.status})`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch { /* not JSON; keep the generic message */ }
    throw new Error(detail);
  }
  await showFrame(await res.blob());
  screenOff.style.display = "none";
  state.liveFails = 0;
  markShot();
  // "Live" must mean a new frame arrived. A badge that keeps claiming Live on a
  // stalled stream is worse than no badge: it says the screen is being watched
  // when it is not.
  const tick = Number(res.headers.get("X-Aegis-Tick") || 0);
  if (tick && tick !== state.liveTick) {
    state.liveTick = tick;
    state.liveStalls = 0;
    liveBadge.classList.remove("stalled");
    liveBadgeText.textContent = "Live";
  } else {
    state.liveStalls += 1;
    if (state.liveStalls >= LIVE_STALL_FRAMES) {
      liveBadge.classList.add("stalled");
      liveBadgeText.textContent = "Stalled";
    }
  }
}

async function liveLoop() {
  if (!state.live) return;
  try {
    await liveFrameOnce();
  } catch (err) {
    state.liveFails += 1;
    if (!state.live) return;                     // stopped while waiting
    if (state.liveFails >= LIVE_MAX_FAILURES) {
      stopLive(`Live view stopped — ${err.message || err}`);
      return;
    }
    screenOff.style.display = "";
    screenOff.textContent = err.message || String(err);
  }
  if (!state.live) return;
  state.liveTimer = setTimeout(liveLoop, LIVE_INTERVAL_MS);
}

function startLive() {
  if (state.live) return;
  state.live = true;
  state.liveFails = 0;
  setLiveUI(true);
  // Say it out loud once: this is a continuous read of the user's screen.
  toast("Live view on — Aegis is watching your screen 🎥");
  liveLoop();
}

function stopLive(message) {
  if (!state.live) return;
  state.live = false;
  clearTimeout(state.liveTimer);
  state.liveTimer = null;
  setLiveUI(false);
  screenOff.style.display = "none";
  if (message) toast(message);
}

liveToggle.addEventListener("click", () => {
  state.live ? stopLive("Live view off") : startLive();
});

// A hidden tab is not being watched: capturing it is cost with no reader.
document.addEventListener("visibilitychange", () => {
  if (document.hidden && state.live) {
    stopLive("Live view paused — the tab went to the background");
  }
});

// ---------------------------------------------------------------------------
// Updates
//
// Three separate steps, on purpose: checking is read-only and happens on its
// own; downloading is a click; installing is a second click that reuses the
// approval flow, because it replaces the program the user is running. Nothing
// here can restart the app without that last click.
// ---------------------------------------------------------------------------
const UPDATE_POLL_MS = 6 * 60 * 60 * 1000;   // twice a day is enough

function showUpdate(stateName, title, detail, opts = {}) {
  updateCard.hidden = false;
  updateCard.classList.toggle("busy", stateName === "busy");
  updateCard.classList.toggle("failed", stateName === "failed");
  updateTitle.textContent = title;
  updateDetail.textContent = detail;
  updateGet.hidden = !opts.canGet;
  updateGet.disabled = stateName === "busy";
  updateInstall.hidden = !opts.canInstall;
  updateGet.textContent = opts.getLabel || "Get it";
}

function hideUpdate() {
  updateCard.hidden = true;
  updateCard.classList.remove("busy", "failed");
}

async function checkForUpdates({ quiet = true } = {}) {
  try {
    const res = await api("/api/update/check");
    const info = await res.json();
    if (!res.ok) throw new Error(info.detail || `HTTP ${res.status}`);

    if (info.available) {
      showUpdate("ready", `Aegis ${info.latest.version} is available`, 
        info.installable
          ? `You are on ${info.current}. Download it, then restart to switch over.`
          : (info.reason || "A newer version exists."),
        { canGet: Boolean(info.installable) });
      return info;
    }
    if (!quiet) {
      showUpdate("ready", "Up to date", info.reason || `You are on ${info.current}.`);
      setTimeout(hideUpdate, 4000);
    }
    return info;
  } catch (err) {
    if (!quiet) {
      showUpdate("failed", "Could not check", err.message || String(err));
      setTimeout(hideUpdate, 6000);
    }
    return null;
  }
}

async function downloadUpdate() {
  showUpdate("busy", "Downloading…", "Verifying the download before it is allowed to replace anything.");
  try {
    const res = await api("/api/update/download", { method: "POST" });
    const info = await res.json();
    if (!res.ok || !info.ok) throw new Error(info.detail || `HTTP ${res.status}`);
    showUpdate("ready", `Aegis ${info.staged.version} is ready`,
      "Verified. It installs the next time you restart.",
      { canInstall: true });
  } catch (err) {
    showUpdate("failed", "Download failed", err.message || String(err));
  }
}

async function installUpdate() {
  showUpdate("busy", "Installing…", "Aegis will close and reopen on the new version.");
  try {
    // Step one: ask the server to register the install. The server decides what
    // runs; the reply is an approval id, not a command.
    const res = await api("/api/update/install", { method: "POST" });
    const info = await res.json();
    if (!res.ok || !info.ok) throw new Error(info.detail || `HTTP ${res.status}`);

    if (!window.confirm(`${info.description}\n\nAegis will close and reopen.`)) {
      await api("/api/approve/deny", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approval_id: info.approval_id }),
      }).catch(() => {});
      showUpdate("ready", "Install cancelled", "Nothing was changed.", { canInstall: true });
      return;
    }

    // Step two: redeem it. The app closes shortly after this returns.
    const done = await api("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approval_id: info.approval_id }),
    });
    const result = await done.json();
    if (!done.ok || !result.ok) throw new Error(result.result || result.detail || "Install failed");
    updateDetail.textContent = "Closing — Aegis will reopen in a moment.";
  } catch (err) {
    showUpdate("failed", "Could not install", err.message || String(err));
  }
}

updateGet.addEventListener("click", downloadUpdate);
updateInstall.addEventListener("click", installUpdate);
updateLater.addEventListener("click", hideUpdate);
checkUpdates.addEventListener("click", () => checkForUpdates({ quiet: false }));

// The user dismissed this version once; do not nag again this session.
async function refreshUpdateState() {
  try {
    const res = await api("/api/update");
    const info = await res.json();
    if (info.staged && info.installable) {
      showUpdate("ready", `Aegis ${info.staged.version} is ready`,
        "Downloaded and verified. Restart to install it.", { canInstall: true });
      return;
    }
    if (info.enabled) checkForUpdates();
  } catch { /* the update UI is optional */ }
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
// "local" providers need no key and never leave this machine. Their model names
// are NOT guessed: the local panel lists what is actually installed and the
// server rejects nothing, so a wrong name only ever shows up as a 404.
const PROVIDERS = {
  ollama:     { base: "http://127.0.0.1:11434/v1", model: "", local: true },
  lmstudio:   { base: "http://127.0.0.1:1234/v1", model: "", local: true },
  openrouter: { base: "https://openrouter.ai/api/v1", model: "openai/gpt-4o-mini" },
  openai:     { base: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  gemini:     { base: "https://generativelanguage.googleapis.com/v1beta/openai/", model: "gemini-2.0-flash" },
  groq:       { base: "https://api.groq.com/openai/v1", model: "llama-3.3-70b-versatile" },
  custom:     { base: "", model: "" },
};
const LOCAL_BASE = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i;

function isLocalBase(url) {
  return LOCAL_BASE.test((url || "").trim());
}
const KNOWN_MODELS = Object.values(PROVIDERS).map((p) => p.model).filter(Boolean);

function inferProvider(baseUrl) {
  const b = (baseUrl || "").toLowerCase();
  for (const [id, p] of Object.entries(PROVIDERS)) {
    if (p.base && b.startsWith(p.base.toLowerCase())) return id;
  }
  return "custom";
}

setProvider.addEventListener("change", () => {
  const p = PROVIDERS[setProvider.value];
  if (!p) return;
  if (setProvider.value !== "custom") setBaseUrl.value = p.base;
  if (!p.local && (!setModel.value.trim() || KNOWN_MODELS.includes(setModel.value.trim()))) {
    setModel.value = p.model;
  }
  syncLocalVisibility();
  if (p.local) loadLocalStatus();
});

// show / hide the API key
document.getElementById("keyToggle").addEventListener("click", () => {
  const show = setApiKey.type === "password";
  setApiKey.type = show ? "text" : "password";
  document.getElementById("keyToggle").textContent = show ? "🙈" : "👁";
});

function applyConfig(cfg) {
  state.settings = cfg;
  safeModeEl.checked = cfg.safe_mode !== false;
  voiceRepliesEl.checked = cfg.voice_replies !== false;
  assistantNameEl.textContent = cfg.assistant_name || "Aegis";
  setName.value = cfg.assistant_name || "";
  setBaseUrl.value = cfg.llm?.base_url || "";
  setProvider.value = inferProvider(cfg.llm?.base_url);
  setModel.value = cfg.llm?.model || "";
  // The real key never reaches the browser; only its presence and a mask.
  setApiKey.value = "";
  clearApiKeyEl.checked = false;
  keyState.textContent = cfg.llm?.api_key_set
    ? `A key is saved (${cfg.llm.api_key_masked}). Leave the field empty to keep it.`
    : "No key saved yet.";
  updateModelStatus(cfg);
  syncLocalVisibility();
}

// A local brain has no key, so hide the field rather than let someone think
// they are missing one. The cloud section stays collapsed unless it is in use.
function syncLocalVisibility() {
  const local = isLocalBase(setBaseUrl.value);
  if (keyField) keyField.hidden = local;
  if (cloudPanel) cloudPanel.open = !local;
}

// ---------------------------------------------------------------------------
// Local model panel
// ---------------------------------------------------------------------------
// Everything here talks to /api/local/*: the server probes the PC, so the page
// never has to (and cannot) reach localhost itself from a browser sandbox.
let localPoll = null;

function fmtGb(n) {
  return n ? `${Number(n).toFixed(1)} GB` : "unknown";
}

function renderLocal(data) {
  if (!data) return;
  const hw = data.hardware || {};
  localCpu.textContent = hw.cpu || "unknown";
  localRam.textContent = hw.ram_gb ? `${fmtGb(hw.ram_gb)}${hw.gpu ? "" : " · no GPU"}` : "unknown";
  localGpu.textContent = hw.gpu
    ? `${hw.gpu}${hw.vram_gb ? ` · ${fmtGb(hw.vram_gb)}` : ""}`
    : "built-in graphics";
  localSpecs.hidden = false;

  const running = (data.servers || []).filter((s) => s.reachable);
  const installed = data.installed || [];

  localHint.className = "local-hint";
  if (running.length) {
    const names = running.map((s) => s.name).join(", ");
    localHint.textContent = installed.length
      ? `✅ ${names} is running on this PC — ${installed.length} model${installed.length === 1 ? "" : "s"} ready. Nothing leaves this computer.`
      : `✅ ${names} is running, but no model is downloaded yet. Pick one below.`;
    localHint.classList.add("ok");
  } else if (data.hint) {
    localHint.textContent = `No AI server found on this PC yet. ${data.hint}`;
    localHint.classList.add("warn");
  } else {
    localHint.textContent = "No AI server found on this PC yet.";
    localHint.classList.add("warn");
  }

  // Installed models, clickable.
  localModels.innerHTML = "";
  if (installed.length) {
    localModels.hidden = false;
    for (const name of installed) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "model-chip";
      if (name === (state.settings.llm?.model || "")) chip.classList.add("active");
      const vision = /vl|llava|vision|moondream|gemma3|minicpm/i.test(name);
      chip.innerHTML = `${esc(name)}${vision ? ' <span class="eyes" title="can see your screen">👁</span>' : ""}`;
      chip.title = vision ? "Can see your screen" : "Text only — no screenshots";
      chip.addEventListener("click", () => {
        setModel.value = name;
        toast(`Model set to ${name} — press Save settings`);
      });
      localModels.appendChild(chip);
    }
  } else {
    localModels.hidden = true;
  }

  // What to download, and its size, before anything starts.
  const rec = data.recommendation || {};
  if (rec.model && !rec.installed) {
    localSuggest.hidden = false;
    localSuggestText.innerHTML = `Recommended for this PC: <b>${esc(rec.model)}</b> — ${esc(rec.detail || "")}`;
    localUse.dataset.model = rec.model;
    localUse.disabled = Boolean(data.pull?.running);
  } else {
    localSuggest.hidden = true;
  }

  renderPull(data.pull || {});
}

function renderPull(pull) {
  const running = Boolean(pull.running);
  localPull.hidden = !running && !pull.error && !pull.status;
  if (!localPull.hidden) {
    const total = Number(pull.total) || 0;
    const done = Number(pull.completed) || 0;
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    pullFill.style.width = `${pct}%`;
    pullText.className = "pull-text";
    if (pull.error) {
      pullText.classList.add("error");
      pullText.textContent = `⚠ ${pull.error}`;
    } else if (running) {
      pullText.textContent = `Downloading ${pull.model} — ${pct}%${total ? ` (${fmtGb(done / 1e9)} of ${fmtGb(total / 1e9)})` : ""} · ${pull.status || "starting"}`;
    } else if (pull.status === "done") {
      pullText.textContent = `✅ ${pull.model} is ready. Press Refresh, then choose it.`;
      pullFill.style.width = "100%";
    } else if (pull.status === "cancelled") {
      pullText.textContent = "Download cancelled — nothing was installed.";
    }
  }
  pullCancel.hidden = !running;
  localUse.disabled = running;
  if (running && !localPoll) {
    localPoll = setInterval(() => loadLocalStatus({ quiet: true }), 1500);
  } else if (!running && localPoll) {
    clearInterval(localPoll);
    localPoll = null;
  }
}

async function loadLocalStatus(opts = {}) {
  if (!localPanel) return;
  if (!opts.quiet) {
    localHint.className = "local-hint";
    localHint.textContent = "Checking this PC…";
  }
  try {
    const res = await api("/api/local/status");
    const data = await res.json();
    if (data && data.ok === false) {
      localHint.className = "local-hint warn";
      localHint.textContent = data.detail || "Could not check for a local AI server.";
      return;
    }
    renderLocal(data);
  } catch (err) {
    localHint.className = "local-hint warn";
    localHint.textContent = `Could not check for a local AI server (${err.message || err}).`;
  }
}

if (localRefresh) localRefresh.addEventListener("click", () => loadLocalStatus());

if (localUse) {
  localUse.addEventListener("click", async () => {
    const model = localUse.dataset.model;
    if (!model) return;
    localUse.disabled = true;
    localUse.textContent = "Starting…";
    try {
      const res = await api("/api/local/pull", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model }),
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.detail || `server ${res.status}`);
      toast(`Downloading ${model}…`);
      loadLocalStatus({ quiet: true });
    } catch (err) {
      pullText.className = "pull-text error";
      pullText.textContent = `⚠ ${err.message || err}`;
      localPull.hidden = false;
    } finally {
      localUse.textContent = "Use this model";
      localUse.disabled = false;
    }
  });
}

if (pullCancel) {
  pullCancel.addEventListener("click", async () => {
    try {
      await api("/api/local/pull/cancel", { method: "POST" });
      toast("Stopping the download…");
    } catch { /* the poll will show it stopped */ }
  });
}

async function loadSettings() {
  try {
    const res = await api("/api/bootstrap");
    if (!res.ok) throw new Error(`Server error ${res.status}`);
    const data = await res.json();
    applyConfig(data.settings);
    if (data.config_problem) {
      const p = data.config_problem;
      addMessage("assistant", `⚠ Your settings file could not be read, so I started from defaults. ${esc(p.detail)}${
        p.backup ? ` The old file was kept at <code>${esc(p.backup)}</code>.` : ""
      } Re-enter your API key in ⚙️ Settings.`);
    }
  } catch (err) {
    addMessage("assistant", `⚠ Could not load settings (${esc(err.message || err)}). Reload the page.`);
  }
}

const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
let lastFocused = null;

function openSettings() {
  lastFocused = document.activeElement;
  settingsModal.classList.remove("hidden");
  // Probe lazily: the check costs a couple of seconds, and nothing on the first
  // screen needs it. Also re-probes, because the user may have just started Ollama.
  loadLocalStatus();
  const first = settingsModal.querySelector(FOCUSABLE);
  if (first) first.focus();
}

function hideSettings() {
  settingsModal.classList.add("hidden");
  // Never strand keyboard focus inside a hidden dialog.
  if (lastFocused && document.contains(lastFocused)) lastFocused.focus();
  lastFocused = null;
}

// Escape closes; Tab cycles inside the dialog instead of escaping to the page.
document.addEventListener("keydown", (e) => {
  if (settingsModal.classList.contains("hidden")) return;
  if (e.key === "Escape") {
    e.preventDefault();
    hideSettings();
    return;
  }
  if (e.key !== "Tab") return;
  const items = [...settingsModal.querySelectorAll(FOCUSABLE)].filter((el) => !el.disabled);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
});
settingsBtn.addEventListener("click", openSettings);
closeSettings.addEventListener("click", hideSettings);
cancelSettings.addEventListener("click", hideSettings);
settingsModal.addEventListener("click", (e) => { if (e.target === settingsModal) hideSettings(); });

saveSettings.addEventListener("click", async () => {
  const candidateKey = setApiKey.value.trim();
  const payload = {
    assistant_name: setName.value.trim() || "Aegis",
    safe_mode: safeModeEl.checked,
    voice_replies: voiceRepliesEl.checked,
    llm: {
      base_url: setBaseUrl.value.trim(),
      model: setModel.value.trim(),
    },
  };
  // Only ever send a key the user just typed. An empty field means "keep the
  // saved one"; the checkbox is the explicit way to delete it.
  if (clearApiKeyEl.checked) payload.llm.api_key = "";
  else if (candidateKey) payload.llm.api_key = candidateKey;
  try {
    const res = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: payload }),
    });
    const saved = await res.json();
    if (saved.ok === false) throw new Error(saved.detail || "rejected by the server");
    applyConfig(saved);
    toast("Settings saved ✓");
    const now = state.settings.llm || {};
    const onThisPc = isLocalBase(now.base_url);
    const ready = onThisPc ? Boolean(now.model) : Boolean(now.api_key_set);
    let line;
    if (ready && onThisPc) {
      line = ` and I'm thinking with <code>${esc(now.model)}</code> — running on this PC, nothing leaves it.`;
    } else if (ready) {
      line = ` and I'm thinking with <code>${esc(now.model)}</code>.`;
    } else if (onThisPc) {
      line = " and my demo brain is active — pick a model in ⚙️ Settings → 💻 This computer to finish setup.";
    } else {
      line = " and my demo brain is active — add an API key in Settings, or switch to 💻 Run on this PC.";
    }
    addMessage("assistant", `⚙️ Settings saved. I'm <b>${esc(state.settings.assistant_name)}</b>${line}`);
  } catch (err) {
    toast("Could not save settings");
    addMessage("assistant", `⚠ Could not save settings: ${err.message || err}`);
  }
  hideSettings();
});

// test the AI connection from the settings fields
const testConnBtn = document.getElementById("testConnBtn");
const testResult = document.getElementById("testResult");
testConnBtn.addEventListener("click", async () => {
  testConnBtn.disabled = true;
  testResult.className = "test-result bad";
  testResult.textContent = "🔌 Testing connection…";
  try {
    const res = await api("/api/test-connection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        // Send the form values, but omit the key unless the user just typed
        // one — the server then tests against the key it already has.
        settings: {
          llm: {
            base_url: setBaseUrl.value.trim(),
            model: setModel.value.trim(),
            ...(setApiKey.value.trim() ? { api_key: setApiKey.value.trim() } : {}),
          },
        },
      }),
    });
    const data = await res.json();
    testResult.textContent = data.detail || "(no response)";
    testResult.className = data.ok ? "test-result ok" : "test-result bad";
  } catch (err) {
    testResult.textContent = "Could not run the test: " + (err.message || err);
    testResult.className = "test-result bad";
  } finally {
    testConnBtn.disabled = false;
  }
});

safeModeEl.addEventListener("change", async () => {
  state.settings.safe_mode = safeModeEl.checked;
  try {
    const res = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: { safe_mode: safeModeEl.checked } }),
    });
    if (!res.ok) throw new Error(`Server error ${res.status}`);
    toast(safeModeEl.checked ? "🛡 Safe mode on" : "Safe mode off — actions run without asking");
  } catch (err) {
    safeModeEl.checked = !safeModeEl.checked;
    toast(`Could not change safe mode: ${err.message || err}`);
  }
});

// ---------------------------------------------------------------------------
// Animated orb
// ---------------------------------------------------------------------------
const canvas = document.getElementById("orb");
const ctx = canvas.getContext("2d");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
let t = 0;

function drawOrb() {
  t += reduceMotion ? 0 : 0.016;
  const w = canvas.width, h = canvas.height;
  const cx = w / 2, cy = h / 2;
  ctx.clearRect(0, 0, w, h);

  const mode = state.orb;
  const speed = mode === "thinking" ? 2.6 : mode === "listening" ? 1.8 : mode === "speaking" ? 2.1 : 1;
  const breathe = 1 + Math.sin(t * 1.4 * speed) * (mode === "idle" ? 0.03 : 0.07);

  // outer glow
  const glow = ctx.createRadialGradient(cx, cy, 20, cx, cy, 152 * breathe);
  const hue = mode === "listening" ? 330 : mode === "thinking" ? 265 : 195;
  glow.addColorStop(0, `hsla(${hue}, 95%, 65%, 0.55)`);
  glow.addColorStop(0.55, `hsla(${hue + 25}, 90%, 55%, 0.16)`);
  glow.addColorStop(1, "transparent");
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, w, h);

  // core with specular highlight
  const core = ctx.createRadialGradient(cx - 18, cy - 22, 8, cx, cy, 86 * breathe);
  core.addColorStop(0, "#eefaff");
  core.addColorStop(0.28, `hsla(${hue}, 92%, 62%, 0.95)`);
  core.addColorStop(1, `hsla(${hue + 40}, 85%, 28%, 0.92)`);
  ctx.beginPath();
  ctx.arc(cx, cy, 86 * breathe, 0, Math.PI * 2);
  ctx.fillStyle = core;
  ctx.fill();

  // inner shimmer
  const shimmer = ctx.createRadialGradient(cx - 30, cy - 34, 4, cx - 20, cy - 26, 60);
  shimmer.addColorStop(0, "rgba(255,255,255,0.55)");
  shimmer.addColorStop(1, "rgba(255,255,255,0)");
  ctx.beginPath();
  ctx.arc(cx - 16, cy - 20, 52, 0, Math.PI * 2);
  ctx.fillStyle = shimmer;
  ctx.fill();

  // orbiting rings
  const ringCount = mode === "idle" ? 2 : 3;
  for (let i = 0; i < ringCount; i++) {
    const phase = t * speed * (0.6 + i * 0.35) + i * 2.1;
    const radius = 108 + i * 16 + Math.sin(phase) * 7;
    ctx.beginPath();
    ctx.ellipse(cx, cy, radius, radius * 0.42, phase * 0.45, 0, Math.PI * 2);
    ctx.strokeStyle = `hsla(${hue + i * 18}, 90%, ${62 - i * 8}%, ${0.5 - i * 0.11})`;
    ctx.lineWidth = 1.6;
    ctx.stroke();
  }

  // particles
  const particles = mode === "idle" ? 12 : 22;
  for (let i = 0; i < particles; i++) {
    const a = t * (0.25 + (i % 5) * 0.12) * speed + i * 0.7;
    const r = 60 + ((i * 37) % 90) + Math.sin(t * speed + i) * 10;
    const x = cx + Math.cos(a) * r;
    const y = cy + Math.sin(a * 1.3) * r * 0.62;
    ctx.beginPath();
    ctx.arc(x, y, 1.4 + (i % 3) * 0.8, 0, Math.PI * 2);
    ctx.fillStyle = `hsla(${hue + 40}, 95%, 75%, ${0.25 + 0.3 * Math.abs(Math.sin(a))})`;
    ctx.fill();
  }

  if (!reduceMotion) requestAnimationFrame(drawOrb);
}
drawOrb();


// ---------------------------------------------------------------------------
// HUD telemetry
//
// Real numbers from /api/stats rather than the mockup's drifting randoms: the
// gauges are a status readout, so a wrong value is worse than no value. A null
// from the server (no battery, no temperature sensor) renders as a dash.
// ---------------------------------------------------------------------------
const GAUGE_CIRCUMFERENCE = 2 * Math.PI * 36;

function setGauge(circleId, valueId, percent, label) {
  const circle = document.getElementById(circleId);
  const text = document.getElementById(valueId);
  if (!circle || !text) return;
  if (percent === null || percent === undefined) {
    circle.style.strokeDashoffset = GAUGE_CIRCUMFERENCE;
    text.textContent = "—";
    return;
  }
  const clamped = Math.max(0, Math.min(100, percent));
  circle.style.strokeDashoffset = GAUGE_CIRCUMFERENCE * (1 - clamped / 100);
  circle.classList.toggle("hot", clamped > 80);
  text.textContent = label;
}

function setBar(barId, textId, percent, label) {
  const bar = document.getElementById(barId);
  const text = document.getElementById(textId);
  if (!bar || !text) return;
  bar.style.width = `${percent === null || percent === undefined ? 0 : Math.max(0, Math.min(100, percent))}%`;
  text.textContent = label;
}

function formatUptime(seconds) {
  if (!seconds && seconds !== 0) return "—";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `UP ${days}d ${hours}h`;
  if (hours) return `UP ${hours}h ${minutes}m`;
  return `UP ${minutes}m`;
}

async function refreshStats() {
  try {
    const res = await api("/api/stats");
    if (!res.ok) throw new Error(`Server error ${res.status}`);
    const stats = await res.json();
    if (!stats.ok) throw new Error(stats.detail || "unavailable");

    setGauge("gCpu", "vCpu", stats.cpu && stats.cpu.percent, `${Math.round(stats.cpu.percent)}%`);
    setGauge("gMem", "vMem", stats.memory && stats.memory.percent, `${Math.round(stats.memory.percent)}%`);

    const temp = stats.temperature;
    setGauge("gTmp", "vTmp", temp === null ? null : Math.min(100, (temp / 100) * 100), temp === null ? "—" : `${Math.round(temp)}°`);

    if (stats.battery) {
      setBar("bBat", "tBat", stats.battery.percent, `${stats.battery.percent}%${stats.battery.plugged ? " ⚡" : ""}`);
    } else {
      setBar("bBat", "tBat", null, "—");
    }

    if (stats.disk) setBar("bDisk", "tDisk", stats.disk.percent, `${stats.disk.free_gb} GB free`);
    else setBar("bDisk", "tDisk", null, "—");

    // Load is per-core average, so normalise against the core count.
    const load = stats.cpu && stats.cpu.load1;
    const cores = (stats.cpu && stats.cpu.cores) || 1;
    if (load === null || load === undefined) {
      setBar("bLoad", "tLoad", null, "—");
    } else {
      setBar("bLoad", "tLoad", (load / cores) * 100, load.toFixed(2));
    }

    const uptimeEl = document.getElementById("uptime");
    if (uptimeEl) uptimeEl.textContent = formatUptime(stats.uptime_seconds);
  } catch (err) {
    // Keep the last good reading on screen; the HUD must not shout on a blip.
    console.warn("stats unavailable:", err.message || err);
  }
}

const clockEl = document.getElementById("clock");
if (clockEl) {
  const tickClock = () => {
    clockEl.textContent = new Date().toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
  };
  tickClock();
  setInterval(tickClock, 1000);
}

refreshStats();
setInterval(refreshStats, 3000);

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
loadSettings().then(() => {
  // First run on a machine with no model: say what to do instead of leaving the
  // user to guess. Only once per load, and only while the demo brain is active.
  const llm = state.settings.llm || {};
  const noBrain = isLocalBase(llm.base_url) ? !llm.model : !llm.api_key_set;
  if (noBrain) {
    addMessage(
      "assistant",
      "🛡 I'm running on the <b>demo brain</b> — I answer without any AI model, so my replies are limited.\n" +
        "To wake up the real brain, open <b>⚙️ Settings → 💻 This computer</b>: it checks your PC and offers " +
        "the best model it can run, then downloads it. <b>No API key, no account, nothing leaves your computer.</b>"
    );
  }
});
refreshScreenImage();

// The version comes from the server, so the footer cannot claim a build that
// is not the one running (it said v1.0 for four releases).
refreshUpdateState();
setInterval(() => checkForUpdates(), UPDATE_POLL_MS);

api("/api/health")
  .then((r) => (r.ok ? r.json() : null))
  .then((info) => {
    const foot = document.getElementById("footNote");
    if (info && info.version && foot) {
      foot.textContent = `v${info.version} · runs on this PC · nothing leaves it`;
    }
  })
  .catch(() => { /* the static text is fine */ });
