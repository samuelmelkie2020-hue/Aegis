# Security

Aegis runs on your computer, can see your screen and can act on your machine. That
makes its own security posture part of what it is, not an afterthought. This page
says exactly what protects you, what does not, and where to report a problem.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Use GitHub's private reporting:
**Security → Report a vulnerability** on this repository (or
<https://github.com/samuelmelkie2020-hue/Aegis/security/advisories/new>).

Include what you did, what happened, the version from the app footer (`v1.8.x`),
your OS, and — if you can — the smallest reproduction. You will get an
acknowledgement within a few days. Credit is given in the release notes unless you
ask otherwise.

Supported versions: the current release and the one before it. Older builds are not
patched; the app can update itself (sidebar → *Get it*), which is the fix path for
anything already installed.

## The trust model, in one paragraph

Aegis binds to `127.0.0.1` and requires an access token on every `/api` call. The
only callers it treats as legitimate are the pages it served itself, on this
machine. Everything else — another site in your browser, another machine on your
network, another user on this computer — is treated as hostile by default. The
agent's own actions are then bounded by safe mode (approval cards) plus path and
output policy in `aegis/safety.py`.

## What is protected

| Risk | Control |
|---|---|
| Another website drives the API (CSRF / DNS rebinding) | `Host` header allowlist, `Origin` check on every state-changing call, `Sec-Fetch-Site` check before the token is put in a page, `SameSite` session cookie |
| Anything reads your token | The token file is created `0600` (plus an ACL on Windows), is git-ignored, and the agent's own file tools refuse to read or write it — a prompt injection cannot ask for it |
| The page leaks the token to a third-party host | Only loopback host names receive it, unless you opt in with `AEGIS_PREVIEW_HOST_SUFFIXES` (empty by default) |
| Someone clicks Approve for you | Approval cards are server-side, single-use, expire in 5 minutes, bound to the session that saw them, and the arguments are the server's own — a request cannot swap them |
| Prompt injection in a web page, file or screenshot takes over | Credential folders (`.ssh`, `.aws`, `.gnupg`, …) are unreadable; reads and writes outside your home folder are refused; tool output is scrubbed of credential-shaped strings before it reaches the model |
| The Test-connection button is used to probe your network | Outbound probes are limited to loopback and public addresses; private, link-local and reserved ranges are refused |
| A malicious "update" runs | HTTPS + GitHub host allowlist, SHA-256 must match the digest GitHub publishes, exact byte count, 250 MB ceiling, zip-slip rejected, and the new build must pass its own `--self-check` before it can replace the running one |
| A stolen laptop session browses the API docs | `/docs`, `/redoc` and `/openapi.json` are disabled — they needed no token |
| A runaway turn pins the machine | Every turn has a 300-second budget and a user-facing Stop (`POST /api/chat/stop`, or the button / `Esc`) |

## What is not protected — read this

- **Anything you approve runs with your full user privileges.** Safe mode asks; it does not sandbox. There is
  no container, no seatbelt, no seccomp. If you approve `rm -rf ~`, it will run.
- **A local model is not a filter.** The safety rules above are enforced in code
  around the model, not by prompting it. A model that decides a dangerous command is
  a good idea still has to get past safe mode and the path policy.
- **Screen and file content leaves the machine if you choose a cloud provider.** With
  a local model nothing is uploaded. With OpenAI/Gemini/Groq/OpenRouter the
  screenshots, file contents and clipboard you ask about go to that provider. That is
  a configuration decision, and the app tells you which mode you are in.
- **`AEGIS_HOST=0.0.0.0` exposes the app to your network.** The token is still
  required, but a network you do not control is not a safe place to put a
  full remote-control endpoint.
- **`AEGIS_ALLOWED_HOSTS=*` and `AEGIS_EMBED_TOKEN=1` are for throwaway sandboxes.**
  They exist so a hosted preview can work. With both set, anyone who can reach the
  URL controls the machine.
- **The token file protects against other users and other processes, not against
  malware running as you.** Nothing at user level can.

## Handling a leaked token

A token is a password for your PC. If one leaks (a screenshot, a pasted log, a
committed file):

```bash
# close Aegis, then delete the token so the next start makes a new one
rm ~/.config/Aegis/.aegis_token        # Linux
rm "$HOME/Library/Application Support/Aegis/.aegis_token"   # macOS
del "%APPDATA%\Aegis\.aegis_token"     # Windows
```

(When you run Aegis from source the token lives next to `run.py` instead.)

Start Aegis again and use the new URL it prints. Anything holding the old token is
now locked out — including the browser tab you had open.

## Verifying a download

Every release lists a SHA-256 per asset, and GitHub publishes a digest per asset.
Compare before you run a build:

```bash
sha256sum Aegis-linux.zip      # macOS: shasum -a 256 · Windows: certutil -hashfile Aegis-windows.zip SHA256
```

## Testing this yourself

The security properties above are covered by the test suite (`tests/test_security.py`,
`tests/test_streaming.py`, `tests/test_tools.py`) rather than by prose only:

```bash
python -m pytest -q tests/test_security.py
```
