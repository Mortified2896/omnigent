# Acceptance test specification — fresh 0.7 database mode

> **Fresh-DB mode.** The cutover does **not** migrate the existing
> production `chat.db` into upstream v0.7. The existing database
> state is moved aside to a read-only backup, and a fresh upstream
> 0.7 database is initialized at the same configured database URI
> on first boot. **No tests in this suite import, snapshot, or
> assert against the legacy database.** The only persistent state
> the tests interact with is the fresh database the new wheel
> creates itself.

All tests are written for the `rebuild/upstream-0.7` branch and
**must not modify the production database, the production release
dirs, or any remote**. Tests run against the live URL after the
in-place cutover, against a disposable test repository, and against
the **fresh** upstream 0.7 database the wheel itself initialized
during boot.

## Contract

Each test is a single pytest function that, given the freshly
booted `omnigent` server + host + runner, exercises one
acceptance criterion and asserts the expected outcome. The pytest
suite is bounded — no test takes longer than 90 s, and the full
suite finishes in < 10 minutes.

The disposable test repository is created per test session at
`/tmp/omnigent-acceptance/<test-name>` and torn down at the end
of the session. **No disposable database fixture is created by
the test suite** — the suite uses the server's own fresh database
(every test creates its own session, agent, and harness worktree
via the live API and tears them down at the end of the session).

## Tests

### 1. Database initializes successfully on first boot

```python
def test_01_db_initializes_on_first_boot():
    """A brand-new empty `<data_dir>/chat.db` is created and
    brought to Alembic head by the new wheel's first engine
    connection (`get_or_create_engine` →
    `_initialize_or_verify_schema` →
    `alembic.command.upgrade('head')`)."""
    # Boot against an empty data dir; no pre-existing chat.db.
    proc = start_local_server(wheel="omnigent==0.7.0",
                              env={"OMNIGENT_DATA_DIR": empty_tmpdir})
    try:
        wait_for_loopback("/health", timeout=45)
        # The journalctl-equivalent log captures
        # "Running database migrations…".
        assert "Running database migrations" in proc.boot_log
        assert "schema is out of date" not in proc.boot_log
        # The fresh DB has tables.
        with sqlite3.connect(proc.db_path) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table'"
                )
            }
        assert "alembic_version" in tables
        # Upstream tables are present.
        for required in ("agents", "conversations", "comments",
                         "files", "policies", "permissions",
                         "scheduled_tasks", "projects",
                         "inbox", "tasks"):
            assert required in tables, (
                f"expected upstream table {required!r} in fresh "
                f"0.7 schema; got {sorted(tables)}"
            )
        # The alembic_version row is at the upstream v0.7 head.
        with sqlite3.connect(proc.db_path) as conn:
            head = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        assert head == "zf1a2b3c4d5e"  # upstream v0.7 head
    finally:
        stop_local_server(proc)
```

### 2. Web and host components report the expected SHA

```python
def test_02_version_sha_matches_built_wheel():
    """GET /health on the web service reports the build-baked SHA
    embedded in the wheel's omnigent/_build_info.py."""
    proc = start_local_server(wheel="omnigent==0.7.0")
    try:
        payload = get_health(proc)
        assert payload["version_sha"] == baked_build_info_sha()
    finally:
        stop_local_server(proc)
```

### 3. Web UI loads

```python
def test_03_web_ui_loads():
    """The SPA bundle is served at `/` and returns 200 with
    `text/html`."""
    proc = start_local_server(wheel="omnigent==0.7.0")
    try:
        resp = proc.url_session.get("/", timeout=30)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert b"<div id=\"root\"></div>" in resp.content
        # The SPA entry script is referenced.
        assert b"/src/main.tsx" in resp.content
    finally:
        stop_local_server(proc)
```

### 4. A new session can be created

```python
def test_04_new_session_creates():
    """`POST /v1/sessions` against the fresh DB returns 201 with
    a session id; a subsequent `GET /v1/sessions/<id>` returns
    the session state."""
    proc = start_local_server(wheel="omnigent==0.7.0")
    try:
        resp = proc.url_session.post(
            "/v1/sessions",
            json={"agent": "verity+opencode",
                  "prompt": "trivial echo"},
            timeout=30,
        )
        assert resp.status_code == 201
        session_id = resp.json()["id"]
        assert session_id.startswith("conv_")
        # Read it back.
        resp = proc.url_session.get(
            f"/v1/sessions/{session_id}", timeout=30)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == session_id
        assert body["status"] in ("created", "running")
    finally:
        stop_session(proc, session_id)
        stop_local_server(proc)
```

### 5. Pi completes a real repository edit

```python
def test_05_pi_real_repo_edit(tmp_path: Path):
    """A Pi session on the upstream harness edits a file inside
    the disposable test repo and the diff is visible to the host."""
    repo = fixture_repo(tmp_path)
    proc = start_local_server(wheel="omnigent==0.7.0",
                              env={"OMNIGENT_PI_PATH": pi_binary()})
    try:
        sess = start_session(proc, harness="pi-native",
                             workspace=repo["worktree_path"],
                             prompt="rename the function foo to bar")
        wait_for_completion(sess, timeout=120)
        diff = repo["git"].diff(main_branch="main")
        assert "bar" in diff
        assert "foo" not in diff
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 6. Pi can test, commit, and push the change

```python
def test_06_pi_commit_and_push(tmp_path: Path):
    """Pi pushes its commit to the test repo on a
    `polly/acceptance-6` branch; the parent agent sees the
    commit SHA."""
    repo = fixture_repo(tmp_path)
    proc = start_local_server(wheel="omnigent==0.7.0",
                              env={"OMNIGENT_PI_PATH": pi_binary()})
    try:
        sess = start_session(proc, harness="pi-native",
                             workspace=repo["worktree_path"],
                             prompt=("rename foo to bar, then commit "
                                     "on polly/acceptance-6, then "
                                     "push"))
        wait_for_completion(sess, timeout=180)
        last_event = last_session_event(
            sess, type="git.commit.outcome")
        assert last_event is not None
        # Pushed SHA appears in the remote worker's branch.
        pushed_sha = repo["git"].rev_parse("polly/acceptance-6")
        assert pushed_sha == last_event.payload["commit_sha"]
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 7. OpenCode completes the same workflow

```python
def test_07_opencode_real_repo_edit(tmp_path: Path):
    """OpenCode native harness performs the same edit / commit /
    push workflow as Pi."""
    repo = fixture_repo(tmp_path)
    proc = start_local_server(
        wheel="omnigent==0.7.0",
        env={"OMNIGENT_OPENCODE_PATH": opencode_binary()},
    )
    try:
        sess = start_session(proc, harness="opencode-native",
                             workspace=repo["worktree_path"],
                             prompt=("rename foo to bar, then commit "
                                     "on polly/acceptance-7, then "
                                     "push"))
        wait_for_completion(sess, timeout=180)
        diff = repo["git"].diff(main_branch="main",
                                ref="polly/acceptance-7")
        assert "bar" in diff
        assert "foo" not in diff
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 8. Delegation to OpenCode does not silently fall back

```python
def test_08_no_silent_fallback_opencode():
    """A request for OpenCode through the canonical resolver
    always lands on the opencode-native harness; never
    claude-sdk or Verity, even when the dispatch spec is
    ambiguous."""
    proc = start_local_server(wheel="omnigent==0.7.0",
                              agent="verity+opencode")
    try:
        sess = start_session(proc, agent_selector="opencode",
                             prompt="trivial echo")
        events = collect_session_events(sess, types={
            "session.created", "session.harness",
        })
        harness = next(e for e in events
                       if e["type"] == "session.harness")
        assert harness["payload"]["harness"] == "opencode-native"
        assert "claude-sdk" not in events_text(events)
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 9. Parent receives the child completion event

```python
def test_09_parent_receives_child_completion():
    """A child session's completion lands in the parent's
    sys_read_inbox with the full child result; the parent's
    stream emits the same content via the session child-status
    update."""
    proc = start_local_server(wheel="omnigent==0.7.0",
                              agent="verity+opencode")
    try:
        sess = start_session(proc, agent_selector="verity",
                             prompt="delegate a tiny edit to opencode")
        wait_for_completion(sess, timeout=180)
        inbox = read_inbox(sess)
        assert any(item["type"] == "sub_agent" for item in inbox)
        # Parent's stream must include a child-session updated
        # event.
        events = collect_session_events(
            sess,
            types={"session.child_session.updated"},
        )
        assert len(events) >= 1
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 10. Long-running work is not killed by a short idle timeout

```python
def test_10_long_running_work_survives_idle_timeout():
    """A 10-minute quiet harness turn completes successfully when
    OMNIGENT_HARNESS_IDLE_TIMEOUT_S is set to 1 hour."""
    proc = start_local_server(
        wheel="omnigent==0.7.0",
        env={"OMNIGENT_HARNESS_IDLE_TIMEOUT_S": "3600"},
    )
    try:
        sess = start_session(proc, harness="pi-native",
                             prompt="long-running quiet task")
        # Wait while keeping the session idle (no user messages).
        time.sleep(10 * 60)
        # Session must still be alive, not killed by the watchdog.
        status = get_session_status(proc, sess)
        assert status in ("running", "idle")
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 11. OmniRoute provenance records both requested and executed routing details

```python
def test_11_omniroute_provenance_recorded():
    """A request to OmniRoute's v0.7 series sets both
    omniroute.requested.model and omniroute.executed.model on the
    Langfuse span; verified is True when the response names a
    distinct provider+model."""
    proc = start_local_server(
        wheel="omnigent==0.7.0",
        config="tests/fixtures/omniroute_provider.yaml",
    )
    spans = InMemorySpanExporter().collect()
    try:
        sess = start_session(proc, harness="claude-sdk",
                             prompt="trivial echo")
        wait_for_completion(sess, timeout=60)
        root = spans.find_root(session_id=sess.id)
        assert root.attributes["omniroute.requested.model"]
        assert root.attributes["omniroute.executed.provider"]
        assert root.attributes["omniroute.executed.model"]
        assert root.attributes["omniroute.decision.id"]
        assert root.attributes["omniroute.route_approval.state"] in (
            "approved", "default"
        )
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 12. Langfuse receives the expected trace hierarchy

```python
def test_12_langfuse_trace_hierarchy():
    """A session emits a root span with session.id, parent / child
    linkage, the OmniRoute provenance attributes, and child
    spans for tool calls and LLM calls."""
    proc = start_local_server(
        wheel="omnigent==0.7.0",
        config="tests/fixtures/omniroute_provider.yaml",
        env={"OMNIGENT_TELEMETRY_ENABLED": "true"},
    )
    spans = InMemorySpanExporter().collect()
    try:
        sess = start_session(proc, harness="claude-sdk",
                             prompt="delegate a tiny task")
        wait_for_completion(sess, timeout=180)
        root = spans.find_root(session_id=sess.id)
        assert root.attributes["session.id"] == sess.id
        children = spans.children(root)
        assert any(c.name == "omnigent.tool" for c in children)
        assert any(c.name == "omnigent.llm.request"
                   for c in children)
        # All children carry session.id too.
        for c in children:
            assert c.attributes["session.id"] == sess.id
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 13. Parallel workers use separate worktrees without collisions

```python
def test_13_parallel_workers_isolated(tmp_path: Path):
    """Two concurrent sub-agents working on the same repo land
    on different branches, with disjoint worktree dirs."""
    repo = fixture_repo(tmp_path)
    proc = start_local_server(wheel="omnigent==0.7.0",
                              agent="verity+pi+opencode")
    try:
        # Two parallel children: one to pi, one to opencode-native.
        s1 = start_session(proc, agent_selector="pi",
                           prompt="rename foo to bar",
                           workspace=repo["worktree_path"])
        s2 = start_session(proc, agent_selector="opencode",
                           prompt="rename foo to baz",
                           workspace=repo["worktree_path"])
        wait_for_completion(s1, timeout=180)
        wait_for_completion(s2, timeout=180)
        # Both children committed on their own branch.
        assert repo["git"].rev_parse("polly/acceptance-13-pi")
        assert repo["git"].rev_parse(
            "polly/acceptance-13-opencode")
        # The two commits are distinct.
        assert (repo["git"].rev_parse("polly/acceptance-13-pi")
                != repo["git"].rev_parse(
                    "polly/acceptance-13-opencode"))
    finally:
        stop_session(proc, s1)
        stop_session(proc, s2)
        stop_local_server(proc)
```

### 14. Mobile access works from the existing URL

```python
def test_14_mobile_access_works_from_existing_url():
    """The same SPA bundle that loads on the laptop loads on the
    phone URL; auth header injection succeeds; a phone-realistic
    viewport / user-agent gets a 200 from `/` and a 200 from
    `/api/whoami` (header auth)."""
    proc = start_local_server(wheel="omnigent==0.7.0",
                              env={"OMNIGENT_AUTH_ENABLED": "1",
                                   "OMNIGENT_AUTH_PROVIDER":
                                       "header",
                                   "OMNIGENT_AUTH_HEADER":
                                       "X-Forwarded-Email"})
    try:
        # The phone's user-agent is in the iOS Safari family.
        phone_ua = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 "
            "Mobile/22E240 Safari/604.1"
        )
        # `/` serves the SPA bundle.
        resp = proc.url_session.get(
            "/",
            headers={"User-Agent": phone_ua,
                     "X-Forwarded-Email": "<operator-identity>"},
            timeout=30,
        )
        assert resp.status_code == 200
        # `/api/whoami` returns the identity.
        resp = proc.url_session.get(
            "/api/whoami",
            headers={"User-Agent": phone_ua,
                     "X-Forwarded-Email": "<operator-identity>"},
            timeout=30,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["email"] == "<operator-identity>"
        assert body["is_admin"] is True
    finally:
        stop_local_server(proc)
```

### 15. No test imports legacy database state

```python
def test_15_no_test_imports_legacy_state():
    """A grep over the rebuilt codebase asserts no test or
    script references the legacy fork-lineage Alembic revisions
    (`ze1b2c3d4e5f`, `zg1a2b3c4d5e`, `zd1b2c3d4e5f`, the
    fork-specific migrations `ze1b2c3d4e5f_add_issue_runs_table`,
    `zg1a2b3c4d5e_repair_conversations_kind`, etc.). The legacy
    state is moved aside to a read-only backup; it is never
    imported or migrated."""
    # Stat the search results; do not actually execute migrations.
    offenders = grep_for_legacy_revision_references(
        root="omnigent/", tests_root="tests/",
    )
    assert not offenders, (
        f"unexpected legacy references: {sorted(offenders)}"
    )
```

## Removed tests (vs. the previous 15-test spec)

The following tests from the prior spec were removed because they
were **deployment-tooling tests**, not **fresh-0.7 acceptance
tests**. They exercised the fork's `promote_release.sh` /
`rollback_release.sh` / supervisor canary machinery, which the
rebuild does not need. Their contract is now covered by the
fresh-0.7 acceptance tests + the Phase D pre-cutover canary on
the existing Omnigent VM (NOT on a disposable VM and NOT on
another host — see `PHASED_IMPLEMENTATION_PLAN.md` §D for the
isolation rules that keep Phase D off the live production
service).

- **test_12** "A failed candidate release does not replace
  production" — was a fork deployment test; superseded by the
  cutover script's `cutover.sh --confirm` gate and the Phase D
  pre-cutover canary on the existing Omnigent VM (see
  `PHASED_IMPLEMENTATION_PLAN.md` §D).
- **test_13** "Rollback restores the exact previously live SHA" —
  was a fork deployment test; the rebuild's Phase G rollback
  re-runs the new wheel against a fresh DB, not the fork wheel.
- **test_14** "Web and host always run the same release" — was a
  fork deployment test; the rebuild's C.4 explicitly rewrites the
  single systemd unit so web and host are the same process.

The **test_15** "No test modifies the production database" was
rewritten as **test_15** "No test imports legacy database state"
above, because the fresh-DB cutover means there is no legacy DB
to protect — the legacy DB is moved aside as a read-only backup,
and the new DB is empty when the suite starts.

> **Phase D relationship.** Phase D runs the rebuild wheel on the
> **existing Omnigent VM** (NOT a disposable VM, NOT another host)
> under the isolation rules in `PHASED_IMPLEMENTATION_PLAN.md`
> §D.0a (what Phase D uses) + §D.0b (what Phase D does NOT do): unused temp loopback port, temp `OMNIGENT_DATA_DIR`, fresh
> temp SQLite, temp artifact + harness dirs, temp configs,
> disposable Git branches (or a dedicated disposable GitHub test
> repo). Phase D does NOT touch the production database, the live
> systemd unit, the production port, or the reverse proxy. The
> acceptance tests in this file are the contract Phase D proves
> against that canary deployment; the per-check scripts under
> `deploy/rebuild/tests/checks/` are the implementations.

## Fixtures

- `fixture_repo(tmp_path)` →
  `{"worktree_path": Path, "git": GitFixture}`
- `start_local_server(wheel, env, config, agent)` →
  `LocalServerHandle` with `.url_session`, `.db_path`,
  `.boot_log`, `.proc`
- `InMemorySpanExporter` collects every OTel span the process
  emits (uses
  `opentelemetry-sdk.trace.export.in_memory_span_exporter.NewSimpleSpanExporter`).

## Test environment

- A clean `omnigent==0.7.0` wheel (built from the upstream
  `v0.7.0` tag via `python -m build`) and a `langfuse==2.x`
  fixture.
- **No disposable database fixture.** Each `start_local_server`
  invocation points `OMNIGENT_DATA_DIR` at a fresh empty
  directory; the wheel's first-boot migration path creates the
  schema.
- A disposable git fixture repository per test session.
- The Pi / OpenCode harnesses' CLI binaries (`pi`, `opencode`)
  installed under the system path or under a per-test prefix;
  tests skip the Pi / OpenCode assertions if the binary is
  missing and the test reports as `SKIPPED` with the reason
  "harness binary missing" (this is the upstream convention).
- The OmniRoute fixture is a tiny in-process FastAPI server that
  replies to `/routes:select` with a deterministic
  provider+model pairing; no external network is required.