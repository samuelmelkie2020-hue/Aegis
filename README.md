# 🦞 Claw — Your own Jarvis for the PC

A personal AI assistant that can **see your screen** and **control your computer** — like Jarvis.

Ask it in plain English (or by voice) and it will look at your screen, run commands, open apps, type, click, manage files and report system status.

> *"What's on my screen?" · "Open Spotify and play something" · "Run the tests and tell me what failed" · "What's eating my memory?"*

---

## ✨ Features

| | |
|---|---|
| 👀 **Sees your PC** | Captures your screen and analyzes it with a vision model |
| 💻 **Controls your PC** | Runs shell commands, opens apps, types, presses shortcuts, clicks |
| 📂 **File assistant** | Lists, reads, writes and searches files |
| 🖥 **System monitor** | CPU, memory, disk, battery, uptime, top processes |
| 🎙 **Voice in, voice out** | Talk to it (browser speech recognition), it talks back (TTS) |
| 🧠 **Bring your own brain** | Works with OpenAI, Gemini, Groq, OpenRouter — or fully local **Ollama** |
| 🛡 **Safe mode** | Asks your approval before touching your PC (on by default) |
| 🤖 **Demo brain** | Works instantly with *no* API key for basic commands |

---

## 🚀 Quick start

### 💻 The easy way — download the app

Grab the build for your OS from **[Releases](https://github.com/samuelmelkie2020-hue/claw/releases)** and run it:

| OS | File | Then |
|---|---|---|
| **Windows** | `Claw-windows.zip` | Unzip → double-click **`Claw.exe`** |
| **macOS** | `Claw-macos.zip` | Unzip → drag **`Claw.app`** to Applications → open it |
| **Linux** | `Claw-linux.zip` | Unzip → `chmod +x Claw` → run `./Claw` |

No Python, no terminal, no browser tab. Claw opens in **its own window**, and that window *is* the app. 🎉

> **First launch on macOS:** the builds are unsigned, so Gatekeeper will complain. Right-click the app → **Open** → **Open**, or run `xattr -dr com.apple.quarantine /Applications/Claw.app`. You only do this once.
>
> **First launch on Windows:** SmartScreen may show *"Windows protected your PC"* — click **More info** → **Run anyway**.

### 🧑‍💻 Running from source

Requires **Python 3.10+** (3.11 or newer recommended).

```bash
git clone https://github.com/samuelmelkie2020-hue/claw.git
cd claw

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
python run.py                    # desktop window
```

Or skip the typing: double-click **`start.bat`** (Windows) / run **`bash start.sh`** (Mac & Linux) — they set up the virtualenv, install what's missing, and launch the desktop app. Both scripts fall back to the browser automatically if a native webview isn't available.

It works immediately with the built-in **demo brain** (try *"what's on my screen?"*, *"run dir"*, *"open notepad"*).

### 🌐 Prefer the browser?

```bash
python run.py --web              # serves http://127.0.0.1:8000 and opens your browser
python run.py --web --no-open    # just serve, don't open anything
```

The web UI is the same app, and it is still handy for LAN use (see [Safety](#-safety)) or for debugging the frontend with devtools. `start.sh --web` and `start.bat --web` do the same thing.

### 🧰 Command-line flags

| Flag | What it does |
|---|---|
| *none* | Opens the native desktop window (default) |
| `--web` | Serve in the browser instead of a native window |
| `--no-open` | With `--web`: don't launch a browser tab |
| `--port N` | Fixed port instead of a free one (default `8000`) |
| `--host H` | Bind address, e.g. `0.0.0.0` for LAN (default `127.0.0.1`) |
| `--debug` | Verbose logging |
| `--self-check` | Diagnose the install: frozen vs source, data dir, server start/stop, webview availability |

---

## 🧠 Connect a real AI model (30 seconds)

Click **⚙️ Settings** in the app and fill in any OpenAI-compatible provider:

| Provider | API base URL | Example model |
|---|---|---|
| **OpenAI** | `https://api.openai.com/v1` | `gpt-4o-mini`, `gpt-4o` |
| **Google Gemini** | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` |
| **Groq** | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| **OpenRouter** | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini` |
| **Ollama (local, free)** | `http://localhost:11434/v1` | `llama3.2-vision` |

Paste your API key and **Save**. Use a **vision-capable model** so Jarvis can see your screen.

> 🔑 Your key is stored only on your machine — `jarvis_config.json` in the app's data folder (**Windows** `%APPDATA%\Claw`, **macOS** `~/Library/Application Support/Claw`, **Linux** `~/.config/Claw`; the repo folder when running from source — override with `JARVIS_DATA_DIR`) (git-ignored, `chmod 600`) and is never sent back to the browser.

---

## 🗣 How to talk to it

- **Type** in the chat box — Enter sends.
- **🎙 Voice** — click the mic and speak (Chrome/Edge; it also reads replies aloud — toggle with *Speak replies*).
- **Quick actions** — the chips on the left for common requests.

Things you can say:

```
What's on my screen?
Click the blue button in the middle of the screen
Open Chrome and go to youtube.com
Run "git status" and explain it
Write a note to my desktop called todo.txt with my shopping list
Find all PDF files in my Downloads folder
How is my PC doing?
```

---

## 🛡 Safety

- **Ask before acting** (safe mode) is ON by default: Jarvis pauses before running commands, opening apps, typing, clicking or writing files, and shows an **Approve / Deny** card. Turn it off only on machines you fully trust it with.
- **Safe mode cannot be disabled by a browser.** It is read from `jarvis_config.json` on every turn; the page can only *save* a new value, and only through an authenticated request.
- It refuses destructive commands (deleting system files, formatting disks…) unless you explicitly ask.
- **Localhost only by default.** The server binds to `127.0.0.1`, so it is not reachable from your network.
- **Every `/api` call needs an access token.** Claw runs commands and reads files, so the API is not open. A random token is generated on first run and stored in `.jarvis_token` (next to your config, `chmod 600`). The desktop window loads `http://127.0.0.1:<port>/?token=…` once and the page trades it for an HttpOnly session cookie; in `--web` mode the terminal prints that URL for you to open.
- **Your API key never leaves the server.** The Settings dialog shows only whether a key is saved, plus a masked hint (`sk-…1234`). Leaving the key field blank keeps the saved key; tick **Forget the saved key** to delete it.
- Requests whose `Host` header is not this machine are rejected, which blocks DNS-rebinding attacks from a web page you happen to visit.

### What Jarvis will not do

- **Read your credentials.** `~/.ssh`, `~/.aws`, `~/.gnupg`, `.env` files, private keys, `.netrc`, and Jarvis's own config/token are refused by the file tools, whichever way the path is spelled (symlinks are resolved first). Reads outside your home folder are refused too.
- **Write outside your home folder** — and never to its own config or an `authorized_keys`-style file.
- **Repeat a credential into the model.** Anything that looks like a key, token, PEM block, or a password inside a URL is masked (`sk-abc… [redacted]`) before tool output reaches the AI provider.
- **Run an approved action twice.** Approvals live on the server, are bound to your session, expire after 5 minutes, and are consumed on use. The page only ever sends the approval *id*; it cannot change the command.
- **Be nudged into clicking the wrong thing.** Screenshots are downscaled for the model, and `click` maps the coordinates back to your real screen (and refuses points outside it).
- **Hang or run away.** A turn has a 5-minute budget; if you close the tab, the agent stops at its next step instead of working on into the void. Tool results are capped, searches are bounded, and only the newest screenshot is kept in context.
- **Lose your settings silently.** A settings file that cannot be parsed is moved aside (`jarvis_config.json.corrupt-<timestamp>`) and reported in the app, rather than quietly reset to defaults and overwritten.

### If you really want it on your LAN

```bash
JARVIS_HOST=0.0.0.0 JARVIS_ALLOWED_HOSTS=192.168.1.50 python run.py --web
```

The token is still required, so only devices that can read the URL (or the `.jarvis_token` file) can control the PC. Add your host or IP to `JARVIS_ALLOWED_HOSTS`, otherwise the `Host` check rejects it. Only do this on a network you trust.

---

## ⚙️ Configuration

Everything is editable in the app's Settings. Power users can edit `jarvis_config.json` directly:

```json
{
  "assistant_name": "Jarvis",
  "llm": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-...",
    "model": "gpt-4o"
  },
  "safe_mode": true,
  "voice_replies": true
}
```

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `JARVIS_PORT` | `8000` | Port to serve on |
| `JARVIS_HOST` | `127.0.0.1` | Bind address. `0.0.0.0` exposes it on your network (token still required) |
| `JARVIS_CONFIG` | `./jarvis_config.json` | Path to the config file |
| `JARVIS_TOKEN` | *(generated)* | Use a fixed access token instead of `.jarvis_token` |
| `JARVIS_TOKEN_FILE` | `./.jarvis_token` | Where the generated token is stored |
| `JARVIS_ALLOWED_HOSTS` | *(loopback + preview)* | Comma-separated extra `Host` values to accept |
| `JARVIS_EMBED_TOKEN` | `true` on localhost | Force embedding (or not embedding) the token in the served page |
| `JARVIS_NO_GUI_ERRORS` | off | `1` skips the crash window and only writes `claw-crash.log` (used by CI and headless machines) |
| `JARVIS_DATA_DIR` | platform data folder | Where the config, token and clapboard files live (ignored when `JARVIS_CONFIG` is set) |
| `JARVIS_ALLOW_READ_OUTSIDE_HOME` | off | `1` lets the file tools read outside your home folder (credentials stay blocked) |
| `JARVIS_ALLOW_WRITE_OUTSIDE_HOME` | off | `1` lets `write_file` write outside your home folder (config and tokens stay blocked) |

---

## 🧑‍💻 Development

```bash
pip install -r requirements-dev.txt   # runtime deps + pytest + ruff
python -m pytest -q                   # 209 tests, ~3s, no display or API key needed
ruff check .                          # lint (config lives in pyproject.toml)
python run.py --self-check            # diagnose a machine: data dir, server, webview
node --check jarvis/static/app.js     # front-end syntax
```

The suite runs headless: it redirects `HOME`, the config file and the token file
to a temp directory before any `jarvis` module is imported, so it can never
touch your real settings, and it fakes the display, the model and the mouse.
CI runs the tests on 3.10–3.12 plus lint and a JS syntax check (`.github/workflows/ci.yml`).
A second workflow (`.github/workflows/desktop.yml`) freezes the app with PyInstaller on
Windows, macOS and Linux, smoke-tests each binary with `--self-check`, and attaches the
archives to the GitHub Release whenever a `v*` tag is pushed.

Front-end changes are covered by `tests/test_frontend.py`, which asserts that
every element id the script uses exists, that the UI keeps its accessibility
contract (live regions, dialog semantics, visible focus), and that no API call
is hardcoded to `localhost` (which would break behind the preview proxy).

| Area | Where |
|---|---|
| Access control (token, Host/Origin) | `jarvis/security.py`, `tests/test_security.py` |
| Settings validation + secret masking | `jarvis/config.py`, `tests/test_config.py` |
| Path policy + redaction | `jarvis/safety.py`, `tests/test_tools.py` |
| Agent loop, safe mode, approvals | `jarvis/llm.py`, `jarvis/approvals.py`, `tests/test_agent.py` |
| SSE contract | `jarvis/server.py`, `tests/test_streaming.py` |
| Desktop shell, port/server lifecycle, packaging | `jarvis/desktop.py`, `packaging/claw.spec`, `tests/test_desktop.py` |

### Building the desktop app yourself

```bash
pip install -r requirements-dev.txt pyinstaller
bash packaging/build.sh          # Windows: packaging\build.ps1
```

The result lands in `dist/` — `Claw.exe`, `Claw.app`, or a `Claw` binary. The spec is
`packaging/claw.spec`: one-file, windowed (no console), with `jarvis/static` bundled.
Always run `dist/Claw --self-check` after freezing — a build that works from source but
not frozen is the most common packaging failure.

## 🧩 How it works

```
┌──────────────┐   chat / voice    ┌────────────────────────────┐
│  Web UI      │ ◄───────────────► │  FastAPI server            │
│  (desktop    │   live SSE stream │  (uvicorn, own thread)     │
│   window or  │                   │                            │
│   browser)   │                   │   ┌────────────────────┐   │
                                   │   │  Agent loop (LLM)  │   │
                                   │   └─────────┬──────────┘   │
                                   │             │ tool calls   │
                                   │   ┌─────────▼──────────┐   │
                                   │   │  Tools             │   │
                                   │   │  see_screen        │   │
                                   │   │  run_command       │   │
                                   │   │  open_app / type   │   │
                                   │   │  click / keys      │   │
                                   │   │  files / system    │   │
                                   │   └────────────────────┘   │
                                   └────────────────────────────┘
```

0. `run.py` picks a free port, starts uvicorn on `127.0.0.1` in a background thread, then
   hands that URL to a native window via **pywebview** (`jarvis/desktop.py`). The web UI in
   that window is the same one `--web` serves; it talks to the server over loopback, so no
   code is duplicated between the desktop and browser versions.
1. You ask something (text or voice). The request carries only your message and the conversation — never settings.
2. The server checks your session, then calls a vision-capable LLM with a set of **tools**, using the policy (safe mode, provider) it loaded from disk.
3. The LLM decides what to do — e.g. `see_screen` → look at the screenshot → `click(512, 340)` → `type_text("hello")`.
4. Results (including screenshots) stream back to the chat in real time.
5. If an action needs approval, the server registers it, shows a card, and — once you click — runs that stored action itself, exactly once, then resumes the conversation with the result.

### Streaming protocol (for alternate clients)

`POST /api/chat` returns `text/event-stream`. Each event is one `data:` line of JSON with a `type`:

| `type` | Meaning |
|---|---|
| `status` | `{message}` — keep-alive-ish progress note |
| `tool_start` / `tool_result` | `{name, args}` then `{name, result, ok}` |
| `approval` | `{approval_id, name, args, description}` — POST the id to `/api/approve` |
| `screenshot` | `{data}` — base64 PNG |
| `message` | `{text}` — markdown-ish reply text |
| `error` | `{message}` — the only failure event; render this text once |
| `done` | `{ok, text}` — exactly one per stream, always last |

`: ping` comment lines are sent every 15 s so proxies do not drop a stream during a slow model call.

Without an API key, a built-in **demo brain** handles simple intents so you can try everything right away.

---

## 🖥 Platform notes

- **Windows** — fully supported. If typing/shortcuts don't land, run the terminal as your user (not as a service) and give Python accessibility permissions.
- **macOS** — grant **Screen Recording** + **Accessibility** permissions to **Claw** (or your terminal,
  when running from source) in *System Settings → Privacy & Security*, or screenshots/typing will be blocked.
- **Linux** — needs `WebKitGTK` for the desktop window (`libwebkit2gtk-4.1-0`, plus `python3-gi`) and
  an X11/Wayland session with `xdg-utils` for opening apps. If the webview is missing, `start.sh`
  falls back to the browser; `python run.py --self-check` tells you which pieces are present.
- **Desktop window vs browser** — the window is Chrome-less and owns the app lifecycle (closing it
  stops the server). Use `--web` when you want devtools, LAN access from a phone, or a normal tab.

---

## 🧪 Troubleshooting

| Problem | Fix |
|---|---|
| *"Could not open requirements file"* | You are not in the `claw` folder — run `cd claw` first, or use **start.bat** (it handles everything). |
| *"TLS/SSL connection has been closed"* / can't reach the AI model | Your network is blocking the AI provider (not your key). Press **🔌 Test connection** in Settings; try another Provider, a VPN, or Ollama (local). If you're chatting inside the Arena **preview**: that sandbox is always blocked from AI sites — run Jarvis on your own PC. |
| "Screen capture unavailable" | Run it on a machine with a real display; on macOS grant Screen Recording permission. |
| Mic button says unsupported | Voice input needs Chrome or Edge (Web Speech API). |
| "I couldn't reach the AI model" | Check base URL / key / model in ⚙️ Settings. Ollama users: make sure `ollama serve` is running and you `ollama pull llama3.2-vision`. |
| Jarvis acts but nothing types | Focus the window you want to type into — typing goes to whatever is focused. |
| Port already in use | `python run.py --port 8080` (or `JARVIS_PORT=8080`) — the starters honour it too |
| *"No module named webview"* | `pip install pywebview`; on Linux also install WebKitGTK. Or run `python run.py --web` |
| Desktop window is blank / white | Check `claw-crash.log` in the data folder; run `python run.py --self-check` and retry with `--debug` |
| Desktop app won't start at all (Windows) | `claw-crash.log` sits next to the exe's data folder (`%APPDATA%\Claw`); the message box shows the reason |
| macOS says the app is damaged / from an unidentified developer | It is unsigned: right-click → **Open**, or `xattr -dr com.apple.quarantine /Applications/Claw.app` |
| Where are my settings? | `--self-check` prints the data folder: `%APPDATA%\Claw`, `~/Library/Application Support/Claw` or `~/.config/Claw` |

---

Made with 🦞 — your friendly neighborhood claw.
