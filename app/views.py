from __future__ import annotations

import html
from dataclasses import replace
from typing import Any

from app import __version__


GROUP_TERMINAL_PART_STATUSES = {"complete", "error", "removed"}
GROUP_TERMINAL_EXTRACTION_STATUSES = {"complete", "failed", "skipped"}


def render_page(result: Any | None = None) -> str:
    result_html = ""
    if result is not None:
        status = "success" if result.ok else "error"
        details: list[str] = []
        downloads = result.downloads if hasattr(result, "downloads") else [result]
        successful_downloads = [download for download in downloads if download.ok]
        if getattr(result, "group", None):
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
            source = failed.source_label or failed.submitted_hostname or "unknown source"
            details.append(f"{html.escape(source)}: {html.escape(failed.message)}")
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
        <link rel="stylesheet" href="/static/rdd.css">
      </head>
      <body>
        <main class="submit-page">
          <nav class="nav" aria-label="Main navigation">
            <a href="/">Submit</a>
            <a href="/queue">Queue</a>
          </nav>
          <h1>Real-Debrid Downloader</h1>
          <p>Submit a hoster link or magnet link and send the Real-Debrid download to the internal aria2 worker.</p>
          {result_html}
          <form class="submit-form" method="post" action="/submit">
            <label>
              Hoster URLs or magnet links
              <textarea name="url" required autocomplete="off" placeholder="Paste one URL, magnet link, or a block of text containing multiple links"></textarea>
            </label>
            <button type="submit">Submit to aria2</button>
          </form>
          <p class="app-version">v{html.escape(__version__)}</p>
        </main>
      </body>
    </html>
    """


def render_queue_page(
    snapshot: Any | None,
    groups: list[Any] | None = None,
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
        <link rel="stylesheet" href="/static/rdd.css">
      </head>
      <body>
        <main class="queue-page">
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
        <script src="/static/queue.js"></script>
      </body>
    </html>
    """


def render_queue_sections(
    snapshot: Any | None,
    groups: list[Any],
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


def render_group_section(groups: list[Any]) -> str:
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


def render_group_item(group: Any) -> str:
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
    groups: list[Any],
    snapshot: Any,
) -> list[Any]:
    items_by_gid = {
        item.gid: item
        for item in [*snapshot.active, *snapshot.waiting, *snapshot.stopped]
        if item.gid
    }
    projected_groups: list[Any] = []
    for group in groups:
        projected_parts: list[Any] = []
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


def is_group_clearable(group: Any) -> bool:
    if group.extraction_status in GROUP_TERMINAL_EXTRACTION_STATUSES:
        return True
    return bool(group.parts) and all(part.status in GROUP_TERMINAL_PART_STATUSES for part in group.parts)


def render_queue_section(
    title: str,
    items: list[Any],
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


def render_queue_item(item: Any) -> str:
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
