/* ============ Claw / Jarvis — front-end logic ============ */
"use strict";

// ---------------------------------------------------------------------------
// Elements
// ---------------------------------------------------------------------------
const chatLog = document.getElementById("chatLog");
const userInput = document.getElementById("userInput");
const sendBtn = document.getElementById("sendBtn");
const micBtn = document.getElementById("micBtn");
const orbLabel = document.getElementById("orbLabel");
const screenImg = document.getElementById("screenImg");
const screenOff = document.getElementById("screenOff");
const refreshScreen = document.getElementById("refreshScreen");
const shotTime = document.getElementById("shotTime");
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

const heroTemplate = (document.getElementById("welcomeHero") || {}).outerHTML || "";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  history: [],            // [{role, content}] — text-only conversation history
  busy: false,
  orb: "idle",            // idle | listening | thinking | speaking
  settings: {
    assistant_name: "Jarvis",
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

window.__jarvisOpenSettings = () => openSettings();
window.__jarvisNewChat = () => document.getElementById("clearChat")?.click();

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
const JARVIS_TOKEN = window.JARVIS_TOKEN || "";

async function api(path, options = {}) {
  const opts = { credentials: "same-origin", ...options };
  if (JARVIS_TOKEN) {
    opts.headers = { ...(opts.headers || {}), "X-Jarvis-Token": JARVIS_TOKEN };
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    throw new Error(
      "Locked. Open the link printed in the Jarvis terminal to unlock the app."
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
  const hasBrain = Boolean(llm.api_key_set) || /localhost|127\.0\.0\.1/.test(llm.base_url || "");
  modelDot.classList.toggle("live", hasBrain);
  modelStatus.textContent = hasBrain ? `${llm.model || "AI"} connected` : "Demo brain";
  document.querySelector(".brand-sub").textContent =
    hasBrain ? `Claw · ${llm.model || "AI"} connected` : "Claw · demo brain";
}

// ---------------------------------------------------------------------------
// Chat rendering
// ---------------------------------------------------------------------------
function addMessage(role, html) {
  document.getElementById("welcomeHero")?.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const avatar = role === "user" ? "🧑" : "🦞";
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
    <p>Jarvis wants to act on your PC:</p>
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
async function sendMessage(text, opts = {}) {
  const message = (text ?? userInput.value).trim();
  if (!message || state.busy) return;

  if (!opts.hiddenUser) {
    addMessage("user", mdLite(message));
    userInput.value = "";
    userInput.style.height = "auto";
  }

  state.busy = true;
  sendBtn.disabled = true;
  setOrb("thinking");

  const statusPill = addStatus("Thinking…");
  const bubble = addMessage("assistant", "");
  let fullText = "";

  try {
    const res = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Only the message and history cross the wire. safe_mode and the LLM
      // endpoint are decided by the server (see jarvis/server.py).
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
    state.busy = false;
    sendBtn.disabled = false;
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
async function refreshScreenImage() {
  try {
    const res = await api(`/api/screenshot?t=${Date.now()}`);
    if (!res.ok) throw new Error(`Server error ${res.status}`);
    const blob = await res.blob();
    if (screenImg.dataset.blobUrl) URL.revokeObjectURL(screenImg.dataset.blobUrl);
    const url = URL.createObjectURL(blob);
    screenImg.dataset.blobUrl = url;
    screenImg.src = url;
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
// Settings
// ---------------------------------------------------------------------------
const PROVIDERS = {
  openrouter: { base: "https://openrouter.ai/api/v1", model: "openai/gpt-4o-mini" },
  openai:     { base: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  gemini:     { base: "https://generativelanguage.googleapis.com/v1beta/openai/", model: "gemini-2.0-flash" },
  groq:       { base: "https://api.groq.com/openai/v1", model: "llama-3.3-70b-versatile" },
  ollama:     { base: "http://localhost:11434/v1", model: "llama3.2-vision" },
  custom:     { base: "", model: "" },
};
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
  if (!setModel.value.trim() || KNOWN_MODELS.includes(setModel.value.trim())) setModel.value = p.model;
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
  assistantNameEl.textContent = cfg.assistant_name || "Jarvis";
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
    assistant_name: setName.value.trim() || "Jarvis",
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
    addMessage("assistant", `⚙️ Settings saved. I'm <b>${esc(state.settings.assistant_name)}</b>${
      (state.settings.llm?.api_key_set || state.settings.llm?.base_url?.includes("localhost"))
        ? ` and I'm thinking with <code>${esc(state.settings.llm.model)}</code>.`
        : " and my demo brain is active — add an API key in Settings to unlock full intelligence."
    }`);
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
// Boot
// ---------------------------------------------------------------------------
loadSettings();
refreshScreenImage();
