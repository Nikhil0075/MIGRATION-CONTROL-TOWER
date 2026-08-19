import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Guards for shipped image assets. Two properties of this app make a
// broken asset reference unusually hard to notice by hand, so they are
// pinned here instead:
//
//   1. The CSP is `img-src 'self' data: blob:` (frontend/app.py). Any
//      remote image — a CDN URL pasted straight from the generator, say —
//      is silently blocked at runtime with nothing in the network tab but
//      a violation.
//   2. `output.publicPath = "/"` plus the SPA catch-all in
//      frontend/app.py means a MISTYPED asset path does not 404. It falls
//      through to index.html and returns 200 with an HTML body, so the
//      browser just renders a broken image and the server log looks fine.

const SRC = resolve(__dirname);
const BRAND_DIR = join(SRC, "assets", "brand", "v1");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      return entry === "assets" || entry === "generated" ? [] : sourceFiles(path);
    }
    return /\.(ts|tsx|html|css)$/.test(entry) ? [path] : [];
  });
}

const SOURCES = sourceFiles(SRC).map((path) => ({ path, text: readFileSync(path, "utf8") }));

describe("shipped image assets", () => {
  it("references no remote images, which the CSP would block", () => {
    const offenders: string[] = [];
    for (const { path, text } of SOURCES) {
      for (const match of text.matchAll(/(?:src|href)\s*=\s*["'](https?:\/\/[^"']+)["']/g)) {
        if (/\.(png|jpe?g|gif|webp|svg|avif)(\?|$)/i.test(match[1])) {
          offenders.push(`${path} -> ${match[1]}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("resolves every /assets reference to a file that exists", () => {
    // The failure this prevents: a typo returns index.html with a 200, so
    // there is no 404 anywhere to notice.
    const missing: string[] = [];
    for (const { path, text } of SOURCES) {
      for (const match of text.matchAll(/["'](\/assets\/[^"']+)["']/g)) {
        const asset = join(SRC, match[1].replace(/^\/assets\//, "assets/"));
        try {
          statSync(asset);
        } catch {
          missing.push(`${path} -> ${match[1]}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });

  it("keeps the brand assets within a size budget", () => {
    // Every byte here is downloaded by every visitor. The generated master
    // is a 4MB 2752px PNG; these are derived down from it, and this is
    // what stops the full-resolution original being dropped in by mistake.
    const budgetKb: Record<string, number> = {
      "logo-horizontal.png": 400,
      "logo-horizontal-reversed.png": 150,
      "logo-horizontal-mono.png": 150,
      "logo-symbol.png": 200,
      "logo-symbol-reversed.png": 100,
      "favicon-32.png": 10,
      "favicon-48.png": 20,
      "favicon-180.png": 60,
      // JPEG, and by far the largest asset: it is the one image a visitor
      // downloads before they can even sign in.
      "architecture-hero.jpg": 300,
    };
    const oversized = Object.entries(budgetKb)
      .map(([name, limit]) => ({ name, kb: statSync(join(BRAND_DIR, name)).size / 1024, limit }))
      .filter(({ kb, limit }) => kb > limit)
      .map(({ name, kb, limit }) => `${name} is ${kb.toFixed(0)}KB, budget ${limit}KB`);
    expect(oversized).toEqual([]);
  });

  it("ships favicons with real transparency, not a painted background", () => {
    // The generator cannot actually produce alpha: asked for a transparent
    // background it PAINTS a grey checkerboard, which previews correctly
    // and is opaque in production. tools/brand_assets.py keys the white
    // out; this asserts that step actually ran on what shipped.
    const png = readFileSync(join(BRAND_DIR, "favicon-32.png"));
    // IHDR colour-type byte: 6 = RGBA, 4 = greyscale+alpha.
    expect([4, 6]).toContain(png[25]);
  });
});

describe("release build", () => {
  const webDir = resolve(SRC, "..", "web", "js");

  it.skipIf(!existsSync(webDir))("ships no end-to-end auth bypass", () => {
    // `npm run test:e2e` compiles a bundle with __MCT_E2E_BYPASS__ = true,
    // which signs in a fake operator with every role and skips Firebase
    // entirely. If that bundle is what gets served, the console is open.
    //
    // Grepping for "MCT_E2E_BYPASS" does NOT detect it — webpack's
    // DefinePlugin substitutes the token in BOTH builds, so the string is
    // absent either way and the check silently always passes. The real
    // marker is the fake session's uid: in a clean build the `if (false)`
    // branch is dead-code-eliminated and the literal disappears.
    const bundle = readdirSync(webDir).find((n) => /^main\..*\.js$/.test(n));
    expect(bundle, "no main bundle found — run npm run build").toBeTruthy();
    const source = readFileSync(join(webDir, bundle!), "utf8");
    expect(source).not.toContain("playwright-operator");
  });
});
