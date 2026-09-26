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
