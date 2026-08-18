// Day 10 hardening: a real regression check for app.js's esc()/badge()
// escaping, run under plain Node (no browser, no test framework
// dependency added to the project) via `node frontend/static/test_esc.js`.
// tests/test_frontend_xss.py shells out to this so `pytest tests/` still
// exercises it as part of the one-command test suite.
//
// app.js is a plain browser script (not a module, and it touches
// `document`/`fetch` at load time for tab wiring and the initial
// loadDashboard() call) — stub just enough of the DOM/fetch surface for
// it to load without throwing, then call the real esc()/badge()
// functions it defines globally.

// A stub element with just enough surface (innerHTML, style, classList,
// querySelector) for app.js's boot-time loadDashboard() call to run
// without throwing — this file is testing esc()/badge(), not DOM
// rendering, so the stub only needs to not crash, not to be accurate.
function stubElement() {
  return {
    innerHTML: "",
    style: {},
    classList: { add: () => {}, remove: () => {} },
    querySelector: () => null,
    addEventListener: () => {},
  };
}
global.document = {
  querySelectorAll: () => [],
  getElementById: () => stubElement(),
  querySelector: () => null,
  addEventListener: () => {},
};
global.fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });

const fs = require("fs");
const path = require("path");
const appJsSource = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
// eslint-disable-next-line no-eval
eval(appJsSource);

const HARDCODED_PAYLOADS = [
  '<script>alert(1)</script>',
  '"><img src=x onerror=alert(1)>',
  "'; DROP TABLE runs; --",
  "<b>bold</b> & \"quoted\" 'apos'",
];

// Fix pass after the second audit: also run simulator/injection_corpus/'s
// 12 prompt-injection cases through esc()/badge(). tests/test_injection_defense.py
// already proves these are contained as *agent-instruction* payloads
// (never treated as instructions); running them here proves the
// *separate* claim that if this same untrusted estate content is ever
// rendered as HTML in the Control Tower UI, it can't break out of markup
// either — one corpus, two independently-verified containment properties.
const CORPUS_PATH = path.join(__dirname, "..", "..", "simulator", "injection_corpus", "payloads.json");
const CORPUS_PAYLOADS = JSON.parse(fs.readFileSync(CORPUS_PATH, "utf8")).cases.map((c) => c.payload_text);

const XSS_PAYLOADS = [...HARDCODED_PAYLOADS, ...CORPUS_PAYLOADS];

let failures = 0;

function assert(condition, message) {
  if (!condition) {
    failures += 1;
    console.error("FAIL: " + message);
  } else {
    console.log("PASS: " + message);
  }
}

for (const payload of XSS_PAYLOADS) {
  const escaped = esc(payload);
  assert(!escaped.includes("<script>"), `esc() strips <script> from ${JSON.stringify(payload)}`);
  assert(!escaped.includes('"'), `esc() strips raw " from ${JSON.stringify(payload)}`);
  assert(!escaped.includes("'"), `esc() strips raw ' from ${JSON.stringify(payload)}`);
  assert(!escaped.includes("<") || escaped.includes("&lt;"), `esc() encodes < as &lt; in ${JSON.stringify(payload)}`);

  const badgeHtml = badge(payload);
  assert(!badgeHtml.includes("<script>"), `badge() strips <script> from ${JSON.stringify(payload)}`);
  assert(!badgeHtml.includes('badge-"'), `badge() doesn't let a payload break out of the class attribute`);
}

if (failures > 0) {
  console.error(`\n${failures} XSS escaping check(s) failed.`);
  process.exit(1);
}
console.log(`\nAll ${XSS_PAYLOADS.length * 6} XSS escaping checks passed.`);
