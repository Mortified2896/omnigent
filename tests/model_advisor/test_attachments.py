"""Attachment boundaries, router context, and immutable round identity."""

import base64
import io
from dataclasses import replace

import pytest
from PIL import Image

from omnigent.model_advisor_attachments import (
    MAX_ADVISOR_TEXT_CHARACTERS,
    advisor_attachment_context,
    advisor_image_inputs,
    attachment_prompt_data,
    normalize_attachments,
)
from omnigent.model_advisor_provider_workflow import LogicalFrozenRound
from tests.model_advisor.test_provider_workflow import frozen_round


def attachment(filename, content, content_type="text/plain"):
    return {
        "filename": filename,
        "content_type": content_type,
        "data": base64.b64encode(content).decode(),
    }


def test_text_context_is_bounded_without_losing_frozen_bytes():
    raw = b"x" * (MAX_ADVISOR_TEXT_CHARACTERS + 50)
    files = normalize_attachments([attachment("notes.txt", raw), attachment("code.py", b"second")])
    context = advisor_attachment_context(files)
    assert len(context[0]["text"]) == MAX_ADVISOR_TEXT_CHARACTERS
    assert context[0]["truncated"] is True
    assert context[1]["text"] == "" and context[1]["truncated"] is True
    assert base64.b64decode(files[0]["data"]) == raw


def test_image_is_a_native_input_and_not_base64_in_the_text_prompt():
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(stream, format="PNG")
    files = normalize_attachments([attachment("screen.png", stream.getvalue(), "image/png")])
    context = advisor_attachment_context(files)
    images = advisor_image_inputs({"attachments": context})
    assert images == [{"type": "image", "url": context[0]["image_url"]}]
    prompt = attachment_prompt_data({"attachments": context})
    assert "screen.png" in prompt
    assert "image_url" not in prompt and files[0]["data"] not in prompt


def test_pdf_is_honestly_described_for_selection():
    files = normalize_attachments([attachment("paper.pdf", b"%PDF-1.7", "application/pdf")])
    context = advisor_attachment_context(files)
    assert context[0]["filename"] == "paper.pdf"
    assert "metadata" in context[0]["note"]
    assert "text" not in context[0]
    assert advisor_image_inputs({"attachments": context}) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"data": "not-base64!"},
        {"filename": ""},
        {"content_type": "application/x-unknown", "filename": "unknown.bin"},
        {"file_id": "foreign-file"},
    ],
)
def test_invalid_or_external_attachment_is_rejected(changes):
    with pytest.raises(ValueError):
        normalize_attachments([{**attachment("notes.txt", b"hello"), **changes}])


def test_same_name_with_changed_bytes_changes_round_identity():
    original = frozen_round()
    first = replace(
        original, attachments=normalize_attachments([attachment("notes.txt", b"first")])
    )
    second = replace(
        original, attachments=normalize_attachments([attachment("notes.txt", b"second")])
    )
    assert first.fingerprint != second.fingerprint != original.fingerprint
    assert LogicalFrozenRound.from_payload(first.to_payload()) == first
    assert first.advisor_input()["attachments"][0]["text"] == "first"


def test_remote_image_context_is_rejected():
    with pytest.raises(ValueError, match="inline data"):
        advisor_image_inputs({"attachments": [{"image_url": "https://example.com/private"}]})
