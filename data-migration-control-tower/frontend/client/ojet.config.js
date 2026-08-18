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
    config.optimization = {
      ...config.optimization,
      splitChunks: { chunks: "all" }
    };
    return { context, webpack: config };
  }
};
