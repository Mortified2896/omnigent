# Current model as the Model Advisor

Choose **Current model** in the existing Recommender model picker, then choose
the advisor's reasoning effort independently. This is a saved setting, not a
model-specific mode. The allowed answer pool, provider switches, execution
effort, model pinning and approval controls retain their existing roles. To try
only reasoning changes for a checkpoint, select only that checkpoint's desired
efforts in the answer pool.

In a new chat, the advisor uses the selected execution model with the existing
bounded standalone request. Once a native Codex chat exists, the advisor makes
one ephemeral fork of its history on the same app-server, pins the verified
current model, updates only the child's effort and appends the advisor request.
It adds no developer instruction override, tool-definition override, output
schema override or caller-supplied cache key. The existing trusted policy hook
denies every tool on the marked advisor child, including MCP and agent tools.
The child is not registered as a visible side chat and is unsubscribed afterward.

Current-model recommendations can run while the main response is active. Run or
automatic confirmation waits until that response finishes; the server also
checks idle state before claiming and immediately before applying a route.
Context access is bound to the authorized continuation session and its local
bridge. Missing context or an old/untrusted hook fails the recommendation with
an explanation, without silently starting a fresh-context call. Existing native
sessions must resume with the updated harness to register the new guard.

Context inheritance is not a guarantee of provider KV-cache reuse. Changing
request-level reasoning can change the rendered prefix. OpenAI documents
append-only `configuration_update` items on supported models for preserving
that prefix; the native harness controls whether it uses that mechanism.
See [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).
Reported cached-input counters are preserved as evidence; missing counters stay
unknown.

An isolated native check on Codex CLI 0.153.4 with its available GPT-6 Astra
confirmed inherited synthetic history, concurrent parent completion, strict JSON
selection and actual PreToolUse denial. A High-effort advisor reported 9,088
cached tokens out of 16,573 input tokens. Two Low-effort advisor calls reported
zero cached tokens. This verifies the functional path and exposes the older
runtime's effort-change limitation; it does not establish Sol 6.1 cache behavior.
Keep the advisor's effort equal to the current effort when testing cache reuse on
that runtime. Do not enable experimental reasoning overrides across unsupported
models or claim cache preservation without native request/usage evidence.
