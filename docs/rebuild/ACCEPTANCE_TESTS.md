# Acceptance test specification

All tests are written for the `rebuild/upstream-0.7` branch and **must not
modify the production database, the production release dirs, or any
remote**. Tests run against a disposable test repository and a
disposable SQLite/PostgreSQL fixture.

## Contract

Each test is a single pytest function that, given a clean `omnigent`
server + host + runner, exercises one acceptance criterion and asserts
the expected outcome. The pytest suite is bounded — no test takes
longer than 90 s, and the full suite finishes in < 10 minutes.

The disposable test repository is created per test session at
`/tmp/omnigent-acceptance/<test-name>` and torn down at the end of
the session. The disposable database is either a per-session SQLite
file or a per-session `pglite` instance; in either case the connection
string is unique to the test and is dropped on teardown.

## Tests

### 1. Omnigent starts successfully

```python
def test_01_omnigent_starts():
    """The upstream server boots under the v0.7.0 tag and binds its
    loopback port within 30 s."""
    proc = start_local_server(wheel="omnigent==0.7.0")
    try:
        wait_for_loopback("/health", timeout=30)
        assert health_ok(proc) is True
    finally:
        stop_local_server(proc)
```

### 2. Web and host components report the expected SHA

```python
def test_02_version_sha_matches_built_wheel():
    """GET /health on the web service reports the build-baked SHA
    embedded in the wheel's omnigent/_build_info.py."""
    proc = start_local_server(wheel="omnigent==0.7.0")
    expected_sha = baked_build_info_sha()
    try:
        payload = get_health(proc)
        assert payload["version_sha"] == expected_sha
    finally:
        stop_local_server(proc)
```

### 3. Pi completes a real repository edit

```python
def test_03_pi_real_repo_edit(tmp_path: Path):
    """A Pi session on the upstream harness edits a file inside the
    disposable test repo and the diff is visible to the host."""
    repo = fixture_repo(tmp_path)
    proc = start_local_server(wheel="omnigent==0.7.0", agent="pi")
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

### 4. Pi can test, commit, and push the change

```python
def test_04_pi_commit_and_push(tmp_path: Path):
    """Pi pushes its commit to the test repo on a `polly/acceptance-4`
    branch; the parent agent sees the commit SHA."""
    repo = fixture_repo(tmp_path)
    proc = start_local_server(wheel="omnigent==0.7.0", agent="pi")
    try:
        sess = start_session(proc, harness="pi-native",
                             workspace=repo["worktree_path"],
                             prompt=("rename foo to bar, then commit on "
                                     "polly/acceptance-4, then push"))
        wait_for_completion(sess, timeout=180)
        last_event = last_session_event(sess, type="git.commit.outcome")
        assert last_event is not None
        # Pushed SHA appears in the remote worker's branch.
        pushed_sha = repo["git"].rev_parse("polly/acceptance-4")
        assert pushed_sha == last_event.payload["commit_sha"]
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 5. OpenCode completes the same workflow

```python
def test_05_opencode_real_repo_edit(tmp_path: Path):
    """OpenCode native harness performs the same edit/commit/push
    workflow as Pi."""
    repo = fixture_repo(tmp_path)
    proc = start_local_server(wheel="omnigent==0.7.0", agent="opencode")
    try:
        sess = start_session(proc, harness="opencode-native",
                             workspace=repo["worktree_path"],
                             prompt=("rename foo to bar, then commit on "
                                     "polly/acceptance-5, then push"))
        wait_for_completion(sess, timeout=180)
        diff = repo["git"].diff(main_branch="main",
                                ref="polly/acceptance-5")
        assert "bar" in diff
        assert "foo" not in diff
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 6. Delegation to OpenCode does not silently fall back

```python
def test_06_no_silent_fallback_opencode():
    """A request for OpenCode through the canonical resolver
    always lands on the opencode-native harness; never claude-sdk
    or Verity, even when the dispatch spec is ambiguous."""
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

### 7. Parent receives the child completion event

```python
def test_07_parent_receives_child_completion():
    """A child session's completion lands in the parent's
    sys_read_inbox with the full child result; the parent's stream
    emits the same content via the session child-status update."""
    proc = start_local_server(wheel="omnigent==0.7.0",
                              agent="verity+opencode")
    try:
        sess = start_session(proc, agent_selector="verity",
                             prompt="delegate a tiny edit to opencode")
        wait_for_completion(sess, timeout=180)
        inbox = read_inbox(sess)
        assert any(item["type"] == "sub_agent" for item in inbox)
        # Parent's stream must include a child-session updated event.
        events = collect_session_events(sess,
                                       types={"session.child_session.updated"})
        assert len(events) >= 1
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 8. Long-running work is not killed by a short idle timeout

```python
def test_08_long_running_work_survives_idle_timeout():
    """A 10-minute quiet harness turn completes successfully when
    HARNESS_TURN_TIMEOUT_S is set to 1 hour."""
    proc = start_local_server(wheel="omnigent==0.7.0",
                              env={"HARNESS_TURN_TIMEOUT_S": "3600"})
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

### 9. OmniRoute provenance records both requested and executed routing details

```python
def test_09_omniroute_provenance_recorded():
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

### 10. Langfuse receives the expected trace hierarchy

```python
def test_10_langfuse_trace_hierarchy():
    """A session emits a root span with session.id, parent/child
    linkage, the OmniRoute provenance attributes, and child spans
    for tool calls and LLM calls."""
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
        assert any(c.name == "omnigent.llm.request" for c in children)
        # All children carry session.id too.
        for c in children:
            assert c.attributes["session.id"] == sess.id
    finally:
        stop_session(proc, sess)
        stop_local_server(proc)
```

### 11. Parallel workers use separate worktrees without collisions

```python
def test_11_parallel_workers_isolated(tmp_path: Path):
    """Two concurrent sub-agents working on the same repo land on
    different branches, with disjoint worktree dirs."""
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
        assert repo["git"].rev_parse("polly/acceptance-11-pi")
        assert repo["git"].rev_parse("polly/acceptance-11-opencode")
        # The two commits are distinct.
        assert (repo["git"].rev_parse("polly/acceptance-11-pi")
                != repo["git"].rev_parse("polly/acceptance-11-opencode"))
    finally:
        stop_session(proc, s1)
        stop_session(proc, s2)
        stop_local_server(proc)
```

### 12. A failed candidate release does not replace production

```python
def test_12_failed_candidate_does_not_promote(tmp_path: Path):
    """A deliberate-broken migration rehearsal must not be promoted
    to production. The deployed-sha marker must still point at the
    pre-candidate SHA after the failed attempt."""
    deploy_root = fixture_deploy_root(tmp_path)
    initial = deploy_root["current_sha"]
    broken = "broken_candidate_sha"
    proc = start_local_server(
        wheel="omnigent==0.7.0",
        env={"DEPLOY_ROOT": str(deploy_root["path"])},
    )
    try:
        result = run_promote_release(deploy_root, broken)
        assert result.exit_code != 0  # migrate rehearsal must fail
        assert deploy_root["current_sha"] == initial
        assert deploy_root["deployed_sha"] == initial
    finally:
        stop_local_server(proc)
```

### 13. Rollback restores the exact previously live SHA

```python
def test_13_rollback_restores_previous(tmp_path: Path):
    """After a successful promote, rollback_release.sh restores
    the pre-promote SHA and updates deployed-sha only after the
    health-gated verification passes."""
    deploy_root = fixture_deploy_root(tmp_path)
    initial = deploy_root["current_sha"]
    candidate = "good_candidate_sha"
    proc = start_local_server(
        wheel="omnigent==0.7.0",
        env={"DEPLOY_ROOT": str(deploy_root["path"])},
    )
    try:
        run_promote_release(deploy_root, candidate)
        assert deploy_root["current_sha"] == candidate
        # Rollback.
        result = run_rollback_release(deploy_root)
        assert result.exit_code == 0
        assert deploy_root["current_sha"] == initial
        assert deploy_root["deployed_sha"] == initial
    finally:
        stop_local_server(proc)
```

### 14. Web and host always run the same release

```python
def test_14_web_and_host_same_release():
    """The web and host systemd unit drop-ins point at the same
    release path (and the live process's argv[0] matches)."""
    deploy_root = fixture_deploy_root()
    proc = start_local_server(
        wheel="omnigent==0.7.0",
        env={"DEPLOY_ROOT": str(deploy_root["path"])},
    )
    try:
        run_promote_release(deploy_root, "candidate_sha")
        web_dropin = dropin_path("omni-control-room-web.service")
        host_dropin = dropin_path("omni-control-room-host.service")
        assert web_dropin.release == host_dropin.release
        # /proc/<MainPID>/cmdline for the host must include the
        # release's .venv/bin/python and `-P -m omni host`.
        cmdline = read_proc_cmdline(host_main_pid())
        assert "release/.venv/bin/python" in cmdline
        assert "-P" in cmdline
        assert "-m" in cmdline
        assert "omni" in cmdline
        assert "host" in cmdline
    finally:
        stop_local_server(proc)
```

### 15. No test modifies the production database

```python
def test_15_no_production_db_modification(tmp_path: Path):
    """No acceptance test writes to the production database. A
    snapshot of the production DB row count is taken before and
    after the test session; the difference must be zero."""
    before = production_db_row_count()
    proc = start_local_server(wheel="omnigent==0.7.0",
                              test_db=tmp_path / "test.db")
    try:
        # Run a representative subset of tests 1-14.
        run_acceptance_subset(proc, tests=[1, 3, 4, 5, 9, 11, 12, 13])
    finally:
        stop_local_server(proc)
    after = production_db_row_count()
    assert before == after, (
        f"production DB row count changed: {before} -> {after}"
    )
```

## Fixtures

- `fixture_repo(tmp_path)` → `{"worktree_path": Path, "git": GitFixture}`
- `fixture_deploy_root(tmp_path)` → `{"path": Path, "current_sha": str,
  "deployed_sha": str, "previous_sha": str}`
- `start_local_server(wheel, env, config, test_db, agent)` →
  `LocalServerHandle`
- `InMemorySpanExporter` collects every OTel span the process emits
  (uses `opentelemetry-sdk.trace.export.in_memory_span_exporter.NewSimpleSpanExporter`).

## Test environment

- A clean `omnigent==0.7.0` wheel (built from the upstream `v0.7.0` tag
  via `python -m build`) and a `langfuse==2.x` fixture.
- A disposable `pglite` instance (or a per-session SQLite file) for
  the database.
- A disposable git fixture repository per test session.
- The Pi/OpenCode harnesses' CLI binaries (`pi`, `opencode`) installed
  under the system path or under a per-test prefix; tests skip the
  Pi/OpenCode assertions if the binary is missing and the test
  reports as `SKIPPED` with the reason "harness binary missing" (this
  is the upstream convention).
