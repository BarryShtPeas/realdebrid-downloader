from __future__ import annotations

import html
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Form
from fastapi.responses import HTMLResponse


NO_DOWNLOAD_MESSAGE = "No download available from Real-Debrid for this link."

logger = logging.getLogger("rd_downloader")
app = FastAPI(title="Real-Debrid Downloader")


@dataclass(frozen=True)
class Settings:
    realdebrid_api_token: str | None
    realdebrid_api_base_url: str
    aria2_rpc_url: str
    aria2_rpc_secret: str | None
    aria2_download_dir: str
    aria2_max_connection_per_server: str
    aria2_split: str
    submitted_url_logging: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            realdebrid_api_token=os.getenv("REALDEBRID_API_TOKEN"),
            realdebrid_api_base_url=os.getenv(
                "REALDEBRID_API_BASE_URL",
                "https://api.real-debrid.com/rest/1.0",
            ).rstrip("/"),
            aria2_rpc_url=os.getenv("ARIA2_RPC_URL", "http://rd-aria2:6800/jsonrpc"),
            aria2_rpc_secret=os.getenv("ARIA2_RPC_SECRET"),
            aria2_download_dir=os.getenv("ARIA2_DOWNLOAD_DIR", "/downloads"),
            aria2_max_connection_per_server=os.getenv(
                "ARIA2_MAX_CONNECTION_PER_SERVER",
                "8",
            ),
            aria2_split=os.getenv("ARIA2_SPLIT", "8"),
            submitted_url_logging=os.getenv("APP_SUBMITTED_URL_LOGGING", "false").lower()
            in {"1", "true", "yes", "on"},
        )


@dataclass(frozen=True)
class DownloadResult:
    ok: bool
    message: str
    aria2_gid: str | None = None
    filename: str | None = None
    host_supported: bool | None = None


class DownloadUnavailableError(Exception):
    pass


class ConfigurationError(Exception):
    pass


class UpstreamError(Exception):
    pass


class RealDebridClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def supported_domains(self) -> set[str] | None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.settings.realdebrid_api_base_url}/hosts/domains",
                )
                response.raise_for_status()
                return self._parse_supported_domains(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Real-Debrid supported-host lookup was unavailable: %s", exc)
            return None

    async def check_link(self, submitted_url: str) -> dict[str, Any] | None:
        response = await self._post_authenticated(
            "unrestrict/check",
            data={"link": submitted_url},
        )
        if response.status_code in {400, 404, 503}:
            return None
        self._raise_for_unexpected_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("Real-Debrid check returned invalid JSON") from exc
        return payload if isinstance(payload, dict) else None

    async def unrestrict_link(self, submitted_url: str) -> dict[str, Any]:
        response = await self._post_authenticated(
            "unrestrict/link",
            data={"link": submitted_url},
        )
        if response.status_code in {400, 404, 503}:
            raise DownloadUnavailableError
        self._raise_for_unexpected_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("Real-Debrid unrestrict returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise DownloadUnavailableError
        return payload

    async def _post_authenticated(
        self,
        path: str,
        data: dict[str, str],
    ) -> httpx.Response:
        if not self.settings.realdebrid_api_token:
            raise ConfigurationError("REALDEBRID_API_TOKEN is not configured.")

        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(
                f"{self.settings.realdebrid_api_base_url}/{path}",
                data=data,
                headers={
                    "Authorization": f"Bearer {self.settings.realdebrid_api_token}",
                },
            )

    @staticmethod
    def _parse_supported_domains(payload: Any) -> set[str] | None:
        domains: set[str] = set()

        def add_domain(value: Any) -> None:
            if isinstance(value, str) and value.strip():
                domains.add(value.strip().lower().lstrip("*."))

        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, str):
                    add_domain(item)
                elif isinstance(item, dict):
                    for value in item.values():
                        if isinstance(value, list):
                            for nested in value:
                                add_domain(nested)
                        else:
                            add_domain(value)
        elif isinstance(payload, dict):
            for key, value in payload.items():
                add_domain(key)
                if isinstance(value, list):
                    for nested in value:
                        add_domain(nested)
                elif isinstance(value, dict):
                    for nested in value.values():
                        if isinstance(nested, list):
                            for nested_item in nested:
                                add_domain(nested_item)
                        else:
                            add_domain(nested)
                else:
                    add_domain(value)

        return domains or None

    @staticmethod
    def _raise_for_unexpected_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in {401, 403}:
                raise ConfigurationError(
                    "Real-Debrid rejected the configured API token.",
                ) from exc
            raise UpstreamError(
                f"Real-Debrid returned HTTP {status_code}.",
            ) from exc


class Aria2Client:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def add_uri(self, direct_url: str) -> str:
        params: list[Any] = []
        if self.settings.aria2_rpc_secret:
            params.append(f"token:{self.settings.aria2_rpc_secret}")
        params.extend(
            [
                [direct_url],
                {
                    "dir": self.settings.aria2_download_dir,
                    "max-connection-per-server": self.settings.aria2_max_connection_per_server,
                    "split": self.settings.aria2_split,
                },
            ],
        )
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "aria2.addUri",
            "params": params,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(self.settings.aria2_rpc_url, json=payload)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError("aria2 JSON-RPC request failed.") from exc

        if not isinstance(data, dict) or data.get("error"):
            raise UpstreamError("aria2 rejected the download request.")
        gid = data.get("result")
        if not isinstance(gid, str) or not gid:
            raise UpstreamError("aria2 did not return a download id.")
        return gid


class Downloader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.realdebrid = RealDebridClient(settings)
        self.aria2 = Aria2Client(settings)

    async def submit(self, submitted_url: str) -> DownloadResult:
        self._log_submission(submitted_url)
        host_supported = await self._host_supported(submitted_url)

        try:
            await self.realdebrid.check_link(submitted_url)
        except DownloadUnavailableError:
            return DownloadResult(
                ok=False,
                message=NO_DOWNLOAD_MESSAGE,
                host_supported=host_supported,
            )
        except (ConfigurationError, UpstreamError):
            raise
        except Exception as exc:
            logger.info("Real-Debrid availability check was inconclusive: %s", exc)

        unrestricted = await self.realdebrid.unrestrict_link(submitted_url)
        direct_url = unrestricted.get("download")
        if not isinstance(direct_url, str) or not direct_url.startswith(("http://", "https://")):
            return DownloadResult(
                ok=False,
                message=NO_DOWNLOAD_MESSAGE,
                host_supported=host_supported,
            )

        gid = await self.aria2.add_uri(direct_url)
        filename = unrestricted.get("filename")
        return DownloadResult(
            ok=True,
            message="Download submitted to aria2.",
            aria2_gid=gid,
            filename=filename if isinstance(filename, str) else None,
            host_supported=host_supported,
        )

    async def _host_supported(self, submitted_url: str) -> bool | None:
        domains = await self.realdebrid.supported_domains()
        if domains is None:
            return None
        hostname = urlparse(submitted_url).hostname
        if not hostname:
            return None
        hostname = hostname.lower().removeprefix("www.")
        return any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains)

    def _log_submission(self, submitted_url: str) -> None:
        parsed = urlparse(submitted_url)
        host = parsed.hostname or "unknown-host"
        if self.settings.submitted_url_logging:
            logger.info("Received submitted hoster URL with redacted path for host=%s", host)
            return
        logger.info("Received submitted hoster URL for host=%s", host)


def get_downloader() -> Downloader:
    return Downloader(Settings.from_env())


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return render_page()


@app.post("/submit", response_class=HTMLResponse)
async def submit(
    url: str = Form(...),
    downloader: Downloader = Depends(get_downloader),
) -> str:
    try:
        result = await downloader.submit(url)
    except ConfigurationError as exc:
        logger.warning("Downloader configuration error: %s", exc)
        result = DownloadResult(ok=False, message=str(exc))
    except UpstreamError as exc:
        logger.warning("Downloader upstream error: %s", exc)
        result = DownloadResult(
            ok=False,
            message="Download service is temporarily unavailable. Please try again later.",
        )
    except DownloadUnavailableError:
        result = DownloadResult(ok=False, message=NO_DOWNLOAD_MESSAGE)

    return render_page(result)


def render_page(result: DownloadResult | None = None) -> str:
    result_html = ""
    if result is not None:
        status = "success" if result.ok else "error"
        details: list[str] = []
        if result.filename:
            details.append(f"File: {html.escape(result.filename)}")
        if result.aria2_gid:
            details.append(f"aria2 id: {html.escape(result.aria2_gid)}")
        if result.host_supported is False:
            details.append("Real-Debrid did not list this host, but unrestrict was attempted.")
        detail_html = "".join(f"<p>{detail}</p>" for detail in details)
        result_html = (
            f'<section class="result {status}" role="status">'
            f"<p>{html.escape(result.message)}</p>"
            f"{detail_html}"
            "</section>"
        )

    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Real-Debrid Downloader</title>
        <style>
          :root {{
            color-scheme: light dark;
            --bg: #f7f7f4;
            --fg: #202124;
            --muted: #5f6368;
            --line: #d8d7d0;
            --control: #ffffff;
            --accent: #136f63;
            --accent-fg: #ffffff;
            --success-bg: #e8f4ee;
            --success-fg: #174d32;
            --error-bg: #f9e6e4;
            --error-fg: #8a1f11;
          }}
          @media (prefers-color-scheme: dark) {{
            :root {{
              --bg: #171917;
              --fg: #f4f1ea;
              --muted: #bbb5aa;
              --line: #3e433e;
              --control: #20251f;
              --accent: #3db9a7;
              --accent-fg: #091311;
              --success-bg: #173528;
              --success-fg: #b8f1d0;
              --error-bg: #3b1d1a;
              --error-fg: #ffc3bc;
            }}
          }}
          * {{ box-sizing: border-box; }}
          body {{
            background: var(--bg);
            color: var(--fg);
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            line-height: 1.5;
            margin: 0;
            min-height: 100vh;
            padding: 3rem 1rem;
          }}
          main {{
            margin: 0 auto;
            max-width: 42rem;
          }}
          h1 {{
            font-size: clamp(2rem, 6vw, 3.5rem);
            letter-spacing: 0;
            line-height: 1;
            margin: 0 0 1rem;
          }}
          p {{
            color: var(--muted);
            margin: 0 0 1.25rem;
          }}
          form {{
            border-top: 1px solid var(--line);
            display: grid;
            gap: 1rem;
            padding-top: 1.25rem;
          }}
          label {{
            display: grid;
            font-weight: 650;
            gap: .45rem;
          }}
          input, button {{
            border-radius: 8px;
            font: inherit;
            min-height: 3rem;
            width: 100%;
          }}
          input {{
            background: var(--control);
            border: 1px solid var(--line);
            color: var(--fg);
            padding: .75rem .85rem;
          }}
          button {{
            background: var(--accent);
            border: 0;
            color: var(--accent-fg);
            cursor: pointer;
            font-weight: 750;
            padding: .75rem 1rem;
          }}
          .result {{
            border-radius: 8px;
            margin: 0 0 1.25rem;
            padding: 1rem;
          }}
          .result p {{
            color: inherit;
            margin: 0;
          }}
          .result p + p {{
            margin-top: .35rem;
          }}
          .success {{
            background: var(--success-bg);
            color: var(--success-fg);
          }}
          .error {{
            background: var(--error-bg);
            color: var(--error-fg);
          }}
        </style>
      </head>
      <body>
        <main>
          <h1>Real-Debrid Downloader</h1>
          <p>Submit a hoster link and send the unrestricted download to the internal aria2 worker.</p>
          {result_html}
          <form method="post" action="/submit">
            <label>
              Hoster URL
              <input name="url" type="url" required autocomplete="off" placeholder="https://example.com/file">
            </label>
            <button type="submit">Submit to aria2</button>
          </form>
        </main>
      </body>
    </html>
    """


def main() -> None:
    logging.basicConfig(level=os.getenv("APP_LOG_LEVEL", "info").upper())
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8080"))
    log_level = os.getenv("APP_LOG_LEVEL", "info")
    uvicorn.run("app.main:app", host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
