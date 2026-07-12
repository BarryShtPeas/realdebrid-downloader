# RDD Sender Firefox Extension

Local Firefox WebExtension for sending links and selected text to a Real-Debrid Downloader instance.

## Install for local testing

1. Open `about:debugging#/runtime/this-firefox` in Firefox.
2. Click `Load Temporary Add-on...`.
3. Select `extensions/firefox-rdd/manifest.json`.
4. Open the extension options.
5. Enter your RDD base URL, such as `http://localhost:8080`.
6. Click `Test connection`.

## Usage

- Right-click a link and choose `Send link to RDD`.
- Select text containing one or more URLs, right-click, and choose `Send selected URLs to RDD`.
- Click the toolbar button to open options.

The extension submits data to `POST /api/submit` and tests connectivity with `GET /api/version`.

## Security

The extension stores only the RDD base URL in Firefox extension storage. It does not store Real-Debrid tokens, aria2 RPC secrets, submitted URLs, or cookies.

## Scope

This is an unpacked local MVP. AMO packaging, signing, publishing, and review preparation are intentionally out of scope.
