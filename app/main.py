from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app import __version__
from app.views import (
    is_group_clearable,
    project_groups_for_display,
    render_page,
    render_queue_page,
    render_queue_sections,
)


NO_DOWNLOAD_MESSAGE = "No download available from Real-Debrid for this link."
NO_TORRENT_READY_MESSAGE = "Torrent is not ready on Real-Debrid yet. Please try again later."
TORRENT_PROCESSING_FAILED_MESSAGE = "Real-Debrid could not process this magnet link."
NO_SUPPORTED_ARCHIVE_MESSAGE = "Could not find a supported archive start file."
PLAY_MULTI_LINK_MESSAGE = "Play supports one hoster URL at a time. Use Download for multi-link or multipart submissions."
PLAY_MAGNET_MESSAGE = "Play does not support magnet links yet. Use Download to send magnet links to the queue."
PLAY_UNSUPPORTED_MESSAGE = "This Real-Debrid link is not marked as browser-streamable. Use Download to send it to aria2."
PLAY_SESSION_EXPIRED_MESSAGE = "Playback session expired. Paste the link again to start a new player."
TORRENT_ERROR_STATUSES = {"magnet_error", "error", "virus", "dead"}
PLAY_SESSION_TTL_SECONDS = 6 * 60 * 60

logger = logging.getLogger("rd_downloader")


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> Any:
    settings = Settings.from_env()
    task: asyncio.Task[Any] | None = None
    try:
        await Aria2Client(settings).configure_queue_options()
    except UpstreamError as exc:
        logger.warning("aria2 queue configuration failed during startup: %s", exc)
    if settings.group_poll_seconds > 0:
        task = asyncio.create_task(group_monitor_loop(settings))
        fastapi_app.state.group_monitor_task = task
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


APP_NAME = "Real-Debrid Downloader"


app = FastAPI(
    title=APP_NAME,
    version=__version__,
    description="Self-hosted API and web UI for submitting Real-Debrid downloads to aria2.",
    lifespan=lifespan,
)


class ApiSubmitRequest(BaseModel):
    url: str = Field(
        ...,
        min_length=1,
        description="One hoster URL, magnet link, or free text containing one or more links.",
    )


class ApiDownloadResult(BaseModel):
    ok: bool
    message: str
    aria2_gid: Optional[str] = None
    filename: Optional[str] = None
    host_supported: Optional[bool] = None
    group_id: Optional[str] = None
    submitted_hostname: Optional[str] = None


class ApiGroupPart(BaseModel):
    aria2_gid: str
    filename: Optional[str] = None
    status: str
    total_length: int
    completed_length: int
    progress_percent: float


class ApiGroupResult(BaseModel):
    id: str
    name: str
    original_hosts: list[str]
    extraction_status: str
    progress_percent: float
    complete_parts: int
    total_parts: int
    parts: list[ApiGroupPart]


class ApiSubmitResponse(BaseModel):
    ok: bool
    message: str
    downloads: list[ApiDownloadResult]
    group: Optional[ApiGroupResult] = None


class ApiQueueItem(BaseModel):
    gid: str
    status: str
    name: str
    total_length: int
    completed_length: int
    download_speed: int
    eta_seconds: Optional[int]
    progress_percent: float
    can_pause: bool
    can_resume: bool
    can_remove: bool
    can_reorder: bool
    can_clear: bool


class ApiQueueResponse(BaseModel):
    active: list[ApiQueueItem]
    waiting: list[ApiQueueItem]
    stopped: list[ApiQueueItem]
    groups: list[ApiGroupResult]


class ApiQueueMoveRequest(BaseModel):
    position: int = Field(
        ...,
        description="Zero-based target position in aria2's waiting queue.",
    )


class ApiMessageResponse(BaseModel):
    ok: bool
    message: str


class ApiVersionResponse(BaseModel):
    name: str
    version: str


class HealthResponse(BaseModel):
    status: str


@dataclass(frozen=True)
class Settings:
    realdebrid_api_token: str | None
    realdebrid_api_base_url: str
    aria2_rpc_url: str
    aria2_rpc_secret: str | None
    aria2_download_dir: str
    aria2_max_connection_per_server: str
    aria2_max_concurrent_downloads: int
    aria2_split: str
    submitted_url_logging: bool
    app_download_dir: str
    group_state_file: str
    extract_timeout_seconds: int
    group_poll_seconds: int
    queue_stream_interval_seconds: float
    torrent_poll_seconds: float
    torrent_ready_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        config_dir = os.getenv("APP_CONFIG_DIR", "/config")
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
            aria2_max_concurrent_downloads=parse_env_int_range(
                "ARIA2_MAX_CONCURRENT_DOWNLOADS",
                default=1,
                minimum=1,
                maximum=3,
            ),
            aria2_split=os.getenv("ARIA2_SPLIT", "8"),
            submitted_url_logging=os.getenv("APP_SUBMITTED_URL_LOGGING", "false").lower()
            in {"1", "true", "yes", "on"},
            app_download_dir=os.getenv("APP_DOWNLOAD_DIR", "/downloads"),
            group_state_file=os.getenv(
                "APP_GROUP_STATE_FILE",
                os.path.join(config_dir, "download-groups.json"),
            ),
            extract_timeout_seconds=parse_env_int("APP_EXTRACT_TIMEOUT_SECONDS", 7200),
            group_poll_seconds=parse_env_int("APP_GROUP_POLL_SECONDS", 30),
            queue_stream_interval_seconds=parse_env_float("APP_QUEUE_STREAM_INTERVAL_SECONDS", 1.0),
            torrent_poll_seconds=parse_env_float("APP_TORRENT_POLL_SECONDS", 5.0),
            torrent_ready_timeout_seconds=parse_env_int("APP_TORRENT_READY_TIMEOUT_SECONDS", 900),
        )


@dataclass
class DownloadResult:
    ok: bool
    message: str
    aria2_gid: str | None = None
    filename: str | None = None
    direct_url: str | None = None
    host_supported: bool | None = None
    group_id: str | None = None
    local_path: str | None = None
    submitted_hostname: str | None = None
    source_label: str | None = None


@dataclass
class PlayResult:
    ok: bool
    message: str
    filename: str | None = None
    direct_url: str | None = None
    stream_url: str | None = None
    mime_type: str | None = None
    realdebrid_id: str | None = None
    streamable: bool | None = None
    host_supported: bool | None = None
    submitted_hostname: str | None = None
    source_label: str | None = None


@dataclass(frozen=True)
class PlaySession:
    id: str
    direct_url: str
    filename: str | None
    mime_type: str | None
    created_at: float


@dataclass(frozen=True)
class SubmissionResult:
    ok: bool
    message: str
    downloads: list[DownloadResult]
    group: "DownloadGroup | None" = None


@dataclass
class DownloadGroupPart:
    aria2_gid: str
    filename: str | None
    local_download_path: str | None
    status: str
    error: str | None = None
    total_length: int = 0
    completed_length: int = 0


@dataclass
class DownloadGroup:
    id: str
    name: str
    created_at: str
    updated_at: str
    original_hosts: list[str]
    parts: list[DownloadGroupPart]
    extraction_status: str = "pending"
    extraction_error: str | None = None
    extraction_output_path: str | None = None

    @property
    def progress_percent(self) -> float:
        total = sum(part.total_length for part in self.parts)
        completed = sum(part.completed_length for part in self.parts)
        if total <= 0:
            if self.parts and all(part.status == "complete" for part in self.parts):
                return 100.0
            return 0.0
        return min(100.0, (completed / total) * 100)

    @property
    def complete_parts(self) -> int:
        return sum(1 for part in self.parts if part.status == "complete")


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


class GroupStateStore:
    def __init__(self, state_file: str) -> None:
        self.state_file = state_file

    def load_groups(self) -> list[DownloadGroup]:
        try:
            with open(self.state_file, encoding="utf-8") as state:
                payload = json.load(state)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            logger.warning("Download group state could not be read: %s", exc)
            return []

        group_payloads = payload.get("groups") if isinstance(payload, dict) else None
        if not isinstance(group_payloads, list):
            return []
        groups: list[DownloadGroup] = []
        for item in group_payloads:
            if not isinstance(item, dict):
                continue
            group = parse_download_group(item)
            if group is not None:
                groups.append(group)
        return groups

    def save_groups(self, groups: list[DownloadGroup]) -> None:
        parent = os.path.dirname(self.state_file) or "."
        os.makedirs(parent, exist_ok=True)
        payload = {"groups": [group_to_dict(group) for group in groups]}
        temp_file = os.path.join(parent, f".{os.path.basename(self.state_file)}.{uuid.uuid4().hex}.tmp")
        with open(temp_file, "w", encoding="utf-8") as state:
            json.dump(payload, state, indent=2, sort_keys=True)
            state.write("\n")
        os.replace(temp_file, self.state_file)

    def add_group(self, group: DownloadGroup) -> None:
        groups = self.load_groups()
        groups.append(group)
        self.save_groups(groups)

    def update_group(self, group: DownloadGroup) -> None:
        groups = self.load_groups()
        for index, existing in enumerate(groups):
            if existing.id == group.id:
                groups[index] = group
                break
        else:
            groups.append(group)
        self.save_groups(groups)

    def remove_group(self, group_id: str) -> bool:
        groups = self.load_groups()
        retained: list[DownloadGroup] = []
        removed = False
        for group in groups:
            if group.id == group_id and is_group_clearable(group):
                removed = True
                continue
            retained.append(group)
        if removed:
            self.save_groups(retained)
        return removed

    def clear_eligible_groups(self) -> int:
        groups = self.load_groups()
        retained = [group for group in groups if not is_group_clearable(group)]
        removed_count = len(groups) - len(retained)
        if removed_count:
            self.save_groups(retained)
        return removed_count


def parse_download_group(payload: dict[str, Any]) -> DownloadGroup | None:
    group_id = payload.get("id")
    name = payload.get("name")
    parts_payload = payload.get("parts")
    if not isinstance(group_id, str) or not group_id:
        return None
    if not isinstance(name, str) or not name:
        name = f"download-group-{group_id[:8]}"
    if not isinstance(parts_payload, list):
        parts_payload = []

    parts: list[DownloadGroupPart] = []
    for part_payload in parts_payload:
        if not isinstance(part_payload, dict):
            continue
        gid = part_payload.get("aria2_gid")
        if not isinstance(gid, str) or not gid:
            continue
        filename = part_payload.get("filename")
        local_path = part_payload.get("local_download_path")
        status = part_payload.get("status")
        error = part_payload.get("error")
        parts.append(
            DownloadGroupPart(
                aria2_gid=gid,
                filename=filename if isinstance(filename, str) and filename else None,
                local_download_path=local_path if isinstance(local_path, str) and local_path else None,
                status=status if isinstance(status, str) and status else "unknown",
                error=error if isinstance(error, str) and error else None,
                total_length=parse_int(part_payload.get("total_length")),
                completed_length=parse_int(part_payload.get("completed_length")),
            ),
        )

    original_hosts = payload.get("original_hosts")
    if not isinstance(original_hosts, list):
        original_hosts = []
    hosts = [host for host in original_hosts if isinstance(host, str) and host]
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    extraction_status = payload.get("extraction_status")
    extraction_error = payload.get("extraction_error")
    extraction_output_path = payload.get("extraction_output_path")
    if (
        extraction_status == "failed"
        and extraction_error == NO_SUPPORTED_ARCHIVE_MESSAGE
        and select_archive_start_file(
            [
                part.local_download_path or part.filename or ""
                for part in parts
            ],
        )
        is None
    ):
        extraction_status = "skipped"
        extraction_error = None
    return DownloadGroup(
        id=group_id,
        name=safe_group_name(name, fallback=f"download-group-{group_id[:8]}"),
        created_at=created_at if isinstance(created_at, str) else now_iso(),
        updated_at=updated_at if isinstance(updated_at, str) else now_iso(),
        original_hosts=hosts,
        parts=parts,
        extraction_status=extraction_status
        if extraction_status in {"pending", "extracting", "complete", "failed", "skipped"}
        else "pending",
        extraction_error=extraction_error if isinstance(extraction_error, str) and extraction_error else None,
        extraction_output_path=extraction_output_path
        if isinstance(extraction_output_path, str) and extraction_output_path
        else None,
    )


def group_to_dict(group: DownloadGroup) -> dict[str, Any]:
    return asdict(group)


SUBMISSION_PATTERN = re.compile(r"https?://[^\s<>'\"]+|magnet:\?[^\s<>'\"]+", re.IGNORECASE)
URL_LEADING_TRIM = "([{'\""
URL_TRAILING_TRIM = ".,;:!?)]}'\""


def extract_urls(text: str) -> list[str]:
    submissions: list[str] = []
    seen: set[str] = set()
    for match in SUBMISSION_PATTERN.finditer(text):
        submission = match.group(0).strip(URL_LEADING_TRIM).rstrip(URL_TRAILING_TRIM)
        if submission and submission not in seen:
            submissions.append(submission)
            seen.add(submission)
    return submissions


def is_magnet_link(submission: str) -> bool:
    return submission.lower().startswith("magnet:?")


def submitted_hostname(submitted_url: str) -> str:
    parsed = urlparse(submitted_url)
    return parsed.hostname.lower() if parsed.hostname else "unknown-host"


def submitted_source_label(submission: str) -> str:
    if is_magnet_link(submission):
        return "magnet link"
    return submitted_hostname(submission)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def derive_group_name(filenames: list[str | None], fallback: str) -> str:
    for filename in filenames:
        if not filename:
            continue
        base = os.path.basename(filename)
        part_match = re.match(r"(?P<name>.+?)\.part0*1\.rar$", base, flags=re.IGNORECASE)
        if part_match:
            return safe_group_name(part_match.group("name"), fallback)
    for filename in filenames:
        if not filename:
            continue
        base = os.path.basename(filename)
        generic_match = re.match(r"(?P<name>.+?)\.(rar|zip|7z)$", base, flags=re.IGNORECASE)
        if generic_match:
            return safe_group_name(generic_match.group("name"), fallback)
    for filename in filenames:
        if filename:
            return safe_group_name(Path(filename).stem, fallback)
    return fallback


def safe_group_name(value: str, fallback: str = "download-group") -> str:
    basename = os.path.basename(value).strip().strip(".")
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", basename)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120] or fallback


def local_download_path(download_dir: str, filename: str | None) -> str | None:
    if not filename:
        return None
    return os.path.join(download_dir, os.path.basename(filename))


def parse_env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def parse_env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def parse_env_int_range(name: str, default: int, minimum: int, maximum: int) -> int:
    value = parse_env_int(name, default)
    return min(max(value, minimum), maximum)


def parse_streamable(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def guess_media_type(filename: str | None) -> str | None:
    if not filename:
        return None
    media_type, _encoding = mimetypes.guess_type(filename)
    return media_type


PLAY_SESSIONS: dict[str, PlaySession] = {}


def create_play_session(result: PlayResult) -> PlaySession:
    if not result.direct_url:
        raise ValueError("Play result does not include a direct URL.")
    purge_expired_play_sessions()
    session_id = uuid.uuid4().hex
    session = PlaySession(
        id=session_id,
        direct_url=result.direct_url,
        filename=result.filename,
        mime_type=result.mime_type,
        created_at=time.monotonic(),
    )
    PLAY_SESSIONS[session_id] = session
    return session


def get_play_session(session_id: str) -> PlaySession | None:
    purge_expired_play_sessions()
    return PLAY_SESSIONS.get(session_id)


def purge_expired_play_sessions() -> None:
    now = time.monotonic()
    expired = [
        session_id
        for session_id, session in PLAY_SESSIONS.items()
        if now - session.created_at > PLAY_SESSION_TTL_SECONDS
    ]
    for session_id in expired:
        PLAY_SESSIONS.pop(session_id, None)


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

    async def add_magnet(self, magnet_link: str) -> dict[str, Any]:
        response = await self._post_authenticated(
            "torrents/addMagnet",
            data={"magnet": magnet_link},
        )
        if response.status_code in {400, 404, 503}:
            raise DownloadUnavailableError
        self._raise_for_unexpected_status(response)
        if response.status_code != 201:
            raise UpstreamError("Real-Debrid returned an unexpected torrent add response.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("Real-Debrid addMagnet returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise DownloadUnavailableError
        return payload

    async def torrent_info(self, torrent_id: str) -> dict[str, Any]:
        response = await self._get_authenticated(f"torrents/info/{torrent_id}")
        if response.status_code in {400, 404, 503}:
            raise DownloadUnavailableError
        self._raise_for_unexpected_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("Real-Debrid torrent info returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise DownloadUnavailableError
        return payload

    async def select_torrent_files(self, torrent_id: str, files: str = "all") -> None:
        response = await self._post_authenticated(
            f"torrents/selectFiles/{torrent_id}",
            data={"files": files},
        )
        if response.status_code == 202:
            return
        if response.status_code in {400, 404, 503}:
            raise DownloadUnavailableError
        self._raise_for_unexpected_status(response)
        if response.status_code != 204:
            raise UpstreamError("Real-Debrid returned an unexpected torrent file selection response.")

    async def delete_torrent(self, torrent_id: str) -> None:
        response = await self._delete_authenticated(f"torrents/delete/{torrent_id}")
        if response.status_code == 404:
            return
        self._raise_for_unexpected_status(response)
        if response.status_code != 204:
            raise UpstreamError("Real-Debrid returned an unexpected torrent delete response.")

    async def _get_authenticated(self, path: str) -> httpx.Response:
        if not self.settings.realdebrid_api_token:
            raise ConfigurationError("REALDEBRID_API_TOKEN is not configured.")

        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.get(
                f"{self.settings.realdebrid_api_base_url}/{path}",
                headers={
                    "Authorization": f"Bearer {self.settings.realdebrid_api_token}",
                },
            )

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

    async def _delete_authenticated(self, path: str) -> httpx.Response:
        if not self.settings.realdebrid_api_token:
            raise ConfigurationError("REALDEBRID_API_TOKEN is not configured.")

        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.delete(
                f"{self.settings.realdebrid_api_base_url}/{path}",
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

    async def configure_queue_options(self) -> None:
        await self._rpc(
            "aria2.changeGlobalOption",
            [
                {
                    "max-concurrent-downloads": str(
                        self.settings.aria2_max_concurrent_downloads,
                    ),
                },
            ],
        )

    async def add_uri(self, direct_url: str) -> str:
        await self.configure_queue_options()
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

    async def tell_status(self, gid: str) -> dict[str, Any]:
        result = await self._rpc("aria2.tellStatus", [gid])
        if not isinstance(result, dict):
            raise UpstreamError("aria2 returned an unexpected status response.")
        return result

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

    async def move_to(self, gid: str, position: int) -> None:
        if position < 0:
            raise ValueError("Queue position must be zero or greater.")
        await self.change_position(gid, position, "POS_SET")

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
        self.group_store = GroupStateStore(settings.group_state_file)

    async def submit_text(self, submitted_text: str) -> SubmissionResult:
        submissions = extract_urls(submitted_text)
        if not submissions:
            return SubmissionResult(
                ok=False,
                message="Paste at least one valid http://, https://, or magnet:? link.",
                downloads=[],
            )
        return await self.submit_urls(submissions)

    async def submit(self, submitted_url: str) -> DownloadResult:
        submission = await self.submit_urls([submitted_url])
        if submission.downloads:
            return submission.downloads[0]
        return DownloadResult(ok=False, message=submission.message)

    async def prepare_play(self, submitted_text: str) -> PlayResult:
        submissions = extract_urls(submitted_text)
        if not submissions:
            return PlayResult(
                ok=False,
                message="Paste one valid http:// or https:// hoster link to play.",
            )
        if len(submissions) > 1:
            return PlayResult(ok=False, message=PLAY_MULTI_LINK_MESSAGE)
        submitted_url = submissions[0]
        if is_magnet_link(submitted_url):
            return PlayResult(ok=False, message=PLAY_MAGNET_MESSAGE, source_label="magnet link")
        try:
            result = await self._resolve_hoster_link_for_play(submitted_url)
        except DownloadUnavailableError:
            return PlayResult(
                ok=False,
                message=NO_DOWNLOAD_MESSAGE,
                submitted_hostname=submitted_hostname(submitted_url),
                source_label=submitted_source_label(submitted_url),
            )
        if not result.ok:
            return result
        if result.streamable is False:
            return PlayResult(
                ok=False,
                message=PLAY_UNSUPPORTED_MESSAGE,
                filename=result.filename,
                mime_type=result.mime_type,
                realdebrid_id=result.realdebrid_id,
                streamable=result.streamable,
                host_supported=result.host_supported,
                submitted_hostname=result.submitted_hostname,
                source_label=result.source_label,
            )
        return result

    async def submit_urls(self, submitted_urls: list[str]) -> SubmissionResult:
        results: list[DownloadResult] = []
        for submitted_url in submitted_urls:
            try:
                results.extend(await self._submit_source(submitted_url))
            except DownloadUnavailableError:
                results.append(
                    DownloadResult(
                        ok=False,
                        message=NO_DOWNLOAD_MESSAGE,
                        submitted_hostname=(
                            None if is_magnet_link(submitted_url) else submitted_hostname(submitted_url)
                        ),
                        source_label=submitted_source_label(submitted_url),
                    ),
                )
            except (ConfigurationError, UpstreamError):
                raise

        submitted = [result for result in results if result.ok and result.aria2_gid]
        group: DownloadGroup | None = None
        if submitted:
            group = self._create_group(submitted_urls, submitted)
            self.group_store.add_group(group)
            for result in submitted:
                result.group_id = group.id

        if len(results) == 1:
            result = results[0]
            return SubmissionResult(ok=result.ok, message=result.message, downloads=results, group=group)

        failed_count = len([result for result in results if not result.ok])
        if submitted and failed_count:
            message = f"Submitted {len(submitted)} of {len(results)} parts to aria2. {failed_count} part(s) failed."
            return SubmissionResult(ok=True, message=message, downloads=results, group=group)
        if submitted:
            return SubmissionResult(
                ok=True,
                message=f"Submitted {len(submitted)} parts to aria2 as one multipart group.",
                downloads=results,
                group=group,
            )
        return SubmissionResult(
            ok=False,
            message=NO_DOWNLOAD_MESSAGE,
            downloads=results,
        )

    async def _submit_source(self, submitted_url: str) -> list[DownloadResult]:
        if is_magnet_link(submitted_url):
            return await self._submit_magnet(submitted_url)
        return [await self._submit_hoster_link(submitted_url)]

    async def _submit_hoster_link(self, submitted_url: str) -> DownloadResult:
        resolved = await self._resolve_hoster_link_for_download(submitted_url)
        if not resolved.ok or not resolved.direct_url:
            return resolved

        gid = await self.aria2.add_uri(resolved.direct_url)
        resolved.aria2_gid = gid
        resolved.message = "Download submitted to aria2."
        return resolved

    async def _resolve_hoster_link_for_download(self, submitted_url: str) -> DownloadResult:
        play = await self._resolve_hoster_link_for_play(submitted_url)
        return DownloadResult(
            ok=play.ok,
            message=play.message,
            filename=play.filename,
            direct_url=play.direct_url,
            host_supported=play.host_supported,
            local_path=local_download_path(self.settings.aria2_download_dir, play.filename),
            submitted_hostname=play.submitted_hostname,
            source_label=play.source_label,
        )

    async def _resolve_hoster_link_for_play(self, submitted_url: str) -> PlayResult:
        self._log_submission(submitted_url)
        host_supported = await self._host_supported(submitted_url)
        source_label = submitted_source_label(submitted_url)

        try:
            await self.realdebrid.check_link(submitted_url)
        except DownloadUnavailableError:
            return PlayResult(
                ok=False,
                message=NO_DOWNLOAD_MESSAGE,
                host_supported=host_supported,
                submitted_hostname=submitted_hostname(submitted_url),
                source_label=source_label,
            )
        except (ConfigurationError, UpstreamError):
            raise
        except Exception as exc:
            logger.info("Real-Debrid availability check was inconclusive: %s", exc)

        unrestricted = await self.realdebrid.unrestrict_link(submitted_url)
        if not isinstance(unrestricted, dict):
            raise DownloadUnavailableError
        direct_url = unrestricted.get("download")
        if not isinstance(direct_url, str) or not direct_url.startswith(("http://", "https://")):
            return PlayResult(
                ok=False,
                message=NO_DOWNLOAD_MESSAGE,
                host_supported=host_supported,
                submitted_hostname=submitted_hostname(submitted_url),
                source_label=source_label,
            )

        filename = unrestricted.get("filename")
        safe_filename = filename if isinstance(filename, str) else None
        mime_type = unrestricted.get("mimeType")
        realdebrid_id = unrestricted.get("id")
        return PlayResult(
            ok=True,
            message="Ready to play.",
            filename=safe_filename,
            direct_url=direct_url,
            mime_type=mime_type if isinstance(mime_type, str) and mime_type else guess_media_type(safe_filename),
            realdebrid_id=realdebrid_id if isinstance(realdebrid_id, str) and realdebrid_id else None,
            streamable=parse_streamable(unrestricted.get("streamable")),
            host_supported=host_supported,
            submitted_hostname=submitted_hostname(submitted_url),
            source_label=source_label,
        )

    async def _submit_magnet(self, magnet_link: str) -> list[DownloadResult]:
        self._log_submission(magnet_link)
        source_label = submitted_source_label(magnet_link)
        added = await self.realdebrid.add_magnet(magnet_link)
        torrent_id = added.get("id")
        if not isinstance(torrent_id, str) or not torrent_id:
            raise DownloadUnavailableError

        files_info = await self._wait_for_torrent_files(torrent_id)
        if files_info is None:
            return [
                DownloadResult(
                    ok=False,
                    message=NO_TORRENT_READY_MESSAGE,
                    source_label=source_label,
                ),
            ]
        if files_info.get("error") == TORRENT_PROCESSING_FAILED_MESSAGE:
            return [
                DownloadResult(
                    ok=False,
                    message=TORRENT_PROCESSING_FAILED_MESSAGE,
                    source_label=source_label,
                ),
            ]

        await self.realdebrid.select_torrent_files(torrent_id, "all")
        ready_info = await self._wait_for_torrent_links(torrent_id)
        if ready_info is None:
            return [
                DownloadResult(
                    ok=False,
                    message=NO_TORRENT_READY_MESSAGE,
                    source_label=source_label,
                ),
            ]
        if ready_info.get("error") == TORRENT_PROCESSING_FAILED_MESSAGE:
            return [
                DownloadResult(
                    ok=False,
                    message=TORRENT_PROCESSING_FAILED_MESSAGE,
                    source_label=source_label,
                ),
            ]

        links = torrent_links(ready_info)
        if not links:
            return [
                DownloadResult(
                    ok=False,
                    message=NO_DOWNLOAD_MESSAGE,
                    source_label=source_label,
                ),
            ]

        results: list[DownloadResult] = []
        for link in links:
            try:
                result = await self._submit_torrent_link(link, source_label)
            except DownloadUnavailableError:
                result = DownloadResult(
                    ok=False,
                    message=NO_DOWNLOAD_MESSAGE,
                    source_label=source_label,
                )
            results.append(result)
        return results

    async def _submit_torrent_link(self, link: str, source_label: str) -> DownloadResult:
        unrestricted = await self.realdebrid.unrestrict_link(link)
        direct_url = unrestricted.get("download")
        if not isinstance(direct_url, str) or not direct_url.startswith(("http://", "https://")):
            raise DownloadUnavailableError

        gid = await self.aria2.add_uri(direct_url)
        filename = unrestricted.get("filename")
        safe_filename = filename if isinstance(filename, str) else None
        return DownloadResult(
            ok=True,
            message="Torrent file submitted to aria2.",
            aria2_gid=gid,
            filename=safe_filename,
            direct_url=direct_url,
            local_path=local_download_path(self.settings.aria2_download_dir, safe_filename),
            source_label=source_label,
        )

    async def _wait_for_torrent_files(self, torrent_id: str) -> dict[str, Any] | None:
        return await self._wait_for_torrent(
            torrent_id,
            lambda info: bool(torrent_files(info)),
        )

    async def _wait_for_torrent_links(self, torrent_id: str) -> dict[str, Any] | None:
        return await self._wait_for_torrent(
            torrent_id,
            lambda info: torrent_status(info) == "downloaded" and bool(torrent_links(info)),
        )

    async def _wait_for_torrent(
        self,
        torrent_id: str,
        is_ready: Any,
    ) -> dict[str, Any] | None:
        timeout = max(self.settings.torrent_ready_timeout_seconds, 0)
        poll_seconds = max(self.settings.torrent_poll_seconds, 0.1)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            info = await self.realdebrid.torrent_info(torrent_id)
            status = torrent_status(info)
            if status in TORRENT_ERROR_STATUSES:
                return {
                    "status": status,
                    "links": [],
                    "error": TORRENT_PROCESSING_FAILED_MESSAGE,
                }
            if is_ready(info):
                return info
            if asyncio.get_running_loop().time() >= deadline:
                return None
            remaining = max(deadline - asyncio.get_running_loop().time(), 0.1)
            await asyncio.sleep(min(poll_seconds, remaining))

    def _create_group(
        self,
        submitted_urls: list[str],
        submitted: list[DownloadResult],
    ) -> DownloadGroup:
        group_id = uuid.uuid4().hex
        now = now_iso()
        name = derive_group_name(
            [result.filename for result in submitted],
            fallback=f"download-group-{group_id[:8]}",
        )
        hosts: list[str] = []
        seen_hosts: set[str] = set()
        for submitted_url in submitted_urls:
            host = submitted_source_label(submitted_url)
            if host not in seen_hosts:
                hosts.append(host)
                seen_hosts.add(host)

        return DownloadGroup(
            id=group_id,
            name=name,
            created_at=now,
            updated_at=now,
            original_hosts=hosts,
            parts=[
                DownloadGroupPart(
                    aria2_gid=result.aria2_gid or "",
                    filename=result.filename,
                    local_download_path=result.local_path,
                    status="submitted",
                )
                for result in submitted
                if result.aria2_gid
            ],
            extraction_status="pending",
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
        if is_magnet_link(submitted_url):
            logger.info("Received submitted magnet link")
            return
        parsed = urlparse(submitted_url)
        host = parsed.hostname or "unknown-host"
        if self.settings.submitted_url_logging:
            logger.info("Received submitted hoster URL with redacted path for host=%s", host)
            return
        logger.info("Received submitted hoster URL for host=%s", host)


class GroupMonitor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.aria2 = Aria2Client(settings)
        self.group_store = GroupStateStore(settings.group_state_file)

    async def poll_once(self) -> None:
        groups = self.group_store.load_groups()
        if not groups:
            return

        changed = False
        for group in groups:
            group_changed = await self._refresh_group(group)
            if await self._maybe_extract(group):
                group_changed = True
            if group_changed:
                group.updated_at = now_iso()
                changed = True

        if changed:
            self.group_store.save_groups(groups)

    async def _refresh_group(self, group: DownloadGroup) -> bool:
        changed = False
        for part in group.parts:
            try:
                status_payload = await self.aria2.tell_status(part.aria2_gid)
            except UpstreamError as exc:
                logger.info("aria2 status lookup failed for tracked group part: %s", exc)
                continue

            status = str(status_payload.get("status") or part.status)
            total_length = parse_int(status_payload.get("totalLength"))
            completed_length = parse_int(status_payload.get("completedLength"))
            error_message = status_payload.get("errorMessage")
            filename = queue_item_name(status_payload)
            path = safe_download_path(
                queue_item_path(status_payload),
                self.settings.app_download_dir,
            )

            if filename and filename != part.filename:
                part.filename = filename
                changed = True
            if path and path != part.local_download_path:
                part.local_download_path = path
                changed = True
            if status != part.status:
                part.status = status
                changed = True
            if total_length != part.total_length:
                part.total_length = total_length
                changed = True
            if completed_length != part.completed_length:
                part.completed_length = completed_length
                changed = True
            normalized_error = error_message if isinstance(error_message, str) and error_message else None
            if normalized_error != part.error:
                part.error = normalized_error
                changed = True

        derived_name = derive_group_name(
            [part.filename or part.local_download_path for part in group.parts],
            fallback=group.name,
        )
        if derived_name != group.name:
            group.name = derived_name
            changed = True
        return changed

    async def _maybe_extract(self, group: DownloadGroup) -> bool:
        if group.extraction_status not in {"pending", "extracting"}:
            return False
        if not group.parts or not all(part.status == "complete" for part in group.parts):
            return False

        archive_paths = [
            path
            for part in group.parts
            if (path := safe_download_path(part.local_download_path, self.settings.app_download_dir))
        ]
        start_file = select_archive_start_file(archive_paths)
        if start_file is None:
            group.extraction_status = "skipped"
            group.extraction_error = None
            group.extraction_output_path = None
            return True

        output_dir = os.path.join(self.settings.app_download_dir, safe_group_name(group.name))
        group.extraction_status = "extracting"
        group.extraction_error = None
        group.extraction_output_path = output_dir
        self.group_store.update_group(group)

        result = await extract_archive(
            start_file=start_file,
            output_dir=output_dir,
            timeout_seconds=self.settings.extract_timeout_seconds,
        )
        if result.ok:
            for archive_path in archive_paths:
                with suppress(FileNotFoundError):
                    os.remove(archive_path)
            group.extraction_status = "complete"
            group.extraction_error = None
        else:
            group.extraction_status = "failed"
            group.extraction_error = result.message
        return True


@dataclass(frozen=True)
class ExtractionResult:
    ok: bool
    message: str | None = None


def select_archive_start_file(paths: list[str]) -> str | None:
    existing_paths = [path for path in paths if path]
    priority_patterns = [
        re.compile(r"\.part0*1\.rar$", re.IGNORECASE),
        re.compile(r"\.rar$", re.IGNORECASE),
        re.compile(r"\.zip$", re.IGNORECASE),
        re.compile(r"\.7z$", re.IGNORECASE),
    ]
    for pattern in priority_patterns:
        for path in sorted(existing_paths):
            if pattern.search(path):
                return path
    return None


def safe_download_path(path: str | None, download_dir: str) -> str | None:
    if not path:
        return None
    try:
        resolved_path = Path(path).resolve(strict=False)
        resolved_download_dir = Path(download_dir).resolve(strict=False)
        resolved_path.relative_to(resolved_download_dir)
    except (OSError, ValueError):
        logger.warning("Ignoring aria2 path outside configured download directory.")
        return None
    return str(resolved_path)


async def extract_archive(
    start_file: str,
    output_dir: str,
    timeout_seconds: int,
) -> ExtractionResult:
    os.makedirs(output_dir, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        "7z",
        "x",
        start_file,
        f"-o{output_dir}",
        "-y",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=max(timeout_seconds, 1),
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return ExtractionResult(ok=False, message="Archive extraction timed out.")

    if process.returncode == 0:
        return ExtractionResult(ok=True)
    output = (stderr or stdout).decode("utf-8", errors="replace").strip()
    return ExtractionResult(
        ok=False,
        message=output[:500] or "Archive extraction failed.",
    )


async def get_downloader() -> Downloader:
    return Downloader(Settings.from_env())


async def get_aria2_client() -> Aria2Client:
    return Aria2Client(Settings.from_env())


async def get_group_store() -> GroupStateStore:
    return GroupStateStore(Settings.from_env().group_state_file)


@app.get("/healthz", response_model=HealthResponse, tags=["system"])
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/api/version", response_model=ApiVersionResponse, tags=["system"])
async def api_version() -> ApiVersionResponse:
    return ApiVersionResponse(name=APP_NAME, version=__version__)


@app.post("/api/submit", response_model=ApiSubmitResponse, tags=["downloads"])
async def api_submit(
    payload: ApiSubmitRequest,
    downloader: Downloader = Depends(get_downloader),
) -> ApiSubmitResponse:
    result = await submit_download_text(payload.url, downloader)
    return api_submit_response(result)


@app.get("/api/queue", response_model=ApiQueueResponse, tags=["queue"])
async def api_queue(
    aria2: Aria2Client = Depends(get_aria2_client),
    group_store: GroupStateStore = Depends(get_group_store),
) -> ApiQueueResponse:
    try:
        snapshot = await aria2.queue_snapshot()
    except UpstreamError as exc:
        logger.warning("aria2 queue API read failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Download queue is temporarily unavailable.",
        ) from exc
    groups = project_groups_for_display(group_store.load_groups(), snapshot)
    return api_queue_response(snapshot, groups)


@app.post("/api/queue/clear-stopped", response_model=ApiMessageResponse, tags=["queue"])
async def api_queue_clear_stopped(
    aria2: Aria2Client = Depends(get_aria2_client),
) -> ApiMessageResponse:
    return await run_api_queue_action(aria2.purge_download_result(), "Stopped history cleared.")


@app.post("/api/queue/groups/clear", response_model=ApiMessageResponse, tags=["multipart groups"])
async def api_queue_clear_groups(
    group_store: GroupStateStore = Depends(get_group_store),
) -> ApiMessageResponse:
    removed_count = group_store.clear_eligible_groups()
    if removed_count == 1:
        return ApiMessageResponse(ok=True, message="Cleared 1 multipart group history entry.")
    if removed_count > 1:
        return ApiMessageResponse(ok=True, message=f"Cleared {removed_count} multipart group history entries.")
    return ApiMessageResponse(ok=False, message="No multipart group history to clear.")


@app.post("/api/queue/groups/{group_id}/clear", response_model=ApiMessageResponse, tags=["multipart groups"])
async def api_queue_clear_group(
    group_id: str,
    group_store: GroupStateStore = Depends(get_group_store),
) -> ApiMessageResponse:
    if group_store.remove_group(group_id):
        return ApiMessageResponse(ok=True, message="Multipart group history entry cleared.")
    return ApiMessageResponse(ok=False, message="Multipart group was not found.")


@app.post("/api/queue/{gid}/pause", response_model=ApiMessageResponse, tags=["queue"])
async def api_queue_pause(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> ApiMessageResponse:
    return await run_api_queue_action(aria2.pause(gid), "Download paused.")


@app.post("/api/queue/{gid}/resume", response_model=ApiMessageResponse, tags=["queue"])
async def api_queue_resume(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> ApiMessageResponse:
    return await run_api_queue_action(aria2.unpause(gid), "Download resumed.")


@app.post("/api/queue/{gid}/remove", response_model=ApiMessageResponse, tags=["queue"])
async def api_queue_remove(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> ApiMessageResponse:
    return await run_api_queue_action(aria2.remove(gid), "Download removed from queue.")


@app.post("/api/queue/{gid}/clear", response_model=ApiMessageResponse, tags=["queue"])
async def api_queue_clear(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> ApiMessageResponse:
    return await run_api_queue_action(aria2.remove_download_result(gid), "History entry cleared.")


@app.post("/api/queue/{gid}/move", response_model=ApiMessageResponse, tags=["queue"])
async def api_queue_move(
    gid: str,
    payload: ApiQueueMoveRequest,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> ApiMessageResponse:
    return await run_api_queue_action(aria2.move_to(gid, payload.position), "Download moved.")


async def run_api_queue_action(action: Any, success_message: str) -> ApiMessageResponse:
    try:
        await action
    except (UpstreamError, ValueError) as exc:
        logger.warning("aria2 queue API action failed: %s", exc)
        return ApiMessageResponse(ok=False, message="Queue action failed. Please try again.")
    return ApiMessageResponse(ok=True, message=success_message)


async def submit_download_text(submitted_text: str, downloader: Any) -> SubmissionResult:
    try:
        if hasattr(downloader, "submit_text"):
            return await downloader.submit_text(submitted_text)
        result = await downloader.submit(submitted_text)
        return SubmissionResult(ok=result.ok, message=result.message, downloads=[result])
    except ConfigurationError as exc:
        logger.warning("Downloader configuration error: %s", exc)
        return SubmissionResult(ok=False, message=str(exc), downloads=[])
    except UpstreamError as exc:
        logger.warning("Downloader upstream error: %s", exc)
        return SubmissionResult(
            ok=False,
            message="Download service is temporarily unavailable. Please try again later.",
            downloads=[],
        )
    except DownloadUnavailableError:
        return SubmissionResult(ok=False, message=NO_DOWNLOAD_MESSAGE, downloads=[])


async def play_download_text(submitted_text: str, downloader: Any) -> PlayResult:
    try:
        if hasattr(downloader, "prepare_play"):
            return await downloader.prepare_play(submitted_text)
        return PlayResult(ok=False, message="Playback is unavailable for this downloader.")
    except ConfigurationError as exc:
        logger.warning("Downloader configuration error: %s", exc)
        return PlayResult(ok=False, message=str(exc))
    except UpstreamError as exc:
        logger.warning("Downloader upstream error: %s", exc)
        return PlayResult(
            ok=False,
            message="Download service is temporarily unavailable. Please try again later.",
        )
    except DownloadUnavailableError:
        return PlayResult(ok=False, message=NO_DOWNLOAD_MESSAGE)


def api_submit_response(result: SubmissionResult) -> ApiSubmitResponse:
    return ApiSubmitResponse(
        ok=result.ok,
        message=result.message,
        downloads=[api_download_result(download) for download in result.downloads],
        group=api_group_result(result.group) if result.group else None,
    )


def api_download_result(download: DownloadResult) -> ApiDownloadResult:
    return ApiDownloadResult(
        ok=download.ok,
        message=download.message,
        aria2_gid=download.aria2_gid,
        filename=download.filename,
        host_supported=download.host_supported,
        group_id=download.group_id,
        submitted_hostname=download.submitted_hostname,
    )


def api_queue_response(snapshot: QueueSnapshot, groups: list[DownloadGroup]) -> ApiQueueResponse:
    return ApiQueueResponse(
        active=[api_queue_item(item) for item in snapshot.active],
        waiting=[api_queue_item(item) for item in snapshot.waiting],
        stopped=[api_queue_item(item) for item in snapshot.stopped],
        groups=[api_group_result(group) for group in groups],
    )


def api_queue_item(item: QueueItem) -> ApiQueueItem:
    return ApiQueueItem(
        gid=item.gid,
        status=item.status,
        name=item.name,
        total_length=item.total_length,
        completed_length=item.completed_length,
        download_speed=item.download_speed,
        eta_seconds=item.eta_seconds,
        progress_percent=item.progress_percent,
        can_pause=item.can_pause,
        can_resume=item.can_resume,
        can_remove=item.can_remove,
        can_reorder=item.can_reorder,
        can_clear=item.can_clear,
    )


def api_group_result(group: DownloadGroup) -> ApiGroupResult:
    return ApiGroupResult(
        id=group.id,
        name=group.name,
        original_hosts=group.original_hosts,
        extraction_status=group.extraction_status,
        progress_percent=group.progress_percent,
        complete_parts=group.complete_parts,
        total_parts=len(group.parts),
        parts=[api_group_part(part) for part in group.parts],
    )


def api_group_part(part: DownloadGroupPart) -> ApiGroupPart:
    total_length = part.total_length
    completed_length = part.completed_length
    progress = min(100.0, (completed_length / total_length) * 100) if total_length > 0 else 0.0
    if total_length <= 0 and part.status == "complete":
        progress = 100.0
    return ApiGroupPart(
        aria2_gid=part.aria2_gid,
        filename=part.filename,
        status=part.status,
        total_length=total_length,
        completed_length=completed_length,
        progress_percent=progress,
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> str:
    return render_page()


@app.post("/submit", response_class=HTMLResponse, include_in_schema=False)
async def submit(
    url: str = Form(...),
    downloader: Downloader = Depends(get_downloader),
) -> Any:
    if submission_contains_magnet(url):
        asyncio.create_task(submit_download_text_in_background(url, downloader))
        return queue_redirect(
            "Magnet submission started. The queue will update when Real-Debrid links are ready.",
            "success",
        )
    result = await submit_download_text(url, downloader)
    return render_page(result)


@app.post("/play", response_class=HTMLResponse, include_in_schema=False)
async def play(
    url: str = Form(...),
    downloader: Downloader = Depends(get_downloader),
) -> str:
    result = await play_download_text(url, downloader)
    if result.ok:
        session = create_play_session(result)
        result.direct_url = None
        result.stream_url = f"/play/{session.id}/stream"
    return render_page(play_result=result)


@app.get("/play/{session_id}/stream", include_in_schema=False)
async def play_stream(
    session_id: str,
    request: Request,
) -> Any:
    return await play_stream_response(session_id, request, "GET")


@app.head("/play/{session_id}/stream", include_in_schema=False)
async def play_stream_head(
    session_id: str,
    request: Request,
) -> Any:
    return await play_stream_response(session_id, request, "HEAD")


async def play_stream_response(session_id: str, request: Request, method: str) -> Any:
    session = get_play_session(session_id)
    if session is None:
        return PlainTextResponse(PLAY_SESSION_EXPIRED_MESSAGE, status_code=404)
    try:
        return await proxy_play_session(session, request.headers.get("range"), method)
    except httpx.HTTPError as exc:
        logger.warning("Real-Debrid playback proxy failed: %s", exc)
        return PlainTextResponse("Playback stream is temporarily unavailable.", status_code=502)


def submission_contains_magnet(submitted_text: str) -> bool:
    return any(is_magnet_link(submission) for submission in extract_urls(submitted_text))


async def submit_download_text_in_background(submitted_text: str, downloader: Any) -> None:
    try:
        result = await submit_download_text(submitted_text, downloader)
    except Exception as exc:
        logger.warning("Background magnet submission failed: %s", exc)
        return
    if not result.ok:
        logger.info("Background magnet submission finished without queued downloads: %s", result.message)


async def group_monitor_loop(settings: Settings) -> None:
    monitor = GroupMonitor(settings)
    while True:
        try:
            await monitor.poll_once()
        except Exception as exc:
            logger.warning("Download group monitor failed: %s", exc)
        await asyncio.sleep(settings.group_poll_seconds)


@app.get("/queue", response_class=HTMLResponse, include_in_schema=False)
async def queue(
    message: Optional[str] = Query(None),
    level: str = Query("success"),
    aria2: Aria2Client = Depends(get_aria2_client),
    group_store: GroupStateStore = Depends(get_group_store),
) -> str:
    try:
        snapshot = await aria2.queue_snapshot()
        groups = project_groups_for_display(group_store.load_groups(), snapshot)
        return render_queue_page(snapshot, groups=groups, message=message, level=level)
    except UpstreamError as exc:
        logger.warning("aria2 queue read failed: %s", exc)
        return render_queue_page(
            None,
            groups=group_store.load_groups(),
            message="Download queue is temporarily unavailable.",
            level="error",
        )


@app.get("/queue/events", include_in_schema=False)
async def queue_events(
    request: Request,
    once: bool = Query(False, include_in_schema=False),
    aria2: Aria2Client = Depends(get_aria2_client),
    group_store: GroupStateStore = Depends(get_group_store),
) -> StreamingResponse:
    interval = Settings.from_env().queue_stream_interval_seconds
    return StreamingResponse(
        queue_event_stream(
            request=request,
            aria2=aria2,
            group_store=group_store,
            interval_seconds=interval,
            once=once,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/queue/groups/clear", include_in_schema=False)
async def queue_clear_groups(
    group_store: GroupStateStore = Depends(get_group_store),
) -> RedirectResponse:
    removed_count = group_store.clear_eligible_groups()
    if removed_count == 1:
        return queue_redirect("Cleared 1 multipart group history entry.", "success")
    if removed_count > 1:
        return queue_redirect(f"Cleared {removed_count} multipart group history entries.", "success")
    return queue_redirect("No multipart group history to clear.", "error")


@app.post("/queue/groups/{group_id}/clear", include_in_schema=False)
async def queue_clear_group(
    group_id: str,
    group_store: GroupStateStore = Depends(get_group_store),
) -> RedirectResponse:
    if group_store.remove_group(group_id):
        return queue_redirect("Multipart group history entry cleared.", "success")
    return queue_redirect("Multipart group was not found.", "error")


@app.post("/queue/{gid}/pause", include_in_schema=False)
async def queue_pause(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> RedirectResponse:
    return await run_queue_action(aria2.pause(gid), "Download paused.")


@app.post("/queue/{gid}/resume", include_in_schema=False)
async def queue_resume(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> RedirectResponse:
    return await run_queue_action(aria2.unpause(gid), "Download resumed.")


@app.post("/queue/{gid}/remove", include_in_schema=False)
async def queue_remove(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> RedirectResponse:
    return await run_queue_action(aria2.remove(gid), "Download removed from queue.")


@app.post("/queue/{gid}/clear", include_in_schema=False)
async def queue_clear(
    gid: str,
    aria2: Aria2Client = Depends(get_aria2_client),
) -> RedirectResponse:
    return await run_queue_action(aria2.remove_download_result(gid), "History entry cleared.")


@app.post("/queue/clear-stopped", include_in_schema=False)
async def queue_clear_stopped(
    aria2: Aria2Client = Depends(get_aria2_client),
) -> RedirectResponse:
    return await run_queue_action(aria2.purge_download_result(), "Stopped history cleared.")


@app.post("/queue/{gid}/move", include_in_schema=False)
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


async def proxy_play_session(
    session: PlaySession,
    range_header: str | None,
    method: str = "GET",
) -> Response:
    request_headers = {}
    if range_header:
        request_headers["Range"] = range_header
    client = httpx.AsyncClient(timeout=None, follow_redirects=True)
    try:
        request = client.build_request(method, session.direct_url, headers=request_headers)
        upstream = await client.send(request, stream=True)
        upstream.raise_for_status()
    except Exception:
        await client.aclose()
        raise

    response_headers = play_proxy_headers(upstream.headers, session)
    if method == "HEAD":
        await close_play_proxy(upstream, client)
        return Response(
            status_code=upstream.status_code,
            media_type=response_headers.pop("content-type", None),
            headers=response_headers,
        )
    media_type = response_headers.pop("content-type", None) or "application/octet-stream"
    return StreamingResponse(
        upstream.aiter_bytes(),
        status_code=upstream.status_code,
        media_type=media_type,
        headers=response_headers,
        background=BackgroundTask(close_play_proxy, upstream, client),
    )


def play_proxy_headers(upstream_headers: httpx.Headers, session: PlaySession) -> dict[str, str]:
    response_headers: dict[str, str] = {}
    for header in ["cache-control", "content-length", "content-range", "etag", "last-modified"]:
        value = upstream_headers.get(header)
        if value:
            response_headers[header] = value
    accept_ranges = upstream_headers.get("accept-ranges")
    if accept_ranges:
        response_headers["accept-ranges"] = accept_ranges
    elif response_headers.get("content-length") or response_headers.get("content-range"):
        response_headers["accept-ranges"] = "bytes"
    filename = session.filename or "video"
    response_headers["Content-Disposition"] = f'inline; filename="{safe_header_filename(filename)}"'
    response_headers["content-type"] = (
        upstream_headers.get("content-type")
        or session.mime_type
        or "application/octet-stream"
    )
    return response_headers


async def close_play_proxy(upstream: httpx.Response, client: httpx.AsyncClient) -> None:
    await upstream.aclose()
    await client.aclose()


def safe_header_filename(filename: str) -> str:
    return os.path.basename(filename).replace("\\", "_").replace('"', "_") or "video"


async def queue_event_stream(
    request: Request,
    aria2: Aria2Client,
    group_store: GroupStateStore,
    interval_seconds: float,
    once: bool = False,
) -> Any:
    interval = max(interval_seconds, 0.1)
    while True:
        if await request.is_disconnected():
            break
        try:
            snapshot = await aria2.queue_snapshot()
            groups = project_groups_for_display(group_store.load_groups(), snapshot)
            yield format_sse("queue", {"html": render_queue_sections(snapshot, groups)})
        except Exception as exc:
            logger.warning("aria2 queue event read failed: %s", exc)
            yield format_sse(
                "error",
                {"message": "Download queue is temporarily unavailable."},
            )
        if once:
            break
        await asyncio.sleep(interval)


def format_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


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


def queue_item_path(payload: dict[str, Any]) -> str | None:
    files = payload.get("files")
    if not isinstance(files, list):
        return None
    for file_payload in files:
        if not isinstance(file_payload, dict):
            continue
        path = file_payload.get("path")
        if isinstance(path, str) and path:
            return path
    return None


def torrent_status(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    return status if isinstance(status, str) else "unknown"


def torrent_files(payload: dict[str, Any]) -> list[dict[str, Any]]:
    files = payload.get("files")
    if not isinstance(files, list):
        return []
    return [file_payload for file_payload in files if isinstance(file_payload, dict)]


def torrent_links(payload: dict[str, Any]) -> list[str]:
    links = payload.get("links")
    if not isinstance(links, list):
        return []
    return [
        link
        for link in links
        if isinstance(link, str) and link.startswith(("http://", "https://"))
    ]


def parse_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")


def main() -> None:
    logging.basicConfig(level=os.getenv("APP_LOG_LEVEL", "info").upper())
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8080"))
    log_level = os.getenv("APP_LOG_LEVEL", "info")
    uvicorn.run("app.main:app", host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
