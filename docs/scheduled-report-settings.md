# Editing recurring reports

Owners of an automation can open **Report settings** above the reply box in any
conversation produced by that automation. **Request a change** adds a plain
language instruction to the latest saved prompt. **Edit full prompt** replaces
the prompt. Both paths save only the prompt, leaving the schedule, host, model,
and voice settings untouched. The panel displays **Saved for future runs** only
after the server returns the saved task; failed saves preserve the draft.

The panel fetches one owner-scoped task when opened. It does not mount the Tasks
page's polling query throughout the app. It reloads the latest prompt before
adding an instruction and rejects a full edit if the saved prompt changed since
the editor opened. Settings **General → Reports and automations → Manage
automations** links to the existing editor for schedule and other task settings.

New scheduled runs include their originating task ID and follow-up guidance in
the dispatched prompt. The agent answers ordinary report questions normally.
An explicit request to change future reports uses the existing
`sys_scheduled_task_list` and `sys_scheduled_task_update` tools: read the current
task, preserve unrelated instructions and settings, then acknowledge a successful
save. Editing the automation does not rewrite earlier reports. The dispatched
context is not stored back into the automation's saved prompt.

ElevenLabs report narration uses **V4 Turbo** (`eleven_v4_turbo`) through the
Text to Dialogue WebSocket. It keeps the selected voice and report text, saves
WAV audio, and includes word timings when the provider alignment matches the
original narration. Existing recordings keep their audio. Each text chunk is
generated once; failures do not automatically retry or fall back to a more
expensive model. [ElevenLabs pricing](https://elevenlabs.io/pricing/api) lists
V4 Turbo at half the standard V4 API rate, including on Starter; actual usage
is metered by the subscription account.

Codex reset reporting should use the host's read-only
`/v1/hosts/{host_id}/codex-rate-limits` response, including its actual window
duration and `resets_at` timestamp. Do not assume every account has the same
windows. Compare earlier observations from the same automation for scheduled
rollovers; distinguish those from confirmed provider announcements or unknown
one-off resets. A decrease in used percentage alone is not a reset event.
