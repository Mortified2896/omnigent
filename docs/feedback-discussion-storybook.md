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

The revised mobile layout uses one 12 px conversation gutter. Feedback comments
and proposed changes sit directly in the transcript, with separators instead of
nested cards. Saved comments are readable text with **Edit**; selected tags stay
visible while **Edit tags** opens the full picker. A proposal initially shows its
rating, tags, and comment as text, followed by **Accept changes**, **Edit**, and
**Reject** in one action row. **Edit** opens the shared shadcn fields and **Done**
returns to the readable view without applying anything. The default **Changes** view uses Word-style marks: additions are underlined and
removals are struck through, including changed ratings and tags. **Final text**
shows exactly the values acceptance will save, without change marks. Original
comments are also available through a disclosure while editing or reading Final text. Reply voting and review controls share the actual
message action toolbar with copy actions. Desktop retains the application's wider
column and composer surface.

Under **Prototypes / Feedback discussion**, compare:

| Story | Interaction after saving feedback |
| --- | --- |
| Footer action | **Feedback discussion** prepares an editable request to discuss your assessment in the ongoing chat composer. |
| Quick prompts | Both primary buttons prepare a composer draft. **More feedback options** reveals discussion and review shortcuts. |
| Inline discussion | Both primary buttons prepare a composer draft. **More feedback options** opens a custom question field; **Send to chat** submits that explicitly composed question. |

All three keep **Self Reflection** as a separate action: it prepares a request to
review the agent's actions and tool results, independently of the feedback's save
status. The simulated inspection reply reviews the action record and does not
propose feedback edits. Discussion instead addresses the user's assessment. It
offers **Review feedback** as an explicit follow-up. Only a request for
changes produces an editable proposal; ordinary discussion follow-ups do not.

The stories render the application's actual `BubbleView`, `ChatHeader`,
`ResponseFeedbackActions`, `ChatComposer`, workspace controls, and
`SuggestedFeedback`. Transcript and composer geometry are shared with the app,
including the scroll area, fixed composer, mobile spacing, and theme CSS. The
three proposed controls live in one reusable `FeedbackDiscussionControls`
component for eventual integration. The composer also uses the production
`useAutoGrowTextarea` hook. The inspection toolbar sits outside the product
surface. There is no separate imitation feedback form.

Choose **Partial** under the answer, open **Add comment**, and choose a tag through
**Edit tags** on mobile. The real
feedback editor auto-saves: outcome clicks save immediately, tags save
immediately, and comments save after a 500 ms pause. Wait for **Saved**, open
**Feedback discussion**, then inspect the prepared message in the ongoing composer. It sends after five
seconds unless you start editing, stop the countdown, or dismiss its context.
Editing leaves the draft for manual sending. Existing drafts are preserved and
never auto-sent. Switching an untouched prepared action restarts the countdown.
Hiding the page or resetting the demo cancels the countdown.
Read the discussion reply, choose **Review feedback**, and send that
request if you want an edit proposal. Choose **Edit** to adjust the proposed rating (Success, Partial, Failed, or Not sure), tags, and
comment, then accept; or reject the entire proposal without changing your feedback.
Each AI feedback reply also has independent thumbs up/down controls for usefulness.
Voting never accepts a proposal. Story votes are fixture state and reset on reload. Acceptance updates the same
feedback editor, including the selected rating; **Undo** restores the previous
rating, comment, and tags. Send another message to inspect a follow-up in that
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
ready starting point. All 64 focused tests passed, together with TypeScript, lint, formatting, and the
applicable pre-commit checks. The compiled Storybook build passed.
The latest checks include countdown timing and cancellation, editing all proposed
fields, restoring drafts with a stable proposal ID, rating changes and Undo, and
reply votes that cannot apply proposals. The mobile redesign passed 64 focused
feedback/composer tests and five message rendering tests, including contextual
controls updating when the answer itself is unchanged. The broader message test
file has 18 existing failures caused by `ResponseRouteBadge` rendering without a
query provider; running an unmodified HEAD copy of that source and test reproduced
the same 18 failures. Baseline evidence is retained in
`output/feedback-message-baseline-tests.log`.

Browser inspection covered all three stories at 320 by 568, 390 by 844, and 1440 by 900 on
the compiled private preview. Each fit without horizontal overflow and kept the
composer at the viewport bottom; the desktop chat column measured the shared
768 px width. The footer flow was checked from an outcome click and comment
auto-save through sending, editing a proposed comment, accepting it into the
original form, and Undo restoring the previous comment with the previous rating restored. Quick prompts preserved an existing composer draft. All three sent
their discussion into one conversation without producing automatic edit
proposals. The independent inspection action also preserved the original
feedback and produced no proposal or outbound API request. The dark theme and a fresh browser
asset load were also inspected. These are browser viewport checks; they do not
establish physical iPhone keyboard behavior or live production inference.

Before the tracked-change update, the revised proposal's readable view measured 366 px wide and 279 px high on the
390 px viewport; the previous proposal's editable comment had only 308 px of
usable width. Editing now uses the full 366 px. On the 320 px viewport the proposal
measured 296 px wide. The mobile dark composer also uses only a top border: the
previous side borders reduced its inner width by two pixels and wrapped the
controls into a second row at 390 px. Rendered verification confirmed that both
action groups now share one row in light and dark themes. Saved comment editing,
returning to its readable view, and auto-save were checked in the browser.

Rendered screenshots are retained under `output/playwright/` and copied into
the compiled preview's `inspection/` directory for phone access. A subsequent
Storybook build replaces the compiled directory; retain the originals in
`output/playwright/` and copy them again if needed. The gallery source is retained
at `output/feedback-storybook-inspection.html`; copy it to `inspection/index.html`
after a rebuild. The gallery includes a disclosure comparing the actual previous
proposal screenshot with the redesigned review.

To remove this task's private proxy, use `tailscale serve --https=8443 off`;
do not reset the whole Tailscale Serve configuration. Verify the PID file against
the running command before stopping the task's static file server.

The updated browser flow verified the five-second auto-send, editing cancellation,
manual sending of an adjusted request, editing a proposed rating/tag/comment,
independent thumbs voting, acceptance into the original form, Undo of the rating,
and rejection without changing saved feedback. Updated phone screenshots include
`feedback-countdown-mobile.png`, `feedback-edited-draft-mobile.png`, and
`feedback-review-mobile.png` under `output/playwright/`.

The tracked-change update passed 76 focused tests across eight files, including
Unicode and whitespace preservation, bounded work for long unrelated comments,
literal handling of HTML-like text, switching to Final text, and explicit
acceptance of the displayed rating, tags, and comment. Change marks never apply
feedback automatically.

Rendered tracked changes were checked at 320 by 568 and 390 by 844 in light
and dark themes, and 1440 by 900, without horizontal overflow. Browser interaction
verified changes to all three fields, the clean Final text view, acceptance into
the original form, Undo restoring all previous values, and rejection preserving
the saved feedback. The gallery includes actual captures of a manually edited
proposal changing Partial to Failed, replacing its tag, and extending its comment;
the ready-suggestion fixture starts with Failed feedback and proposes Partial, so its rating can be reviewed immediately. TypeScript, lint,
formatting, applicable pre-commit checks, and the compiled Storybook build passed.

Individual review: select a marked comment edit, a changed rating, or a tag
addition/removal to open its compact Accept/Reject popover. Adjacent removed and
added text is one replacement decision. Accepted edits show their replacement;
rejected edits retain the original. Selecting a resolved edit lets the owner
reverse that decision. Decisions persist with the existing proposal ID and do
not write feedback until explicit save. Final text previews the mixed result.
If changes remain unresolved, **Accept remaining & save** explicitly accepts
those remaining suggestions; otherwise **Save reviewed feedback** saves the
reviewed result. Editing the proposal starts a fresh individual review. Reject
dismisses the proposal without saving; Undo restores the previous complete
feedback and clears the decisions. A failed save keeps decisions available.

Individual rating changes use the same struck-through old value, underlined
replacement and independent popover controls as text. An unchanged rating has
no unnecessary Accept/Reject controls. Browser interaction checked rejecting
a rating while accepting comment edits, saving the mixed final result, and Undo
restoring the original rating and comment.

The individual-review update passed 80 focused tests across nine files and all
applicable pre-commit checks, including TypeScript, lint and formatting.

The suggestion-ready starting point seeds Failed feedback and proposes Partial
in all three variants. Its original form and discussion snapshot use the same
seed; other starting points keep their existing Partial feedback.

Rating and tag corrections reuse `FeedbackChoiceButton`, also used by the
original answer's feedback editor. Pending corrections keep the original choice
filled and mark its removed text; proposed choices use the answer's unselected
button treatment with underlined added text. Changed tags open the same compact
individual decision toolbar. Compact tag labels are shared as well. The
suggestion-ready fixture now includes Environment/dependency → Tests/verification
alongside Failed → Partial. The original editor, proposal and Storybook flow
passed 47 focused tests after this shared-component change.

The suggested rating row now uses the same shared options and order as the answer review: Success, Partial, Failed, Not sure. All four remain visible before and after individual acceptance or rejection; only the original and proposed ratings receive pending change marks. The focused component and Storybook tests cover the stable order and selected rating after both decisions.

The **Advisor prompt tags** story (`prototypes-feedback-discussion--prompt-tags`)
adds a proposal below the initial user message. Try rejecting Testing and applying
the remaining UI tag, or edit the set using the regular Task tags picker. These
are the application's `MessageTaskTags` and `PromptTagSuggestions` components;
the story supplies a simulated authenticated tag-save response. Accepted tags
replace the proposal on the prompt. Rejecting the whole proposal retains existing
tags. Neither action changes the prompt text.

In the application, initial-prompt tag proposals come from the existing logical
Model Advisor call and remain separate from accepted prompt tags. Native pending
inputs, transcript mirroring, and persisted history carry the proposal metadata.
The ongoing-chat footer actions prepare a five-second draft in the real composer,
and review replies use the same tracked-change component with explicit saves and
independent reply votes. The ongoing chat keeps its model configuration during
feedback discussion; the advanced side-chat action remains available. Storybook
responses and cache indicators remain simulated.
