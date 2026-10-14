# Daily Report video

Open a scheduled report and choose **Video report** below the chat. The editor
reuses a saved report recording, up to 30 minutes. Preview the Finance vector
presenter and story cards, edit titles/text/start times, optionally search for
illustrative images, then choose **Create video**. After saving, choose **Watch
saved video** to watch or download the MP4. Creation is explicit and optional;
opening a report does not render video or request new narration.

The React editor and deterministic Canvas presenter are adapted from Article
Lab's `experiments/animated-explainer` prototype in Finance-R-Simulations.
Mediabunny/WebCodecs encodes 1280×720 H.264 visuals at 30 fps on the viewing
device. The Mac uses local FFmpeg to combine these with the complete original
WAV recording and save an MP4. No text, speech, image-generation, or video-model
API is called. The encoder is loaded only when creating a video. Current Chrome
or Safari with H.264 encoding support and local FFmpeg/ffprobe are required.

Saved ElevenLabs word timings anchor scenes when available. Older recordings
without a valid timing sidecar use visibly labeled estimates, with editable
scene starts. Preview animation follows `audio.currentTime`, including seeking.
The presenter's mouth/gestures are stylized animation, not phoneme lip sync.

Image search sends only the entered query to Wikimedia Commons. It offers
verified JPEG/PNG/WebP thumbnails with public-domain or CC0/CC BY/CC BY-SA
metadata. Select images manually after checking their title/source. The video
shows illustrative-image labels, author/license credits and source URLs;
graphic cards remain available when no suitable image exists. Remote HTML is
treated as data. Fetches allow only fixed Commons HTTPS hosts, no redirects,
and never forward user credentials. Audio, images and videos stay scoped to the
conversation's read/edit permissions.

Exports are limited to 128 MiB. The server checks the original audio SHA,
complete duration, ordered scenes, scoped images, video codec/dimensions/frame
rate and bounded upload before publishing. A recording's video, manifest and
cached images expire with its audio retention period. Original audio and report
text are preserved during rendering, cancellation and retry.
