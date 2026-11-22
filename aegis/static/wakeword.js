/* Aegis — wake word matching.
 *
 * "Hey Aegis" is recognised by the browser's speech engine, which is a general
 * dictation model: it has never heard of "Aegis", so it writes back whatever
 * English words sound closest — "hey ages", "hey aegis", "hey eejit". Exact
 * string matching fails on nearly every attempt, so this module compares the
 * *sound* of the phrase (a small Soundex) and tolerates the plausible
 * mishearings, while staying strict about everything that is not the wake word.
 *
 * Pure functions, no DOM. Loaded as a classic script by index.html (no build
 * step, no module loader — the desktop webview has to cope too), and also
 * importable from Node for tests: node --test tests/js/*.test.mjs
 */

/** Soundex of one word, or "" when it has no letters. */
function soundex(word) {
  const letters = String(word || "").toLowerCase().replace(/[^a-z]/g, "");
  if (!letters) return "";
  const codes = {
    b: "1", f: "1", p: "1", v: "1",
    c: "2", g: "2", j: "2", k: "2", q: "2", s: "2", x: "2", z: "2",
    d: "3", t: "3",
    l: "4",
    m: "5", n: "5",
    r: "6",
  };
  const first = letters[0];
  let out = first.toUpperCase();
  let previous = codes[first] || "";
  for (const ch of letters.slice(1)) {
    const code = codes[ch] || "";
    // Vowels and h/y separate doubles ("ae-gis" keeps both g's distinct from a
    // following s), so they reset the "same code" run without being encoded.
    if (code && code !== previous) out += code;
    previous = code;
  }
  return (out + "000").slice(0, 4);
}

/** Does `heard` sound like `wanted`? Soundex-equal, or one small edit apart. */
function soundsLike(heard, wanted) {
  const a = String(heard || "").toLowerCase().trim();
  const b = String(wanted || "").toLowerCase().trim();
  if (!a || !b) return false;
  if (a === b) return true;
  if (soundex(a) === soundex(b)) return true;
  // Soundex is coarse: "aegis" and "ages" collide, but "aegis" and "aegis!" do
  // not need it, and a name misheard by two letters still should not match a
  // different word. One substitution/insertion is tolerated, nothing more.
  return editDistance(a, b) <= Math.max(1, Math.floor(b.length / 4));
}

function editDistance(a, b) {
  if (Math.abs(a.length - b.length) > 2) return 99;
  let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i += 1) {
    const current = [i];
    for (let j = 1; j <= b.length; j += 1) {
      current[j] = Math.min(
        previous[j] + 1,
        current[j - 1] + 1,
        previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1),
      );
    }
    previous = current;
  }
  return previous[b.length];
}

/**
 * Did this transcript start with the wake word?
 *
 * Returns `{matched, rest, heard}` — `rest` is what the user said *after* the
 * wake word, so "hey aegis what's on my screen" can act in one breath.
 *
 * Rules, in order:
 *  - `wakeWords` are alternatives to the name ("hey aegis" or the custom one);
 *  - the greeting is optional ("aegis, ..." works);
 *  - the name must be the *first* thing said, so "I told aegis to..." does not
 *    wake it;
 *  - everything after the match is returned untouched.
 */
function matchWake(text, options = {}) {
  const name = String(options.name || "Aegis").trim();
  const greetings = options.greetings || ["hey", "hi", "hello", "ok", "okay", "yo", "hay"];
  const raw = String(text || "");
  const words = raw.trim().split(/\s+/).filter(Boolean);
  if (!words.length || !name) return { matched: false, rest: "", heard: "" };

  // Strip punctuation from the front of the utterance only: "hey, aegis" is one
  // greeting word to a human and two tokens to a split().
  const clean = (word) => word.toLowerCase().replace(/[^\p{L}\p{N}'-]/gu, "");

  let index = 0;
  const first = clean(words[0]);
  const greeter = greetings.some((greeting) => soundsLike(first, greeting));
  if (greeter) index = 1;

  // Allow the greeting to be a separate clause: "hey ... aegis" (the speech
  // engine sometimes inserts a pause word like "um").
  while (index < words.length && /^(um|uh|er|oh)$/.test(clean(words[index]))) index += 1;

  if (index >= words.length) return { matched: false, rest: "", heard: "" };
  const candidate = clean(words[index]);
  if (!soundsLike(candidate, name)) {
    // The greeting alone is not a wake: "hey" starts a lot of sentences.
    return { matched: false, rest: "", heard: candidate };
  }

  const rest = words.slice(index + 1).join(" ").replace(/^[,.\s]+/, "").trim();
  return { matched: true, rest, heard: candidate };
}

/** The phrase to log/announce, with the greeting the user chose. */
function wakePhrase(name) {
  return `Hey ${String(name || "Aegis").trim() || "Aegis"}`;
}

/**
 * How long the engine should run before recycling.
 *
 * Chrome's SpeechRecognition ends itself every ~60 s even in continuous mode,
 * and `continuous` is silently ignored on some platforms, so the caller must
 * restart it. This is that number in one place.
 */
const RESTART_MS = 45000;

// Exposed for app.js, and for the Node tests (which read it off globalThis).
globalThis.AegisWake = { soundex, soundsLike, matchWake, wakePhrase, RESTART_MS };
