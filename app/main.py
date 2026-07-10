from __future__ import annotations

import html
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse


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
    direct_url: str | None = None
    host_supported: bool | None = None


@dataclass(frozen=True)
class QueueItem:
    gid: str
    status: str
    name: str
    total_length: int
    completed_length: int
    download_speed: int
    eta_seconds: int | None
    error_message: str | None
    can_pause: bool
    can_resume: bool
    can_remove: bool
    can_reorder: bool
    can_clear: bool

    @property
    def progress_percent(self) -> float:
        if self.total_length <= 0:
            return 0.0
        return min(100.0, (self.completed_length / self.total_length) * 100)


@dataclass(frozen=True)
class QueueSnapshot:
    active: list[QueueItem]
    waiting: list[QueueItem]
    stopped: list[QueueItem]


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
        result = await self._rpc(
            "aria2.addUri",
            [
                [direct_url],
                {
                    "dir": self.settings.aria2_download_dir,
                    "max-connection-per-server": self.settings.aria2_max_connection_per_server,
                    "split": self.settings.aria2_split,
                },
            ],
        )
        if not isinstance(result, str) or not result:
            raise UpstreamError("aria2 did not return a download id.")
        return result

    async def queue_snapshot(self) -> QueueSnapshot:
        active = await self.tell_active()
        waiting = await self.tell_waiting()
        stopped = await self.tell_stopped()
        return QueueSnapshot(
            active=[parse_queue_item(item) for item in active],
            waiting=[parse_queue_item(item) for item in waiting],
            stopped=[parse_queue_item(item) for item in stopped],
        )

    async def tell_active(self) -> list[dict[str, Any]]:
        return await self._rpc_list("aria2.tellActive")

    async def tell_waiting(self, offset: int = 0, num: int = 100) -> list[dict[str, Any]]:
        return await self._rpc_list("aria2.tellWaiting", [offset, num])

    async def tell_stopped(self, offset: int = 0, num: int = 50) -> list[dict[str, Any]]:
        return await self._rpc_list("aria2.tellStopped", [offset, num])

    async def pause(self, gid: str) -> None:
        await self._rpc("aria2.pause", [gid])

    async def unpause(self, gid: str) -> None:
        await self._rpc("aria2.unpause", [gid])

    async def remove(self, gid: str) -> None:
        await self._rpc("aria2.remove", [gid])

    async def remove_download_result(self, gid: str) -> None:
        await self._rpc("aria2.removeDownloadResult", [gid])

    async def purge_download_result(self) -> None:
        await self._rpc("aria2.purgeDownloadResult")

    async def change_position(self, gid: str, position: int, how: str) -> None:
        await self._rpc("aria2.changePosition", [gid, position, how])

    async def move(self, gid: str, direction: str) -> None:
        if direction == "top":
            await self.change_position(gid, 0, "POS_SET")
        elif direction == "up":
            await self.change_position(gid, -1, "POS_CUR")
        elif direction == "down":
            await self.change_position(gid, 1, "POS_CUR")
        elif direction == "bottom":
            await self.change_position(gid, 0, "POS_END")
        else:
            raise ValueError("Unsupported queue move direction.")

    async def _rpc_list(
        self,
        method: str,
        params: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        result = await self._rpc(method, params)
        if not isinstance(result, list):
            raise UpstreamError("aria2 returned an unexpected queue response.")
        return [item for item in result if isinstance(item, dict)]

    async def _rpc(self, method: str, params: list[Any] | None = None) -> Any:
        rpc_params: list[Any] = []
        if self.settings.aria2_rpc_secret:
            rpc_params.append(f"token:{self.settings.aria2_rpc_secret}")
        if params:
            rpc_params.extend(params)

        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": rpc_params,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(self.settings.aria2_rpc_url, json=payload)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError("aria2 JSON-RPC request failed.") from exc

        if not isinstance(data, dict) or data.get("error"):
            raise UpstreamError("aria2 rejected the JSON-RPC request.")
        return data.get("result")


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
            direct_url=direct_url,
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


def get_aria2_client() -> Aria2Client:
    return Aria2Client(Settings.from_env())


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


@app.get("/queue", response_class=HTMLResponse)
async def queue(
    message: Optional[str] = Query(None),
    level: str = Query("success"),
    aria2: Aria2Client = Depends(get_aria2_client),
) -> str:
    try:
        snapshot = await aria2.queue_snapshot()
        return render_queue_page(snapshot, message=message, level=level)
    except UpstreamError as exc:
        logger.warning("aria2 queue read failed: %s", exc)
        return render_queue_page(
            None,
            message="Download queue is temporarily unavailable.",
            level="error",
        )


@app.post("/queue/{gid}/pause")
async def queue_pause(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> RedirectResponse:
    return await run_queue_action(aria2.pause(gid), "Download paused.")


@app.post("/queue/{gid}/resume")
async def queue_resume(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> RedirectResponse:
    return await run_queue_action(aria2.unpause(gid), "Download resumed.")


@app.post("/queue/{gid}/remove")
async def queue_remove(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> RedirectResponse:
    return await run_queue_action(aria2.remove(gid), "Download removed from queue.")


@app.post("/queue/{gid}/clear")
async def queue_clear(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> RedirectResponse:
    return await run_queue_action(aria2.remove_download_result(gid), "History entry cleared.")


@app.post("/queue/clear-stopped")
async def queue_clear_stopped(
    aria2: Aria2Client = Depends(get_aria2_client),
) -> RedirectResponse:
    return await run_queue_action(aria2.purge_download_result(), "Stopped history cleared.")


@app.post("/queue/{gid}/move")
async def queue_move(
    gid: str,
    direction: str = Form(...),
    aria2: Aria2Client = Depends(get_aria2_client),
) -> RedirectResponse:
    try:
        action = aria2.move(gid, direction)
    except ValueError:
        return queue_redirect("Unsupported queue move direction.", "error")
    return await run_queue_action(action, "Download moved.")


async def run_queue_action(action: Any, success_message: str) -> RedirectResponse:
    try:
        await action
    except (UpstreamError, ValueError) as exc:
        logger.warning("aria2 queue action failed: %s", exc)
        return queue_redirect("Queue action failed. Please try again.", "error")
    return queue_redirect(success_message, "success")


def queue_redirect(message: str, level: str) -> RedirectResponse:
    query = urlencode({"message": message, "level": level})
    return RedirectResponse(f"/queue?{query}", status_code=303)


def render_page(result: DownloadResult | None = None) -> str:
    result_html = ""
    if result is not None:
        status = "success" if result.ok else "error"
        details: list[str] = []
        if result.filename:
            details.append(f"File: {html.escape(result.filename)}")
        if result.aria2_gid:
            details.append(f"aria2 id: {html.escape(result.aria2_gid)}")
        if result.direct_url:
            escaped_url = html.escape(result.direct_url, quote=True)
            details.append(
                f'Real-Debrid URL: <a href="{escaped_url}">{html.escape(result.direct_url)}</a>',
            )
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
          .nav {{
            display: flex;
            gap: 1rem;
            margin: 0 0 1.25rem;
          }}
          .nav a {{
            color: var(--accent);
            font-weight: 700;
          }}
          a {{
            color: inherit;
            overflow-wrap: anywhere;
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
          <nav class="nav" aria-label="Main navigation">
            <a href="/">Submit</a>
            <a href="/queue">Queue</a>
          </nav>
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


def render_queue_page(
    snapshot: QueueSnapshot | None,
    message: str | None = None,
    level: str = "success",
) -> str:
    message_html = ""
    if message:
        status = "error" if level == "error" else "success"
        message_html = (
            f'<section class="result {status}" role="status">'
            f"<p>{html.escape(message)}</p>"
            "</section>"
        )

    sections = ""
    if snapshot is not None:
        sections = "\n".join(
            [
                render_queue_section("Active", snapshot.active, "No active downloads."),
                render_queue_section("Waiting", snapshot.waiting, "No waiting downloads."),
                render_queue_section(
                    "Stopped",
                    snapshot.stopped,
                    "No completed, removed, or failed downloads.",
                    include_clear_all=True,
                ),
            ],
        )

    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Download Queue - Real-Debrid Downloader</title>
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
            --danger: #9d2a1f;
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
              --danger: #ff8b7d;
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
            max-width: 62rem;
          }}
          h1 {{
            font-size: clamp(2rem, 6vw, 3.5rem);
            letter-spacing: 0;
            line-height: 1;
            margin: 0 0 1rem;
          }}
          h2 {{
            border-bottom: 1px solid var(--line);
            font-size: 1.2rem;
            margin: 2rem 0 1rem;
            padding-bottom: .5rem;
          }}
          p {{
            color: var(--muted);
            margin: 0 0 1.25rem;
          }}
          .nav, .section-heading, .actions, .move-actions {{
            display: flex;
            flex-wrap: wrap;
            gap: .65rem;
          }}
          .nav {{
            margin: 0 0 1.25rem;
          }}
          .nav a {{
            color: var(--accent);
            font-weight: 700;
          }}
          .section-heading {{
            align-items: center;
            justify-content: space-between;
          }}
          .item {{
            background: var(--control);
            border: 1px solid var(--line);
            border-radius: 8px;
            margin: 0 0 1rem;
            padding: 1rem;
          }}
          .item h3 {{
            font-size: 1rem;
            margin: 0 0 .65rem;
            overflow-wrap: anywhere;
          }}
          .meta {{
            color: var(--muted);
            display: grid;
            gap: .25rem;
            margin: 0 0 .85rem;
          }}
          .progress {{
            background: transparent;
            border: 1px solid var(--line);
            border-radius: 999px;
            height: .75rem;
            margin: .75rem 0;
            overflow: hidden;
          }}
          .progress span {{
            background: var(--accent);
            display: block;
            height: 100%;
          }}
          form {{
            display: inline;
          }}
          button {{
            background: var(--accent);
            border: 0;
            border-radius: 8px;
            color: var(--accent-fg);
            cursor: pointer;
            font: inherit;
            font-weight: 750;
            min-height: 2.5rem;
            padding: .55rem .8rem;
          }}
          button.danger {{
            background: var(--danger);
            color: #ffffff;
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
          <nav class="nav" aria-label="Main navigation">
            <a href="/">Submit</a>
            <a href="/queue">Queue</a>
          </nav>
          <h1>Download Queue</h1>
          <p>Manage active, waiting, and recently stopped aria2 downloads.</p>
          {message_html}
          {sections}
        </main>
      </body>
    </html>
    """


def render_queue_section(
    title: str,
    items: list[QueueItem],
    empty_message: str,
    include_clear_all: bool = False,
) -> str:
    clear_all_html = ""
    if include_clear_all and items:
        clear_all_html = (
            '<form method="post" action="/queue/clear-stopped">'
            '<button class="danger" type="submit">Clear stopped history</button>'
            "</form>"
        )
    heading = (
        '<div class="section-heading">'
        f"<h2>{html.escape(title)}</h2>"
        f"{clear_all_html}"
        "</div>"
    )
    if not items:
        return f"{heading}<p>{html.escape(empty_message)}</p>"
    return heading + "".join(render_queue_item(item) for item in items)


def render_queue_item(item: QueueItem) -> str:
    gid = html.escape(item.gid, quote=True)
    status = html.escape(item.status)
    progress = f"{item.progress_percent:.1f}"
    meta = [
        f"Status: {status}",
        f"aria2 id: {html.escape(item.gid)}",
        f"Size: {format_bytes(item.completed_length)} / {format_bytes(item.total_length)}",
    ]
    if item.download_speed > 0:
        meta.append(f"Speed: {format_bytes(item.download_speed)}/s")
    if item.eta_seconds is not None:
        meta.append(f"ETA: {format_duration(item.eta_seconds)}")
    if item.error_message:
        meta.append(f"Error: {html.escape(item.error_message)}")

    controls: list[str] = []
    if item.can_pause:
        controls.append(queue_button(f"/queue/{gid}/pause", "Pause"))
    if item.can_resume:
        controls.append(queue_button(f"/queue/{gid}/resume", "Resume"))
    if item.can_remove:
        controls.append(queue_button(f"/queue/{gid}/remove", "Remove", danger=True))
    if item.can_clear:
        controls.append(queue_button(f"/queue/{gid}/clear", "Clear", danger=True))
    if item.can_reorder:
        controls.append(
            '<div class="move-actions">'
            + queue_move_button(gid, "top", "Top")
            + queue_move_button(gid, "up", "Up")
            + queue_move_button(gid, "down", "Down")
            + queue_move_button(gid, "bottom", "Bottom")
            + "</div>",
        )

    meta_html = "".join(f"<span>{entry}</span>" for entry in meta)
    controls_html = "".join(controls)
    return (
        '<article class="item">'
        f"<h3>{html.escape(item.name)}</h3>"
        f'<div class="meta">{meta_html}</div>'
        '<div class="progress" aria-hidden="true">'
        f'<span style="width: {progress}%"></span>'
        "</div>"
        f'<div class="actions">{controls_html}</div>'
        "</article>"
    )


def queue_button(action: str, label: str, danger: bool = False) -> str:
    class_name = ' class="danger"' if danger else ""
    return (
        f'<form method="post" action="{action}">'
        f'<button{class_name} type="submit">{html.escape(label)}</button>'
        "</form>"
    )


def queue_move_button(gid: str, direction: str, label: str) -> str:
    return (
        f'<form method="post" action="/queue/{gid}/move">'
        f'<input type="hidden" name="direction" value="{html.escape(direction, quote=True)}">'
        f'<button type="submit">{html.escape(label)}</button>'
        "</form>"
    )


def parse_queue_item(payload: dict[str, Any]) -> QueueItem:
    gid = str(payload.get("gid") or "")
    status = str(payload.get("status") or "unknown")
    total_length = parse_int(payload.get("totalLength"))
    completed_length = parse_int(payload.get("completedLength"))
    download_speed = parse_int(payload.get("downloadSpeed"))
    remaining = max(total_length - completed_length, 0)
    eta_seconds = remaining // download_speed if download_speed > 0 else None
    name = queue_item_name(payload)
    error_message = payload.get("errorMessage")
    return QueueItem(
        gid=gid,
        status=status,
        name=name,
        total_length=total_length,
        completed_length=completed_length,
        download_speed=download_speed,
        eta_seconds=eta_seconds,
        error_message=error_message if isinstance(error_message, str) and error_message else None,
        can_pause=status in {"active", "waiting"},
        can_resume=status == "paused",
        can_remove=status in {"active", "waiting", "paused"},
        can_reorder=status in {"waiting", "paused"},
        can_clear=status in {"complete", "error", "removed"},
    )


def queue_item_name(payload: dict[str, Any]) -> str:
    bittorrent = payload.get("bittorrent")
    if isinstance(bittorrent, dict):
        info = bittorrent.get("info")
        if isinstance(info, dict):
            name = info.get("name")
            if isinstance(name, str) and name:
                return name

    files = payload.get("files")
    if isinstance(files, list):
        for file_payload in files:
            if not isinstance(file_payload, dict):
                continue
            path = file_payload.get("path")
            if isinstance(path, str) and path:
                basename = os.path.basename(path)
                return basename or path
            uris = file_payload.get("uris")
            if isinstance(uris, list):
                for uri_payload in uris:
                    if isinstance(uri_payload, dict):
                        uri = uri_payload.get("uri")
                        if isinstance(uri, str) and uri:
                            parsed = urlparse(uri)
                            basename = os.path.basename(parsed.path)
                            if basename:
                                return basename
                            if parsed.hostname:
                                return f"Download from {parsed.hostname}"

    return f"Download {payload.get('gid') or 'unknown'}"


def parse_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def format_bytes(value: int) -> str:
    size = float(max(value, 0))
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def format_duration(seconds: int) -> str:
    seconds = max(seconds, 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def main() -> None:
    logging.basicConfig(level=os.getenv("APP_LOG_LEVEL", "info").upper())
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8080"))
    log_level = os.getenv("APP_LOG_LEVEL", "info")
    uvicorn.run("app.main:app", host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
