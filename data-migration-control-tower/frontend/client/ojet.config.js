const CopyPlugin = require("copy-webpack-plugin");
const webpack = require("webpack");

module.exports = {
  webpack: ({ context, config }) => {
    config.devtool = false;

    // Absolute asset URLs. Without this webpack emits relative paths
    // ("js/main.<hash>.js"), which the browser resolves against the
    // CURRENT route — so a one-segment deep link like /estates works by
    // luck, and a nested one like /estates/new requests
    // /estates/js/main.js, hits the SPA fallback, receives index.html, and
    // the app never boots. It also breaks runtime-loaded lazy chunks on
    // any nested route. /favicon.svg was already absolute for the same
    // underlying reason.
    config.output = { ...(config.output || {}), publicPath: "/" };

    config.resolve = config.resolve || {};
    config.resolve.alias = { ...(config.resolve.alias || {}), chai: false };
    config.plugins = config.plugins || [];
    config.plugins.push(new CopyPlugin({ patterns: [{ from: "src/favicon.svg", to: "favicon.svg" }] }));
    config.plugins.push(
      new webpack.DefinePlugin({
        __MCT_E2E_BYPASS__: JSON.stringify(process.env.MCT_E2E_BYPASS === "1")
      })
    );

    // Strip stray U+FEFF byte-order marks from emitted CSS.
    //
    // A real defect this caught: the bundler emitted a BOM at the seam
    // where the JET theme ends and src/styles/app.css begins, so the
    // selector became "﻿:root" — which matches nothing. Every
    // --mct-* custom property was therefore undefined at runtime, and
    // `background: var(--mct-red)` computed to transparent while the
    // literal `color: #fff` still applied. The result was white text on a
    // white card: the "Sign in with Google" button was present, focusable
    // and 376x34px, but invisible.
    //
    // A BOM is only meaningful at the very start of a file; anywhere else
    // it is a stray token that silently invalidates the rule it prefixes.
    // Stripping it from emitted CSS is safe and makes the class of bug
    // impossible regardless of which loader introduced it.
    config.plugins.push({
      apply(compiler) {
        const { Compilation, sources } = compiler.webpack;
        compiler.hooks.thisCompilation.tap("StripCssBom", (compilation) => {
          compilation.hooks.processAssets.tap(
            { name: "StripCssBom", stage: Compilation.PROCESS_ASSETS_STAGE_OPTIMIZE },
            (assets) => {
              for (const name of Object.keys(assets)) {
                if (!name.endsWith(".css")) continue;
                const original = assets[name].source().toString();
                const cleaned = original.replace(/﻿/g, "");
                if (cleaned !== original) {
                  compilation.updateAsset(name, new sources.RawSource(cleaned));
                }
              }
            },
          );
        });
      },
    });

    config.optimization = {
      ...config.optimization,
      splitChunks: { chunks: "all" }
    };
    return { context, webpack: config };
  }
};
