# Routing evidence audit and evaluation plan — 11 October 2026

Objective: maximize task completion; latency, paid API spending, subscription quota,
router overhead and override behavior are secondary measured tradeoffs. The goal
remains active. This is the first research milestone, not a proven routing improvement.

## Sources actually inspected

Source checkout: fork `Mortified2896/omnigent`, current fetched main at
`cb6d86659114380df14625d33e051ad6ca6becc8`. Research branch
`codex/routing-evidence-audit` uses its own managed worktree. Main checkout is unchanged.
HomeLab owns hosts, GPU resources, service configuration and deployment.

| Collection | Verified size | What it supports |
| --- | --- | --- |
| Mac Omnigent `~/.local/share/omnigent-mac/data/chat.db` | 12 sessions; seven explicitly marked test sessions | Answer reviews, response execution and advisor decisions |
| Latest Omnigent human reviews | 203 raw revisions reduce to eight latest caller/response outcomes, three eligible known outcomes | Two failed and one partial; five latest successes are excluded with test sessions |
| Mac completed answer keys | 19 response keys, seven eligible; four eligible responses have no known outcome | Response-level review queue, not independent task count |
| Codex `~/.codex/state_5.sqlite` and referenced JSONL rollouts | 1,011 indexed chats; 737 root chats after excluding 274 subagent/guardian rows; all 737 rollout files exist | Multi-project requests, transcripts and tool evidence; 3.54 GB of root rollouts, not all semantically reviewed |
| Root Codex index attribution | Two missing model and two missing effort entries | Current/last model metadata; per-turn contexts and request records are stronger and still require full audit |
| ZCode `~/.zcode/cli/db/db.sqlite` | 23 sessions, 1,525 messages, 6,241 parts, 1,339 model requests, 71 turns, 1,520 tool usage records | Structured joins, tokens, latency, retry and tool evidence |
| ZCode join coverage | 1,339/1,339 requests join to parent user messages; 1,320/1,339 join to assistant messages; 23 trace IDs | Reliable request/session joins; 19 request records lack a matching saved assistant message |
| ZCode missingness | No request missing model/provider/variant, latency, input or output token counts | Runtime comparison and retrospective review; `variant` needs semantics checked against GLM configuration |
| ZCode runtime status | 43 completed, 12 error, 16 cancelled turns; summed model-retry counters 46 | Execution outcomes, not proof of human task completion |
| Local collector forensic trace segment | 129 rotated/current JSON files, 2,622,037 span records before deduplication; services are Codex-related | Telemetry volume and service attribution; no ZCode service in this particular retained segment |
| RTX O1 collection | Not read: normal SSH times out and recovery route reports no route to host | No current RTX counts or GPU qualification claimed |
| Local OmniRoute API | Node listeners verified on loopback `:20128` and `:20131`; `GET /api/combos` returns 401 | Service responds but authenticated combo/quota/log inventory is still missing; skill paths were read first |

Root Codex project counts by directory identity include BlockNations 279,
Finance/Article Lab 114, HomeLab 56, Omnigent-named directories 72 and legacy
Control Room 66. These counts include multiple tasks per chat and exclude
subagent/guardian rows. Legacy Control Room is historical data, not source or a
runtime to reactivate. Different host copies can duplicate the same work.

All three eligible Omnigent outcomes join to a response model, effort and access
lane; two join to stored advisor rounds. Model provenance is `session_reported`
for all three, not direct `response_usage`. They cover GPT-6.1-Sol (two) and
GPT-6-Luna (one), high effort (two) and medium (one), direct Codex (two) and
OmniRoute (one). The sample cannot establish comparative model quality.

Nine stored advisor rounds report latency/input/output/cache tokens. Observed
latency ranges from 10.30 to 29.835 seconds; median 14.485 seconds. These mix test
and ordinary records, so they establish recorded overhead only. Session usage
has by-model totals, tokens, cache reads and cost; it cannot allocate historical
turn cost/latency without response-level linkage. No inference snapshots are
present in the twelve Mac metadata rows. Subscription money, API list prices,
measured spending and quota consumption must remain different measures.

## Existing routing, before any replacement

- Smart Routing uses a built-in LLM selector or a gateway `task_v1` router. The
  built-in rubric classifies simple/moderate/complex tasks and chooses a
  relative model position; its harness descriptions and cost/capability order
  are policy assumptions, not calibration on these tasks.
- Model Advisor freezes qualified logical model/effort choices and transport
  preferences, records advice and overrides, and uses owner/host-scoped CAS
  confirmation. Current-model advice can use a guarded ephemeral native Codex
  history fork. A confirmed continuation must stay on the same host, agent and
  workspace and pass idle checks. Native cache reuse is not guaranteed.
- O3 review has benchmark adequacy gates, versioned definitions and distinct
  exact/proxy/advisory/forecast evidence. Default custom Terminal-Bench slices
  have empty manifests. Default candidate profiles/probes are September-era
  snapshots. A cross-harness 0.03 score buffer and task difficulty thresholds
  are explicit policy assumptions. They are not observed probabilities of task
  completion. LLM forecast ranges are not measured benchmark scores.
- Turn routing handles the first real prompt; hooks block, switch while idle,
  and durably replay the prompt. It is not arbitrary next-turn adaptation.
- Native subagent routing is an independent opt-in hook path. Cross-harness
  redirection and model restrictions have their own contracts. Some native
  spawns lack task text and get a placeholder/floor route; that is not learned
  task routing.

Relevant owning files: `omnigent/server/smart_routing.py`, `routing_backend.py`,
`model_advisor_service.py`, `response_attribution.py`, `task_scoring.py`,
`o3_routing_review/{registry,evaluator,forecaster,store}.py`,
`omnigent/runner/{turn_routing,subagent_routing}.py`.

## Benchmarks most plausibly related to this workload

This ordering is a relevance hypothesis, not measured correlation. Sampled recent
chats involve shipping multiplayer/lobby behavior, UI prototypes and article
imports, network diagnosis, advisor changes and deployment/recovery workflows.

1. **Terminal-Bench, pinned current version.** Best broad match for terminal,
   infrastructure, environment setup, multi-step execution and recovery. The
   official site currently identifies Terminal-Bench 4.0. Pin task manifest,
   harness, model, effort, tools and environment; compare resolution rate and
   successful-task resource use. It does not reproduce real credentials or
   production deployments.
2. **SWE-bench Pro**, plus multilingual repository tasks when relevant. Best
   match for changes inside existing Omnigent, Article Lab and BlockNations
   codebases. Preserve public dataset/version and exact agent configuration.
   SWE-bench Verified is a useful secondary baseline but its Python-heavy bug
   repair setting is narrower than the current TS/React/Unity work.
3. **DRBench** for research, analysis and source-backed reports. Treat citation,
   insight and factuality measures as a separate family rather than blending
   them into code resolution rate. Creative/editorial quality needs a personal
   rubric beyond this benchmark.
4. **WebArena-Verified** for browser workflows. It provides verified tasks and
   deterministic evaluation from answers/network evidence. It does not directly
   qualify native macOS control, iPhone behavior or the actual websites used.
5. **A small personal task suite** is the acceptance authority: reproducible
   examples drawn from real work with a clear deliverable, verifier and explicit
   human assessment where intent/taste matters. Public scores supply priors;
   successful delivery on this suite establishes local relevance.

Primary sources inspected 11 October 2026:
- https://www.tbench.ai/
- https://github.com/SWE-bench/SWE-bench
- https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/v2/README.md
- https://github.com/ServiceNow/drbench
- https://github.com/ServiceNow/webarena-verified

## Router shortlist

| Approach | Reason to evaluate | Limitation / admission condition |
| --- | --- | --- |
| Improve current Omnigent selector/control plane | Already owns session, harness, explicit pins, effort and dispatch | Must show gains against fixed-model baseline; remove untested capability assumptions |
| Reproducible deterministic baseline | Qualified candidate filter followed by one fixed strong model/effort or a small task-family rule | Quality-first baseline requires paired task evaluation, not historic popularity |
| Local feedback-assisted predictor | Can learn accepted failure/success patterns and abstain on uncertainty | Three usable Omnigent ratings are insufficient; historical chosen-model outcomes lack counterfactuals |
| Switchyard library / Stage routing | Tool activity, escalation and task routing; embedding option | Pre-1.0; standalone server documented as demo, not production; native session integration still belongs to Omnigent |
| vLLM Semantic Router / SAAR | Session memory, tool-loop locks, provider-state portability and replay | HTTP inference boundary, additional serving/classifier stack; does not itself control native CLI sessions |
| LiteLLM | Provider balancing, cooldowns, retries, budgets; beta auto/feedback routing | API gateway behavior is not native harness control or proof of task-completion gains |
| CLIProxyAPI / existing OmniRoute | API transport, compatible model access and provider failover | An execution adapter, not a validated quality selector; prove exact native tool/context/effort semantics |

Do not install another local service merely to get a model name. First compare
fixed quality-first baseline, current Advisor/Smart Routing and a small shadow
policy on identical tasks. Keep model/effort selection separate from harness and
provider/host; prototype safe next-turn/subagent adaptation only after the native
continuity contract is tested. Explicit model choices remain authoritative.

Current primary router sources:
- https://github.com/NVIDIA-NeMo/Switchyard
- https://github.com/NVIDIA-NeMo/Switchyard/blob/main/docs/routing_algorithms/stage_router_routing.md
- https://github.com/vllm-project/vllm-project.github.io/blob/main/_posts/2026-06-02-session-aware-agentic-routing.md
- https://github.com/vllm-project/semantic-router/blob/main/config/recipes/agent/README.md
- https://docs.litellm.ai/docs/routing
- https://docs.litellm.ai/docs/proxy/auto_routing
- https://github.com/router-for-me/CLIProxyAPI

## Concrete evaluation plan

1. Audit each source with stable response/turn/request IDs. Store human labels,
   machine evidence, proposed AI assessments and runtime status separately.
   Fold outcome/eligibility revisions and flag tests, reviewer/guardian chats,
   forks, repeated tasks and incomplete sessions. Link host/provider/harness
   attribution at the decision boundary, not from current settings.
2. Seed a representative retrospective queue across projects, periods, task
   families, successes, failures and interruptions. Sample before looking at
   outcomes; also maintain a deliberately enriched failure set for debugging.
   The enrichment set must not estimate overall success rate.
3. Manually review a stratified first batch and extract verifiers. A model can
   propose evidence-backed assessments from task/answer/typed execution evidence,
   with evidence references and abstention. Validate its agreement with human
   adjudication before enabling bulk automated reviews. The existing blind
   scoring boundary must stay separate from feedback discussion or supervision.
4. With sufficient coverage, evaluate public benchmark features and simple
   local features against outcome using whole task/root/fork-family groups and
   chronological holdouts. Retain model-release eras and harness configurations.
   Report grouped uncertainty and data coverage. Tiny samples and all-failure
   slices cannot support reliable AUC, benchmark weighting or causal model ranks.
5. Actual paired trials: same tasks and starting repos, isolated tool state,
   comparable quotas and fixed budgets; multiple repeats where stochasticity
   matters. Compare completed-task quality, completion without correction,
   time to useful completion, retries, tokens/cache, spending and quota evidence.
   Task-level success is primary, request-level cost is secondary. Never score
   untried arms from a historical rating or use an unlogged selection propensity.
6. Shadow routing: log input-feature version, qualified candidates, recommended
   model/effort/harness/provider/host, actual execution, uncertainty, explicit
   overrides, fallback, route latency and outcome join. No production switch.
7. Select using measured paired quality, then validate native continuation and
   overrides. Application changes follow fork merge, immutable candidate,
   Preview inspection and owner-approved O1 promotion. RTX availability is an
   actual current blocker to that phase, not to local research/review work.

## Local retrospective review tool

Run `python3 scripts/retrospective_review.py --port 3847`; open
http://127.0.0.1:3847/. The initial queue contains 737 Codex root chats, 23 ZCode
sessions and 12 Mac Omnigent sessions. Filter by project/title, select an assistant
checkpoint, inspect original requests and collapsible tool evidence, then save
Success/Partial/Failed/Not sure, task family, comment, labels, inclusion,
synthetic status and intervention. Human review revisions go to
`~/.local/share/omnigent-routing-research/human-reviews.jsonl`. Original data is
unchanged; this research utility is not yet integrated into production Omnigent.

The local research site is also installed as Mac LaunchAgent
`com.mortified2896.omnigent-task-review`, using an immutable copy of its committed
Python/HTML files under `~/.local/share/omnigent-routing-research/releases/`.
`site-release.json` records the exact commit and file hashes. It starts at login
and restarts if its process exits; serving the site does not run inference jobs.

Saved checkpoints carry a source ID, turn ID, digest and model/effort attribution
so later evaluation can detect changed trace targets. Test browser reviews use a
separate fixture database/output and are marked synthetic. The current tool
reloads saved assessments, retains revisions, distinguishes native Codex
`final_answer` from `commentary` phases and forcibly excludes source-marked tests.
Binary tool media stays in the original trace rather than becoming base64 prose.
The tool still needs further task segmentation, model-blind display and full
large-trace rendering improvements before extensive annotation.

Four initial assistant proposals are stored separately in
`~/.local/share/omnigent-routing-research/assistant-assessments.jsonl` and shown
at matching checkpoints, with buttons to jump to evidence. They assess the
waiting-lobby correction, Article Lab Storybook workflow, current-model Advisor
deployment and a ZCode Netflix PiP question. Proposed outcomes are two successes,
one partial and one not sure. These are illustrative, deliberately selected
examples with ordinal confidence, not a representative success rate or measured
model comparison. No genuine human review was created by this work. Human labels
were not input to those proposed judgments. Each proposal states its task scope
and inspected evidence; assistant proposals remain ineligible for outcome
training until adjudicated/calibrated.

Aggregate machine-readable inventories live under
`~/.local/share/omnigent-routing-research/`; no historical text belongs in Git.
The Codex/ZCode inventory is reproducible with
`python3 scripts/routing_collections_inventory.py`. The Omnigent audit is
`scripts/routing_evidence_audit.py`, with a
regression fixture for binary UUID joins, append-order revisions, eligibility,
not-sure missingness and copied-fork provenance.

## Verification of the research utilities

- Ruff formatting and checks on the three Python utilities and two focused test
  files; four regression checks cover audit joins/revisions, native answer phases,
  injected goal context, media rendering and unsafe ZCode WAL omission.
- Browser QA uses installed Chrome through the Playwright skill fallback. The
  Browser plugin is not installed. The site has been inspected at desktop
  1440×1000 and phone 390×844 sizes.
- A separate synthetic database/output verifies save, retained revisions,
  reload, and synthetic exclusion. No browser test writes to the genuine human
  review file. Live Omnigent and ZCode traces load through their adapters; a
  final Codex check verifies the assistant-proposal/evidence path.
- Local site binds `127.0.0.1:3847`. The tool is isolated from production routing
  and the original source databases. This milestone does not claim a selected
  router, full semantic review of all 3.54 GB, paired model trials or RTX rollout.
