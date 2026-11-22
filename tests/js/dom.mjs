/* A very small page, for testing app.js without a browser.
 *
 * The app is one classic script over plain DOM APIs, so a Proxy that answers
 * every element with another element is enough to load it — and that is worth
 * doing, because the interesting bugs (a listener that never re-arms, a
 * mention that wakes the mic) are wiring bugs, invisible to a syntax check.
 *
 * Nothing here tries to be a browser: no layout, no canvas pixels, no CSS.
 * It records what was asked for and lets a test play the speech engine.
 */

const ctx2d = new Proxy(
  {},
  {
    get(_t, prop) {
      if (prop === "canvas") return { width: 300, height: 300 };
      if (prop === "measureText") return () => ({ width: 10 });
      if (prop === "createRadialGradient" || prop === "createLinearGradient") {
        return () => ({ addColorStop() {} });
      }
      return () => ctx2d;
    },
  },
);

function element() {
  const el = new Proxy(
    {},
    {
      get(target, prop) {
        if (prop in target) return target[prop];
        switch (prop) {
          case "classList":
            return (target.classList = {
              add() {},
              remove() {},
              toggle() {},
              contains() {
                return false;
              },
            });
          case "style":
            return (target.style = {});
          case "dataset":
            return (target.dataset = {});
          case "children":
          case "childNodes":
            return [];
          case "value":
          case "textContent":
          case "innerHTML":
            return "";
          case "width":
          case "height":
            return 300;
          case "checked":
            return false;
          case "getContext":
            return () => ctx2d;
          case "getBoundingClientRect":
            return () => ({ width: 100, height: 100, top: 0, left: 0 });
          case "querySelector":
            return () => element();
          case "querySelectorAll":
            return () => [];
          case "appendChild":
          case "insertBefore":
          case "removeChild":
          case "remove":
          case "focus":
          case "addEventListener":
          case "removeEventListener":
          case "setAttribute":
          case "scrollTo":
          case "click":
          case "play":
          case "pause":
            return () => {};
          default:
            return element();
        }
      },
      set(target, prop, value) {
        target[prop] = value;
        return true;
      },
    },
  );
  return el;
}

/**
 * Build a page: the DOM, the globals app.js expects, and a fake speech engine.
 *
 * @param {object} [options]
 * @param {boolean} [options.remembered] set the wake-word preference in storage
 * @param {object} [options.settings] what /api/bootstrap should answer with
 */
export function createPage(options = {}) {
  const elements = new Map();
  const el = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };

  const store = new Map();
  if (options.remembered) store.set("aegis.wake", "1");
  const sent = [];
  const timers = new Set();

  // A stand-in for Chrome's SpeechRecognition: the test decides what is heard.
  class FakeRecognition {
    constructor() {
      this.started = 0;
      this.stopped = false;
      FakeRecognition.instances.push(this);
    }
    start() {
      this.started += 1;
    }
    stop() {
      this.stopped = true;
      this.onend?.();
    }
    abort() {
      this.stopped = true;
    }
    static instances = [];
  }

  const settings = options.settings || {
    assistant_name: "Aegis",
    safe_mode: true,
    voice_replies: false,
    llm: { base_url: "http://127.0.0.1:11434/v1", model: "fake", api_key_set: true },
  };

  const document = {
    getElementById: el,
    createElement: () => element(),
    querySelector: () => element(),
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    body: element(),
    head: element(),
    documentElement: element(),
    readyState: "complete",
  };

  const page = {
    document,
    navigator: {
      language: "en-US",
      // The microphone grant: allowed, so startWake proceeds.
      mediaDevices: { getUserMedia: async () => ({ getTracks: () => [] }) },
    },
    localStorage: {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, String(value)),
      removeItem: (key) => store.delete(key),
    },
    fetch: async (url, init) => {
      if (String(url).includes("/api/chat")) sent.push({ url, body: init?.body });
      const payload = String(url).includes("bootstrap") ? { settings } : { reply: "ok" };
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => payload,
        text: async () => JSON.stringify(payload),
        body: null,
      };
    },
    setTimeout: (fn, ms) => {
      const id = setTimeout(() => {
        timers.delete(id);
        fn();
      }, ms);
      timers.add(id);
      return id;
    },
    clearTimeout: (id) => {
      timers.delete(id);
      clearTimeout(id);
    },
    setInterval: (fn, ms) => {
      const id = setInterval(fn, ms);
      id.unref?.(); // the HUD poll must not keep the test runner alive
      return id;
    },
    clearInterval: (id) => clearInterval(id),
    queueMicrotask,
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    console,
    performance,
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    SpeechRecognition: FakeRecognition,
    webkitSpeechRecognition: FakeRecognition,
    SpeechSynthesisUtterance: class {},
    speechSynthesis: { speak() {}, cancel() {}, getVoices: () => [] },
    URL,
    Blob: class {},
    TextDecoder,
    TextEncoder,
    FormData: class {},
    AbortController,
    Event: class {},
    location: { href: "http://localhost:8000/", origin: "http://localhost:8000", reload() {} },
    alert() {},
    confirm: () => false,
    prompt: () => null,
    scrollTo() {},
    JSON,
    Math,
  };
  page.window = page;
  page.self = page;
  page.globalThis = page;
  page.parent = page;
  page.addEventListener = () => {};
  page.removeEventListener = () => {};

  page.el = el;
  page.sent = sent;
  page.recognitions = FakeRecognition.instances;
  page.lastEngine = () => FakeRecognition.instances.at(-1);
  page.say = async (transcript, { settle = 900 } = {}) => {
    page.lastEngine().onresult({ results: [[{ transcript }]] });
    await new Promise((resolve) => setTimeout(resolve, settle));
  };
  page.dispose = () => {
    for (const id of timers) clearTimeout(id);
    timers.clear();
    FakeRecognition.instances.length = 0;
  };
  page.store = store;
  return page;
}

/** Load the page's scripts into a vm context, in the order index.html does. */
export async function loadApp(page, files) {
  const fs = await import("node:fs");
  const vm = await import("node:vm");
  vm.createContext(page);
  page.AEGIS_TOKEN = "test-token";
  for (const file of files) {
    const source = fs.readFileSync(new URL(`../../aegis/static/${file}`, import.meta.url), "utf8");
    vm.runInContext(source, page, { filename: file });
  }
  // A function declared at the top level of a classic script is a property of
  // the global object; reading it once avoids a name that shadows the helper.
  page.startWakeFn = page.startWake;
  page.stopWakeFn = page.stopWake;
  // The top-level functions of a classic script live in the context, so a test
  // can call exactly what the UI calls instead of a copy of it.
  page.startWake = () => page.startWakeFn();
  page.stopWake = (reason) => page.stopWakeFn(reason || "");
  return page;
}
