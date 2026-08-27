---
type: reference
title: Firefox Sender Extension
description: Document the local Firefox extension entrypoints, API usage, release tooling, and security boundaries.
tags: [reference, firefox, extension, api]
sources:
  - id: openwiki-source-26a0a6894b5916e63b2ceb6c
    resource: repo://extensions/firefox-rdd/background.js
  - id: openwiki-source-6a821b5fdd9252bf75178a94
    resource: repo://extensions/firefox-rdd/manifest.json
  - id: openwiki-source-940d0ee3536d42376246588b
    resource: repo://extensions/firefox-rdd/options.js
  - id: openwiki-source-18ad44b950599b7217ce5120
    resource: repo://extensions/firefox-rdd/package.json
  - id: openwiki-source-aff79a409f6dbf67202d46b3
    resource: repo://extensions/firefox-rdd/README.md
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
generated: { by: "codex", at: "2026-08-27T23:21:28.898Z" }
verified:
  - by: openwiki/0.4.3
    at: 2026-08-27T23:21:28.898Z
---

# Firefox Sender Extension

The local Firefox WebExtension lives in `extensions/firefox-rdd` and is named `RDD Sender`. It is intended for signed, self-distributed installs rather than public listing.

The extension adds context-menu actions for sending a link or selected text to the configured Real-Debrid Downloader instance. `background.js` submits those values to `POST /api/submit`; `options.js` tests connectivity with `GET /api/version`.

The manifest uses WebExtension manifest v3, a stable Gecko ID, `menus`, `notifications`, and `storage` permissions, plus optional `http://*/*` and `https://*/*` host permissions requested for the configured RDD origin.

The extension stores only the configured RDD base URL in Firefox extension storage. It does not store Real-Debrid tokens, aria2 RPC secrets, submitted URLs, generated direct download URLs, or cookies.

Release tooling is managed by `extensions/firefox-rdd/package.json` with `web-ext lint`, `web-ext build`, and `web-ext sign --channel unlisted`. The extension version in `manifest.json` must match the app version in `app/__init__.py` before signing.

## Related Pages

- [Real-Debrid Downloader Quickstart](../quickstart.md)
- [App Flow](../architecture/app-flow.md)
