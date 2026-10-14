# Generated response audio playback

The canonical artifact remains the lossless WAV. Timing sidecars continue to
validate against that WAV and the exact narration; no database migration is
required for playback copies.

The authenticated content route accepts `?format=mp3`. It lazily derives a
64 kbps MP3 using the host's optional `ffmpeg` / `libmp3lame`, caches it in the
existing ArtifactStore under the canonical WAV SHA-256 and encoding version,
and supports the same authorized byte-range requests as WAV. Encoding is
serialized, limited to one encoder thread, and times out after 60 seconds.
Missing/failed encoding falls back to the WAV. Existing clients requesting the
unqualified content URL still receive the original WAV. No model inference,
trimming, or alignment pass occurs. Seekable MP3 output retains gapless delay
metadata so playback positions use the original sample timeline.

The web player requests the smaller representation, retains native controls,
and retries a failed media request through the authenticated Blob transport.
An interrupted Blob download exposes a Retry audio button. The read-along
listeners follow the mounted audio element, including fallback remounts.

Word mapping uses a global monotonic sequence alignment, including joined
punctuation tokens such as GPT-5.5. This prevents narrated but collapsed work
notes from greedily consuming words in the final answer. DOM changes from lazy
Markdown rendering or expanding work notes rebuild the ranges, coalesced to a
single animation frame. Playback itself uses binary timestamp lookup, without
React updates per frame. Alignment is bounded to 16 million cells (32 MB);
excessively large inputs retain audio without highlighting.

## September 26 iPhone investigation

The phone reported failed native playback and then failed Blob downloading.
O1 logged successful response starts (200/206), which do not establish complete
body delivery. A full loopback download matched the original WAV hash.
The iPhone transport uses ordinary WKWebView networking, without a custom
binary fetch bridge. Tailscale logged connection churn, but the exact client
network exception was unavailable; a codec-specific root cause was not proven.

Today's exact 582.65-second recording was 27,967,244 bytes as PCM WAV and
4,661,997 bytes as MP3. Encoding took about 1.9 seconds. This reduces transfer
size by 83%; it does not claim to repair the phone's network connection.
Original WAVs, Qwen recordings, narration, schedules, and voice settings remain
untouched. Actual iPhone confirmation is still required after O1 deployment.

## Scheduled Kokoro policy

New scheduled sessions explicitly select the Kokoro backend. The legacy
`daily-brief` voice profile resolves to `kokoro-heart` both at session creation
and during audio generation/retry. Generated scheduled audio always requests
Kokoro and native timings, including for sessions that lack the backend field
or still carry a historical Qwen value. There is no Qwen fallback on failure.
Existing ready recordings remain playable.

On RTX, `OMNIGENT_TTS_URL` points to the shared Hatchet gateway on loopback
port 7790. The gateway submits `omnigent-text-to-speech` to its single-slot
worker and returns the timing multipart body unchanged. The coordinator logs
the returned Hatchet run ID with the response ID for tracing. The daily trigger
is owned by Omnigent's RRULE scheduler; Hatchet owns queued speech execution.
Validate both the next scheduled fire and a real Kokoro job with native timings
when accepting a deployment, rather than relying on the TTS health response.

## ElevenLabs scheduled narration

A scheduled task can select ElevenLabs with
`audio_voice_profile: "elevenlabs:<voice_id>"` and `audio_enabled: true`.
The audio worker retrieves `keychain:elevenlabs` through Omnigent's existing
secret store (service `omnigent`, account `elevenlabs` on macOS). The key belongs
to the user running the **server**, which performs audio post-processing; the
connected agent host does not receive it. A Mac Keychain entry is not available
to a server on another machine.

This route explicitly uses the standard `eleven_v4` model. It does not select
Turbo or Flash, set latency optimizations, retry a failed provider request, or
fall back to another speech service. It requests 24 kHz PCM through ElevenLabs'
[speech-with-timestamps endpoint](https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps)
and stores a WAV plus validated word timings for the existing player. Narration
over 9,000 characters is split at a sentence or word boundary; the exact text
and timing offsets are preserved across chunks. Provider failures are saved as
short error codes without response bodies or credentials. The written report
remains available when audio fails.

The direct ElevenLabs path does not depend on `OMNIGENT_TTS_URL` or the RTX
speech worker. Existing recordings remain unchanged. Generating speech sends
the cleaned report narration to ElevenLabs and uses that account's credits.
Verify the intended voice and a short real V4 generation before enabling the
recurring task; mock tests alone do not establish account/model access.
