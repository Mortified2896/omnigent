# Feedback discussion prototypes

Prepared on `codex/feedback-discussion-storybook` for owner inspection. The
compiled Storybook is available privately on the owner's Tailscale network for
phone inspection. The Storybook development process remains deferred until the
owner returns.

Private preview: <https://macbook-pro.taile0361b.ts.net:8443/>. The phone needs
Tailscale connected and the Mac must stay awake. A loopback static file server on
port 6006 serves only `web/storybook-static`; Tailscale Serve adds an HTTPS proxy
on port 8443. The existing port 443 mapping is preserved. The task's static server
PID and log are in `output/feedback-storybook-static.pid` and
`output/feedback-storybook-static.log`. The owned static server helper is
`output/feedback_storybook_static_server.py`; its request queue handles the
production components' parallel module loads. HTML and the story index are not
cached, while hashed assets may be cached.

The screenshot comparison page at
<https://macbook-pro.taile0361b.ts.net:8443/inspection/> links to the interactive
variants. Each image is an actual browser capture of the story at 390 by 844.

Under **Prototypes / Feedback discussion**, compare:

| Story | Interaction after saving feedback |
| --- | --- |
| Footer action | **Feedback discussion** prepares an editable request to discuss your assessment in the ongoing chat composer. |
| Quick prompts | **Feedback discussion** reveals **Discuss my feedback** and **Suggest feedback changes**, which prepare an editable request in the same composer. |
| Inline discussion | **Feedback discussion** opens a small field below the answer. **Send to chat** sends your question as a normal turn in the ongoing chat. |

All three keep **Self Inspection** as a separate action: it prepares a request to
review the agent's actions and tool results, independently of the feedback's save
status. The simulated inspection reply reviews the action record and does not
propose feedback edits. Discussion instead addresses the user's assessment. It
offers **Suggest feedback changes** as an explicit follow-up. Only a request for
changes produces an editable proposal; ordinary discussion follow-ups do not.

The stories render the application's actual `BubbleView`, `ChatHeader`,
`ResponseFeedbackActions`, `ChatComposer`, workspace controls, and
`SuggestedFeedback`. Transcript and composer geometry are shared with the app,
including the scroll area, fixed composer, mobile spacing, and theme CSS. The
three proposed controls live in one reusable `FeedbackDiscussionControls`
component for eventual integration. The composer also uses the production
`useAutoGrowTextarea` hook. The inspection toolbar sits outside the product
surface. There is no separate imitation feedback form.

Choose **Partial** under the answer, enter a comment, and choose a tag. The real
feedback editor auto-saves: outcome clicks save immediately, tags save
immediately, and comments save after a 500 ms pause. Wait for **Saved**, open
**Feedback discussion**, then send the request in the ongoing conversation.
Read the discussion reply, choose **Suggest feedback changes**, and send that
request if you want an edit proposal. Edit and accept the proposed changes or
reject them. Acceptance updates the same
feedback editor while preserving the outcome; **Undo** restores the previous
comment and tags. Send another message to inspect a follow-up in that
conversation. **Start again** resets the current starting point.

The `startingPoint` control can jump to saved feedback, a discussion reply, an
inspection reply, or a ready suggestion.
`responseDelayMs` changes the simulated reply delay. `cacheTelemetry` compares an
unavailable indicator with example provider-reported counters. Both the replies
and cache counters are simulated. State lives only in the story and resets on
reload. The production feedback hooks use an in-memory implementation of their
API contract through a Storybook fetch adapter installed before the query
components mount. It never forwards API requests, even for an unexpected session
ID, and restores the original fetch function when the demo unmounts or resets.
No real sessions or model requests
are created. The preview reproduces the production chat surface with fixture
data; unrelated navigation and attachment actions are inactive. These stories do
not select a production variant or implement cache telemetry.

When the owner returns, start Storybook from this worktree:

```sh
cd /Users/Jo/.codex/worktrees/feedback-discussion-mode/omnigent/web
npm run storybook -- --host 127.0.0.1
```

The default port is 6006; verify and stop only this task's static server before
replacing it with the development process. The private proxy can continue using
the same port. Once the server is running, open these story paths:

```text
/?path=/story/prototypes-feedback-discussion--footer-action
/?path=/story/prototypes-feedback-discussion--quick-prompts
/?path=/story/prototypes-feedback-discussion--inline-proposal
```

Preparation checks:

```sh
npm --prefix web test -- src/storybook/FeedbackDiscussionPrototype.test.tsx src/components/ResponseFeedbackActions.test.tsx src/components/composer/ChatComposer.test.tsx
npm --prefix web run type-check
npm --prefix web run build:storybook
```

For mobile inspection, use `iframe.html?id=<story-id>&viewMode=story` to open a
story without the manager sidebar. The focused tests cover the real feedback
auto-save, draft preservation, ongoing-chat turns, the distinct discussion and
inspection intents in every variant, explicitly requesting a proposal, accepting
and undoing suggestions, rejection, missing cache telemetry, blocked outbound
calls, transport restoration, restarting the demo, and ongoing context at each
ready starting point. All 42 focused
Storybook, feedback editor, and composer
tests passed, as did the applicable pre-commit checks and static build.

Browser inspection covered all three stories at 390 by 844 and 1440 by 900 on
the compiled private preview. Each fit without horizontal overflow and kept the
composer at the viewport bottom; the desktop chat column measured the shared
768 px width. The footer flow was checked from an outcome click and comment
auto-save through sending, editing a proposed comment, accepting it into the
original form, and Undo restoring the previous comment with the outcome
unchanged. Quick prompts preserved an existing composer draft. All three sent
their discussion into one conversation without producing automatic edit
proposals. The independent inspection action also preserved the original
feedback and produced no proposal or outbound API request. The dark theme and a fresh browser
asset load were also inspected. These are browser viewport checks; they do not
establish physical iPhone keyboard behavior or live production inference.

Rendered screenshots are retained under `output/playwright/` and copied into
the compiled preview's `inspection/` directory for phone access. A subsequent
Storybook build replaces the compiled directory; retain the originals in
`output/playwright/` and copy them again if needed. The gallery source is retained
at `output/feedback-storybook-inspection.html`; copy it to `inspection/index.html`
after a rebuild. Its comparison screenshot is `inspection/comparison.png`.

To remove this task's private proxy, use `tailscale serve --https=8443 off`;
do not reset the whole Tailscale Serve configuration. Verify the PID file against
the running command before stopping the task's static file server.
