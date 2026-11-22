/* Tests for the wake word matcher — run with:
 *   node --test tests/js/*.test.mjs
 *
 * These live in tests/js/ rather than the Python suite because the matcher runs
 * in the browser. It is a pure module, so Node can test it directly with no
 * bundler and no browser: the alternative is shipping it untested, and a wake
 * word that fires on the wrong sentence is a privacy bug.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

// The module is a classic script for the browser (no module loader in the
// webview), so the test imports it for its side effect and reads the namespace
// it publishes.
import "../../aegis/static/wakeword.js";

const { matchWake, soundex, soundsLike, wakePhrase, RESTART_MS } = globalThis.AegisWake;

test("soundex groups the mishearings the speech engine actually produces", () => {
  // This is the whole justification for not using ===: a dictation engine has
  // never seen the word "Aegis" and returns the nearest English word.
  assert.equal(soundex("aegis"), soundex("ages"));
  assert.equal(soundex("aegis"), soundex("aegus"));
  assert.notEqual(soundex("aegis"), soundex("allies"));
  assert.equal(soundex(""), "");
  assert.equal(soundex("123"), "");
});

test("the name is matched at the start, with or without a greeting", () => {
  for (const phrase of ["hey aegis", "Hey, Aegis.", "aegis", "hey aegis!", "okay aegis", "yo aegis"]) {
    assert.equal(matchWake(phrase).matched, true, phrase);
  }
});

test("what follows the wake word is kept, so it can act in one breath", () => {
  assert.equal(matchWake("hey aegis what's on my screen").rest, "what's on my screen");
  assert.equal(matchWake("Aegis, open Chrome").rest, "open Chrome");
  assert.equal(matchWake("hey aegis").rest, "");
});

test("plausible mishearings still wake it", () => {
  for (const phrase of ["hey ages", "hey aegus", "hi eegis", "hey aegys"]) {
    assert.equal(matchWake(phrase).matched, true, phrase);
  }
});

test("a sentence that merely mentions the name does not wake it", () => {
  // The most important test here: waking on the wrong sentence means recording
  // someone who did not ask to be recorded.
  for (const phrase of [
    "I told aegis to open chrome",
    "did you ask aegis about it",
    "my friend called aegis yesterday",
  ]) {
    assert.equal(matchWake(phrase).matched, false, phrase);
  }
});

test("the greeting alone is not a wake word", () => {
  for (const phrase of ["hey", "hey what time is it", "hello there", "okay", ""]) {
    assert.equal(matchWake(phrase).matched, false, phrase);
  }
});

test("filler words between the greeting and the name are tolerated", () => {
  assert.equal(matchWake("hey um aegis open chrome").matched, true);
  assert.equal(matchWake("hey um aegis open chrome").rest, "open chrome");
});

test("a different assistant name is honoured", () => {
  assert.equal(matchWake("hey friday", { name: "Friday" }).matched, true);
  assert.equal(matchWake("hey friday", { name: "Aegis" }).matched, false);
  assert.equal(wakePhrase("Friday"), "Hey Friday");
  assert.equal(wakePhrase(""), "Hey Aegis");
});

test("matching is not fooled by case or leading punctuation", () => {
  assert.equal(matchWake("HEY AEGIS do it").matched, true);
  assert.equal(matchWake(",hey aegis").matched, true);
});

test("soundsLike never matches an empty string", () => {
  assert.equal(soundsLike("", "aegis"), false);
  assert.equal(soundsLike("aegis", ""), false);
});

test("the restart interval is a sane number the engine can live with", () => {
  // Chrome ends a recognition session every ~60 s; recycling before that keeps
  // the wake word listening instead of silently dying after a minute.
  assert.ok(RESTART_MS > 10000 && RESTART_MS < 60000, RESTART_MS);
});

test("a name that is not the first thing said is a mention, not a wake", () => {
  // Ordering bug: greeting optional must not become greeting anywhere.
  for (const said of ["what did aegis say", "so hey aegis", "and then aegis opened it"]) {
    assert.equal(matchWake(said, { name: "Aegis" }).matched, false, said);
  }
});

test("the transcript is handed back in full when it does not wake it", () => {
  // The rest is what gets sent; losing it would send a half question.
  const said = "hey aegis set a timer for ten minutes";
  assert.equal(matchWake(said, { name: "Aegis" }).rest, "set a timer for ten minutes");
});
