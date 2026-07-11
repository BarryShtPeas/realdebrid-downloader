from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from pydantic import BaseModel, Field
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app import __version__


NO_DOWNLOAD_MESSAGE = "No download available from Real-Debrid for this link."
GROUP_TERMINAL_PART_STATUSES = {"complete", "error", "removed"}
GROUP_TERMINAL_EXTRACTION_STATUSES = {"complete", "failed", "skipped"}
NO_SUPPORTED_ARCHIVE_MESSAGE = "Could not find a supported archive start file."

logger = logging.getLogger("rd_downloader")


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> Any:
    settings = Settings.from_env()
    task: asyncio.Task[Any] | None = None
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
    url: str = Field(..., min_length=1, description="One URL or free text containing one or more URLs.")


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
    aria2_split: str
    submitted_url_logging: bool
    app_download_dir: str
    group_state_file: str
    extract_timeout_seconds: int
    group_poll_seconds: int
    queue_stream_interval_seconds: float

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


URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
URL_LEADING_TRIM = "([{'\""
URL_TRAILING_TRIM = ".,;:!?)]}'\""


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_PATTERN.finditer(text):
        url = match.group(0).strip(URL_LEADING_TRIM).rstrip(URL_TRAILING_TRIM)
        if url and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def submitted_hostname(submitted_url: str) -> str:
    parsed = urlparse(submitted_url)
    return parsed.hostname.lower() if parsed.hostname else "unknown-host"


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
        urls = extract_urls(submitted_text)
        if not urls:
            return SubmissionResult(
                ok=False,
                message="Paste at least one valid http:// or https:// URL.",
                downloads=[],
            )
        return await self.submit_urls(urls)

    async def submit(self, submitted_url: str) -> DownloadResult:
        submission = await self.submit_urls([submitted_url])
        if submission.downloads:
            return submission.downloads[0]
        return DownloadResult(ok=False, message=submission.message)

    async def submit_urls(self, submitted_urls: list[str]) -> SubmissionResult:
        results: list[DownloadResult] = []
        for submitted_url in submitted_urls:
            try:
                results.append(await self._submit_one(submitted_url))
            except DownloadUnavailableError:
                results.append(
                    DownloadResult(
                        ok=False,
                        message=NO_DOWNLOAD_MESSAGE,
                        submitted_hostname=submitted_hostname(submitted_url),
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

    async def _submit_one(self, submitted_url: str) -> DownloadResult:
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
        safe_filename = filename if isinstance(filename, str) else None
        return DownloadResult(
            ok=True,
            message="Download submitted to aria2.",
            aria2_gid=gid,
            filename=safe_filename,
            direct_url=direct_url,
            host_supported=host_supported,
            local_path=local_download_path(self.settings.aria2_download_dir, safe_filename),
            submitted_hostname=submitted_hostname(submitted_url),
        )

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
            host = submitted_hostname(submitted_url)
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
            path = queue_item_path(status_payload)

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

        archive_paths = [part.local_download_path for part in group.parts if part.local_download_path]
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


def get_downloader() -> Downloader:
    return Downloader(Settings.from_env())


def get_aria2_client() -> Aria2Client:
    return Aria2Client(Settings.from_env())


def get_group_store() -> GroupStateStore:
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
    return ApiMessageResponse(ok=False, message="No completed multipart group history to clear.")


@app.post("/api/queue/groups/{group_id}/clear", response_model=ApiMessageResponse, tags=["multipart groups"])
async def api_queue_clear_group(
    group_id: str,
    group_store: GroupStateStore = Depends(get_group_store),
) -> ApiMessageResponse:
    if group_store.remove_group(group_id):
        return ApiMessageResponse(ok=True, message="Multipart group history entry cleared.")
    return ApiMessageResponse(ok=False, message="Multipart group is still active or was not found.")


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
) -> str:
    result = await submit_download_text(url, downloader)
    return render_page(result)


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
    return queue_redirect("No completed multipart group history to clear.", "error")


@app.post("/queue/groups/{group_id}/clear", include_in_schema=False)
async def queue_clear_group(
    group_id: str,
    group_store: GroupStateStore = Depends(get_group_store),
) -> RedirectResponse:
    if group_store.remove_group(group_id):
        return queue_redirect("Multipart group history entry cleared.", "success")
    return queue_redirect("Multipart group is still active or was not found.", "error")


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


def render_page(result: DownloadResult | SubmissionResult | None = None) -> str:
    result_html = ""
    if result is not None:
        status = "success" if result.ok else "error"
        details: list[str] = []
        downloads = result.downloads if isinstance(result, SubmissionResult) else [result]
        successful_downloads = [download for download in downloads if download.ok]
        if isinstance(result, SubmissionResult) and result.group:
            details.append(
                f"Group: {html.escape(result.group.name)} ({len(result.group.parts)} part(s))",
            )
        for download in successful_downloads:
            if download.filename:
                details.append(f"File: {html.escape(download.filename)}")
            if download.aria2_gid:
                details.append(f"aria2 id: {html.escape(download.aria2_gid)}")
            if download.direct_url:
                escaped_url = html.escape(download.direct_url, quote=True)
                details.append(
                    f'Real-Debrid URL: <a href="{escaped_url}">{html.escape(download.direct_url)}</a>',
                )
            if download.host_supported is False:
                details.append("Real-Debrid did not list this host, but unrestrict was attempted.")
        failed_downloads = [download for download in downloads if not download.ok]
        for failed in failed_downloads:
            host = failed.submitted_hostname or "unknown host"
            details.append(f"{html.escape(host)}: {html.escape(failed.message)}")
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
          textarea, button {{
            border-radius: 8px;
            font: inherit;
            width: 100%;
          }}
          textarea {{
            background: var(--control);
            border: 1px solid var(--line);
            color: var(--fg);
            min-height: 11rem;
            padding: .75rem .85rem;
            resize: vertical;
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
          .app-version {{
            color: var(--muted);
            font-size: .9rem;
            margin-top: 1.25rem;
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
              Hoster URLs
              <textarea name="url" required autocomplete="off" placeholder="Paste one URL or a block of text containing multiple URLs"></textarea>
            </label>
            <button type="submit">Submit to aria2</button>
          </form>
          <p class="app-version">v{html.escape(__version__)}</p>
        </main>
      </body>
    </html>
    """


def render_queue_page(
    snapshot: QueueSnapshot | None,
    groups: list[DownloadGroup] | None = None,
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

    sections = render_queue_sections(snapshot, groups or [])

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
          .parts {{
            border-top: 1px solid var(--line);
            display: grid;
            gap: .35rem;
            margin-top: .8rem;
            padding-top: .8rem;
          }}
          .part {{
            color: var(--muted);
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
          .app-version {{
            color: var(--muted);
            font-size: .9rem;
            margin-top: 1.25rem;
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
          <p>Manage active, waiting, and recently stopped aria2 downloads and multipart extraction groups.</p>
          <p class="live-status" id="queue-live-status" aria-live="polite">Live updates connecting...</p>
          {message_html}
          <div id="queue-sections">
            {sections}
          </div>
          <p class="app-version">v{html.escape(__version__)}</p>
        </main>
        <script>
          (() => {{
            const container = document.getElementById("queue-sections");
            const status = document.getElementById("queue-live-status");
            if (!container || !status) {{
              return;
            }}
            if (!("EventSource" in window)) {{
              status.textContent = "Live updates unavailable";
              return;
            }}

            const source = new EventSource("/queue/events");
            source.addEventListener("open", () => {{
              status.textContent = "Live updates active";
            }});
            source.addEventListener("queue", (event) => {{
              try {{
                const payload = JSON.parse(event.data);
                if (typeof payload.html === "string") {{
                  container.innerHTML = payload.html;
                  status.textContent = "Live updates active";
                }}
              }} catch (_error) {{
                status.textContent = "Live updates unavailable";
              }}
            }});
            source.addEventListener("error", (event) => {{
              let message = "Live updates reconnecting...";
              if ("data" in event && event.data) {{
                try {{
                  const payload = JSON.parse(event.data);
                  if (typeof payload.message === "string") {{
                    message = payload.message;
                  }}
                }} catch (_error) {{
                  message = "Live updates unavailable";
                }}
              }}
              status.textContent = source.readyState === EventSource.CLOSED
                ? "Live updates unavailable"
                : message;
            }});
          }})();
        </script>
      </body>
    </html>
    """


def render_queue_sections(
    snapshot: QueueSnapshot | None,
    groups: list[DownloadGroup],
) -> str:
    sections: list[str] = [render_group_section(groups)]
    if snapshot is None:
        return "\n".join(sections)
    sections.extend(
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
    return "\n".join(sections)


def render_group_section(groups: list[DownloadGroup]) -> str:
    clear_all_html = ""
    if any(is_group_clearable(group) for group in groups):
        clear_all_html = (
            '<form method="post" action="/queue/groups/clear">'
            '<button class="danger" type="submit">Clear multipart history</button>'
            "</form>"
        )
    heading = (
        '<div class="section-heading">'
        "<h2>Multipart Groups</h2>"
        f"{clear_all_html}"
        "</div>"
    )
    if not groups:
        return f"{heading}<p>No multipart groups yet.</p>"
    newest_first = sorted(groups, key=lambda group: group.created_at, reverse=True)
    return heading + "".join(render_group_item(group) for group in newest_first)


def render_group_item(group: DownloadGroup) -> str:
    progress = f"{group.progress_percent:.1f}"
    statuses = {part.status for part in group.parts}
    group_status = "complete" if statuses == {"complete"} else ", ".join(sorted(statuses)) or "unknown"
    meta = [
        f"Status: {html.escape(group_status)}",
        f"Parts: {group.complete_parts} / {len(group.parts)} complete",
        f"Extraction: {html.escape(group.extraction_status)}",
    ]
    if group.extraction_output_path and group.extraction_status == "complete":
        meta.append(f"Extracted to: {html.escape(group.extraction_output_path)}")
    if group.extraction_error:
        meta.append(f"Extraction error: {html.escape(group.extraction_error)}")
    if group.original_hosts:
        meta.append("Source hosts: " + html.escape(", ".join(group.original_hosts)))

    part_rows = []
    for index, part in enumerate(group.parts, start=1):
        name = part.filename or part.local_download_path or part.aria2_gid
        size = ""
        if part.total_length:
            size = f" ({format_bytes(part.completed_length)} / {format_bytes(part.total_length)})"
        error = f" - {html.escape(part.error)}" if part.error else ""
        part_rows.append(
            f'<span class="part">{index}. {html.escape(name)} - {html.escape(part.status)}{size}{error}</span>',
        )

    meta_html = "".join(f"<span>{entry}</span>" for entry in meta)
    parts_html = "".join(part_rows)
    actions_html = ""
    if is_group_clearable(group):
        group_id = html.escape(group.id, quote=True)
        actions_html = (
            '<div class="actions">'
            f'{queue_button(f"/queue/groups/{group_id}/clear", "Clear", danger=True)}'
            "</div>"
        )
    return (
        '<article class="item">'
        f"<h3>{html.escape(group.name)}</h3>"
        f'<div class="meta">{meta_html}</div>'
        '<div class="progress" aria-hidden="true">'
        f'<span style="width: {progress}%"></span>'
        "</div>"
        f'<div class="parts">{parts_html}</div>'
        f"{actions_html}"
        "</article>"
    )


def project_groups_for_display(
    groups: list[DownloadGroup],
    snapshot: QueueSnapshot,
) -> list[DownloadGroup]:
    items_by_gid = {
        item.gid: item
        for item in [*snapshot.active, *snapshot.waiting, *snapshot.stopped]
        if item.gid
    }
    projected_groups: list[DownloadGroup] = []
    for group in groups:
        projected_parts: list[DownloadGroupPart] = []
        changed = False
        for part in group.parts:
            item = items_by_gid.get(part.aria2_gid)
            if item is None:
                projected_parts.append(replace(part))
                continue
            projected_parts.append(
                replace(
                    part,
                    filename=item.name or part.filename,
                    status=item.status,
                    error=item.error_message,
                    total_length=item.total_length,
                    completed_length=item.completed_length,
                ),
            )
            changed = True
        projected_groups.append(replace(group, parts=projected_parts) if changed else replace(group))
    return projected_groups


def is_group_clearable(group: DownloadGroup) -> bool:
    if group.extraction_status in GROUP_TERMINAL_EXTRACTION_STATUSES:
        return True
    return bool(group.parts) and all(part.status in GROUP_TERMINAL_PART_STATUSES for part in group.parts)


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
