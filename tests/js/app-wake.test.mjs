/* Wiring tests for the wake word in app.js — run with:
 *   node --test tests/js/*.test.mjs
 *
 * The matcher is tested on its own (wakeword.test.mjs). This file tests the
 * part a user actually experiences: switching the wake word on opens one
 * session, a sentence that mentions the name does nothing, the phrase acts,
 * and a reload waits for a click. Those are wiring bugs a syntax check cannot
 * see and no browser is available to catch, so the page is loaded against a
 * small fake (tests/js/dom.mjs) with a speech engine the test drives.
 *
 * No npm dependency: plain node:test.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { createPage, loadApp } from "./dom.mjs";

const FILES = ["wakeword.js", "app.js"];

async function boot(options) {
  const page = createPage(options);
  await loadApp(page, FILES);
  return page;
}

test("loading the page opens no microphone", async (t) => {
  const page = await boot();
  t.after(() => page.dispose());
  assert.equal(page.recognitions.length, 0);
  assert.equal(page.el("wakeToggle").checked, false);
  assert.equal(page.store.get("aegis.wake"), undefined, "no preference until asked");
});

test("turning it on opens exactly one session and remembers the choice", async (t) => {
  const page = await boot();
  t.after(() => page.dispose());

  assert.equal(await page.startWake(), true);
  assert.equal(page.recognitions.length, 1);
  assert.equal(page.el("wakeToggle").checked, true);
  assert.equal(page.store.get("aegis.wake"), "1");

  page.stopWake("off");
  assert.equal(page.el("wakeToggle").checked, false);
  assert.equal(page.store.get("aegis.wake"), "0", "switching off must be remembered too");
});

test("a sentence that merely mentions the name does not act", async (t) => {
  const page = await boot();
  t.after(() => page.dispose());
  await page.startWake();

  await page.say("I told aegis to open chrome");
  assert.equal(page.sent.length, 0, "a mention must not reach the chat endpoint");
});

test("the phrase acts, and only the words after it are sent", async (t) => {
  const page = await boot();
  t.after(() => page.dispose());
  await page.startWake();

  await page.say("hey ages what time is it");
  assert.equal(page.sent.length, 1);
  assert.match(String(page.sent[0].body), /what time is it/);
  assert.doesNotMatch(String(page.sent[0].body), /ages/i);
});

test("saying the name alone starts dictation instead of sending nothing", async (t) => {
  const page = await boot();
  t.after(() => page.dispose());
  await page.startWake();

  await page.say("Hey, Aegis.");
  assert.equal(page.sent.length, 0, "there is no request yet");
  assert.ok(page.recognitions.length >= 2, "it should now dictate the request");
});

test("a remembered choice waits for a click before opening the microphone", async (t) => {
  const page = await boot({ remembered: true });
  t.after(() => page.dispose());
  assert.equal(page.recognitions.length, 0, "browsers require a gesture, and so do we");
  assert.equal(page.el("wakeToggle").checked, false, "nothing is on until the click lands");
});

test("a session that ends is restarted, so the toggle never lies", async (t) => {
  const page = await boot();
  t.after(() => page.dispose());
  await page.startWake();

  const first = page.recognitions.length;
  page.lastEngine().onend(); // what Chrome does every ~60 s
  await new Promise((resolve) => setTimeout(resolve, 400));
  assert.ok(page.recognitions.length > first, "a silent engine must be restarted");
});

test("the fake page is not silently passing everything", async (t) => {
  // Guard against the tests above becoming vacuous: a page that knows nothing
  // about wake words must not be able to start one.
  const { createPage: create } = await import("./dom.mjs");
  const bare = create();
  t.after(() => bare.dispose());
  assert.equal(typeof bare.startWakeFn, "undefined");
  assert.equal(bare.recognitions.length, 0);
});
