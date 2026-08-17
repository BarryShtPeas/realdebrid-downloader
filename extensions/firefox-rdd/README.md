# RDD Sender Firefox Extension

Firefox WebExtension for sending links and selected text to a Real-Debrid Downloader instance.

The extension is intended for signed, self-distributed Firefox installs. It is
not listed publicly on addons.mozilla.org.

## Install for local testing

1. Open `about:debugging#/runtime/this-firefox` in Firefox.
2. Click `Load Temporary Add-on...`.
3. Select `extensions/firefox-rdd/manifest.json`.
4. Open the extension options.
5. Enter your RDD base URL, such as `http://localhost:8080`.
6. Click `Test connection`.

## Release tooling

Install the local release dependency:

```bash
cd extensions/firefox-rdd
npm install
```

Run the extension linter:

```bash
npm run lint
```

Build an unsigned package for inspection:

```bash
npm run build
```

Sign an unlisted, self-distributed XPI with Mozilla:

```bash
WEB_EXT_API_KEY="user:..." \
WEB_EXT_API_SECRET="..." \
npm run sign
```

`WEB_EXT_API_KEY` is the AMO JWT issuer and `WEB_EXT_API_SECRET` is the AMO JWT
secret from the Mozilla Developer Hub. Never commit those values. Signed and
unsigned packages are written to `web-ext-artifacts/`, which is gitignored.

The extension version in `manifest.json` must match the app version in
`app/__init__.py`. Bump both together before signing a new release.

## Install a signed XPI

1. Build and sign the extension with `npm run sign`.
2. Open Firefox Add-ons and Themes.
3. Open the settings menu and choose `Install Add-on From File...`.
4. Select the signed `.xpi` from `extensions/firefox-rdd/web-ext-artifacts/`.
5. Open the extension options and configure the RDD base URL.

## Usage

- Right-click a link and choose `Send link to RDD`.
- Select text containing one or more URLs, right-click, and choose `Send selected URLs to RDD`.
- Click the toolbar button to open options.

The extension submits data to `POST /api/submit` and tests connectivity with
`GET /api/version`.

## Security

The extension stores only the RDD base URL in Firefox extension storage. It does
not store Real-Debrid tokens, aria2 RPC secrets, submitted URLs, generated
direct download URLs, or cookies.

The configured RDD instance should be local, VPN-only, or protected by a reverse
proxy. This extension does not add authentication to RDD; it only sends URLs to
the configured RDD API.

## Review hygiene

- The manifest keeps a stable Gecko ID for self-distributed updates.
- The extension requests only `menus`, `notifications`, and `storage`.
- Access to the configured RDD origin is requested at runtime through optional
  host permissions.
- The package does not include remote code, credentials, private hostnames, or
  operator-specific defaults.
