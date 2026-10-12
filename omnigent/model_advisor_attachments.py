"""Bounded inline attachments for pre-session Advisor rounds.

The answer uses the ordinary session upload queue. The selector sees
images, bounded text excerpts, and explicit metadata for binary documents;
it never needs filesystem tools to choose an execution model.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import PurePath
from typing import Any

from omnigent.runtime.content_resolver import (
    _resolve_content_type,
    attachment_text_type_for_extension,
    attachment_upload_limit,
    compress_image_attachment,
    image_filename_for_content_type,
)

MAX_ADVISOR_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_ADVISOR_ATTACHMENTS = 20
MAX_ADVISOR_TEXT_CHARACTERS = 100_000


def normalize_attachments(attachments: list[dict[str, Any]]) -> tuple[dict[str, str], ...]:
    """Validate browser bytes, applying the normal upload MIME and size rules."""
    from omnigent.inner.native_attachments import requires_filesystem
    from omnigent.server.server_config import (
        filesystem_attachment_denied_extensions,
        filesystem_attachment_file_limit,
        filesystem_attachment_total_bytes_limit,
        filesystem_attachment_upload_limit,
    )

    if len(attachments) > MAX_ADVISOR_ATTACHMENTS:
        raise ValueError("Too many Advisor attachments")
    normalized = []
    total = filesystem_total = filesystem_count = 0
    for attachment in attachments:
        if set(attachment) != {"filename", "content_type", "data"} or any(
            not isinstance(value, str) for value in attachment.values()
        ):
            raise ValueError("Invalid Advisor attachment")
        filename = attachment["filename"]
        if not filename or len(filename) > 1024:
            raise ValueError("Invalid attachment filename")
        content_type = _resolve_content_type(attachment["content_type"], filename)
        filesystem = requires_filesystem(filename)
        if filesystem:
            if PurePath(filename).suffix.lower() in filesystem_attachment_denied_extensions():
                raise ValueError("This attachment type is not accepted by this deployment")
            limit = filesystem_attachment_upload_limit()
            content_type = _resolve_content_type("application/octet-stream", filename)
        else:
            limit = attachment_upload_limit(content_type)
            if limit is None:
                content_type = attachment_text_type_for_extension(filename) or content_type
                limit = attachment_upload_limit(content_type)
            if limit is None:
                raise ValueError(f"Unsupported attachment type: {content_type}")
        if len(attachment["data"]) > ((min(limit, MAX_ADVISOR_ATTACHMENT_BYTES) + 2) // 3) * 4:
            raise ValueError("Attachment exceeds the upload size limit")
        try:
            raw = base64.b64decode(attachment["data"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid attachment encoding") from exc
        total += len(raw)
        if len(raw) > limit or total > MAX_ADVISOR_ATTACHMENT_BYTES:
            raise ValueError("Advisor attachments exceed the upload size limit")
        if filesystem:
            filesystem_count += 1
            filesystem_total += len(raw)
            if (
                filesystem_count > filesystem_attachment_file_limit()
                or filesystem_total > filesystem_attachment_total_bytes_limit()
            ):
                raise ValueError("Filesystem attachments exceed the upload quota")
        if content_type in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raw, resolved_type, _ = compress_image_attachment(raw, content_type)
            if resolved_type != content_type:
                filename = image_filename_for_content_type(filename, resolved_type)
            content_type = resolved_type
        normalized.append(
            {
                "filename": filename,
                "content_type": content_type,
                "data": base64.b64encode(raw).decode("ascii"),
            }
        )
    return tuple(normalized)


def advisor_attachment_context(attachments: tuple[dict[str, str], ...]) -> list[dict[str, object]]:
    """Expose data, not instructions, with an honest text-only binary description."""
    from omnigent.inner.native_attachments import requires_filesystem

    context = []
    remaining = MAX_ADVISOR_TEXT_CHARACTERS
    for item in attachments:
        raw = base64.b64decode(item["data"])
        entry: dict[str, object] = {
            "filename": item["filename"],
            "content_type": item["content_type"],
            "bytes": len(raw),
        }
        if item["content_type"].startswith("image/") and item["content_type"] != "image/svg+xml":
            entry["image_url"] = f"data:{item['content_type']};base64,{item['data']}"
        elif item["content_type"] != "application/pdf" and not requires_filesystem(
            item["filename"]
        ):
            text = raw.decode("utf-8", errors="replace")
            entry["text"] = text[:remaining]
            entry["truncated"] = len(text) > remaining
            remaining = max(0, remaining - len(text))
        else:
            entry["note"] = (
                "Binary document: select using metadata; the answer receives the full file."
            )
        context.append(entry)
    return context


def attachment_prompt_data(request: dict[str, object]) -> str:
    """Keep binary image payloads out of the textual prompt."""
    context = request.get("attachments")
    if not context:
        return ""
    if not isinstance(context, list) or any(not isinstance(item, dict) for item in context):
        raise ValueError("Invalid Advisor attachment context")
    return (
        "<attachments>\n"
        + json.dumps(
            [
                {key: value for key, value in item.items() if key != "image_url"}
                for item in context
            ],
            ensure_ascii=False,
        )
        + "\n</attachments>\n"
    )


def advisor_image_inputs(request: dict[str, object]) -> list[dict[str, str]]:
    """Native multimodal turn inputs for the attachments visible in the prompt."""
    context = request.get("attachments", [])
    if not isinstance(context, list):
        raise ValueError("Invalid Advisor attachment context")
    images = []
    for item in context:
        if not isinstance(item, dict):
            raise ValueError("Invalid Advisor attachment context")
        url = item.get("image_url")
        if url is not None:
            if not isinstance(url, str) or not url.startswith("data:image/"):
                raise ValueError("Advisor images must be inline data")
            images.append({"type": "image", "url": url})
    return images
