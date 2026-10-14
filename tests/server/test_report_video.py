"""Report scenes, fixed-domain image search, and original-audio video finishing."""

import hashlib
import io
import json
import math
import struct
import subprocess
import wave

import httpx
import pytest

from omnigent.server.generated_response_audio import clean_narration
from omnigent.server.report_video import (
    VideoManifest,
    build_video_scenes,
    download_commons_image,
    finish_report_video,
    search_commons,
    validate_manifest,
)


def test_scenes_use_original_text_and_native_times_without_action_metadata():
    markdown = (
        "# Daily Report\n\n## News\n\n"
        "- **Omnigent:** Recovery was improved today.\n\n"
        "- **Elon Musk:** A new launch is scheduled.\n\n"
        '```omnigent-report-actions\n{"actions": []}\n```'
    )
    narration = clean_narration(markdown)
    offset = narration.index("Elon Musk")
    units = [{"narration_start": offset, "start_seconds": 17}]
    scenes = build_video_scenes(markdown, narration, 30, units)
    assert scenes[0]["start"] == 0
    assert scenes[-1]["start"] == 17
    assert scenes[-1]["title"] == "Elon Musk"
    assert all("actions" not in scene["summary"] for scene in scenes)
    assert (
        build_video_scenes("", "Original narration", 10, [])[0]["summary"] == "Original narration"
    )


def test_manifest_binds_original_recording_and_ordered_complete_duration():
    audio = b"original audio"
    manifest = VideoManifest(
        audio_sha256=hashlib.sha256(audio).hexdigest(),
        duration=10,
        scenes=[{"start": 0, "title": "News", "summary": "", "image_query": ""}],
    )
    validate_manifest(manifest, audio, 10)
    with pytest.raises(ValueError, match="changed"):
        validate_manifest(manifest, b"replacement audio", 10)
    with pytest.raises(ValueError, match="complete"):
        validate_manifest(manifest, audio, 12)
    manifest.scenes[0].start = 2
    with pytest.raises(ValueError, match="ordered"):
        validate_manifest(manifest, audio, 10)


@pytest.mark.asyncio
async def test_image_search_rejects_untrusted_hosts_and_nonreusable_licenses():
    def page(url, license="Public domain"):
        return {
            "title": "File:Portrait",
            "imageinfo": [
                {
                    "thumburl": url,
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Portrait",
                    "mime": "image/jpeg",
                    "extmetadata": {
                        "Artist": {"value": "<a>Official photographer</a>"},
                        "LicenseShortName": {"value": license},
                    },
                }
            ],
        }

    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": {
                        "1": page("https://upload.wikimedia.org/portrait.jpg"),
                        "2": page("https://localhost/private"),
                        "3": page("https://upload.wikimedia.org:bad/file"),
                        "4": page(
                            "https://upload.wikimedia.org/copyright.jpg", "All rights reserved"
                        ),
                    }
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        results = await search_commons(client, "Donald Trump")
        assert len(results) == 1
        assert results[0]["credit"] == "Official photographer"
        assert seen[0].url.host == "commons.wikimedia.org"
        assert "cookie" not in seen[0].headers and "authorization" not in seen[0].headers
        with pytest.raises(ValueError, match="verified"):
            await download_commons_image(client, "http://localhost/private")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"html", headers={"content-type": "image/jpeg"})
        )
    ) as client:
        with pytest.raises(ValueError, match="match"):
            await download_commons_image(client, "https://upload.wikimedia.org/image.jpg")


def test_finisher_muxes_original_audio_with_fractional_duration(tmp_path):
    duration = 3.13
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(
            b"".join(
                struct.pack(
                    "<h",
                    int(15000 * math.sin(i * 2 * math.pi * 440 / 48000))
                    if 4800 <= i < 48000
                    else 0,
                )
                for i in range(round(duration * 48000))
            )
        )
    visuals = tmp_path / "visuals.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=1280x720:r=30",
            "-t",
            str(duration),
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            str(visuals),
        ],
        check=True,
    )
    output = finish_report_video(visuals.read_bytes(), buf.getvalue(), duration)
    finished = tmp_path / "finished.mp4"
    finished.write_bytes(output)
    info = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(finished)]
        )
    )
    video, audio = info["streams"]
    assert video["codec_name"] == "h264" and video["width"] == 1280
    assert audio["codec_name"] == "aac" and float(audio["start_time"]) == 0
    assert abs(float(audio["duration"]) - duration) < 0.001
    # AAC encoder priming is skipped: the original 100 ms silence is retained.
    pcm = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(finished),
            "-map",
            "0:a",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-",
        ]
    )
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    first = next(i for i, sample in enumerate(samples) if abs(sample) > 3000)
    assert abs(first / 48000 - 0.1) < 0.005
    with pytest.raises(ValueError, match="Invalid"):
        finish_report_video(b"not an MP4", buf.getvalue(), duration)
