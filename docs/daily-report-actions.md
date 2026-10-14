# Daily report decisions

Scheduled reports can offer up to three actionable findings at their end. Each
finding offers **Review first**, **Try it safely**, and **Not now**, with nothing
preselected. Choose any findings, use **Add choices to chat**, edit the prepared
request if desired, and use the ordinary **Send** button. Existing draft text,
reply quotes and attachments are preserved. Selecting choices does not send a
message, change an automation, install software or approve production promotion.

Review asks for a fresh compatibility/evidence check. Try asks for a reversible
trial on the available named host, following project release rules and requiring
approval before production promotion. The resulting request instructs the agent
to use the relevant OmniRoute Agent Skill before OmniRoute operations. Later
and unselected findings authorize no action.

To offer the panel, the report's final answer ends with one complete block in
one text message:

````markdown
```omnigent-report-actions
{"schema_version":1,"actions":[{"title":"Review the relevant software update","target":"Software on the verified available host","why":"Why this update matters for our installed setup.","source_url":"https://example.com/release"}]}
```
````

This is finding data only: the renderer accepts bounded title/target/reason
fields and an optional HTTP(S) source without URL credentials. It ignores
commands and model-written option/prompts. Quoted blocks, examples, incomplete
or invalid metadata stay ordinary Markdown. The panel only appears in the
owning scheduled-report surface after the response settles. It is not a pending
agent question, so completion, run history and report audio need not wait for an
answer. The speech cleaner removes fenced metadata; the spoken summary should
briefly mention the available choices in ordinary prose.

The saved Daily Report prompt controls when to suggest decisions. Offer them
only for evidence-backed developments relevant to the actual installed setup;
never fill three slots just to fill them, assume an upstream release is safe for
our customized fork, or start a trial while researching the unattended report.
For existing reports, **Choose actions** beside **Report settings** prepares a
request to check that report's findings and use the current saved decision-card
format. Send it to receive a panel in the follow-up answer. Changing the saved
prompt does not rewrite old reports or regenerate their audio.
