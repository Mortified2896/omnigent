const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  CANONICAL_LOCAL_URL,
  canonicalizeLocalUrl,
  canonicalizeLocalSettings,
  applyO3ServerEnvironment,
} = require("../src/o3_local_profile");

describe("O3 local desktop profile", () => {
  it("collapses every loopback spelling and port onto 127.0.0.1:6768", () => {
    assert.equal(canonicalizeLocalUrl("http://localhost:6767/"), CANONICAL_LOCAL_URL);
    assert.equal(canonicalizeLocalUrl("http://127.0.0.1:49763/"), CANONICAL_LOCAL_URL);
    assert.equal(canonicalizeLocalUrl("http://[::1]:8000/"), CANONICAL_LOCAL_URL);
    assert.equal(canonicalizeLocalUrl("https://remote.example/"), "https://remote.example/");
  });

  it("removes duplicate legacy locals while preserving remote servers", () => {
    const result = canonicalizeLocalSettings({
      server_url: "http://localhost:6767/",
      recent_servers: [
        "http://127.0.0.1:6767/",
        "http://localhost:6768/",
        "https://remote.example/",
      ],
      allowed_hosting_origins: ["http://localhost:6767", "https://remote.example"],
    });
    assert.equal(result.server_url, CANONICAL_LOCAL_URL);
    assert.deepEqual(result.recent_servers, [CANONICAL_LOCAL_URL, "https://remote.example/"]);
    assert.deepEqual(result.allowed_hosting_origins, [
      "http://127.0.0.1:6768",
      "https://remote.example",
    ]);
  });

  it("applies the complete O3 server feature profile", () => {
    const env = {};
    applyO3ServerEnvironment(env, "/Users/test");
    assert.equal(env.OMNIGENT_O3_ROUTING_REVIEW, "1");
    assert.equal(env.OMNIGENT_O3_OMNIROUTE_BASE_URL, "http://127.0.0.1:20128");
    assert.match(env.OMNIGENT_O3_RECOMMENDATION_CATALOG_DIR, /o3-noncodex-readiness/);
    assert.match(env.OMNIGENT_O3_BENCHMARK_REGISTRY, /published-benchmark-registry-v2\.json$/);
    assert.equal(env.OMNIGENT_O3_ADVISER_MODEL, "opencode/big-pickle");
  });
});

it("uses the relocated catalogue when the old worktree no longer exists", () => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "o3-profile-"));
  try {
    const catalogue = path.join(
      home,
      "GitHub",
      "_worktrees",
      "omniroute-customizations",
      "o3-noncodex-readiness",
      "catalogs",
      "o3",
    );
    fs.mkdirSync(catalogue, { recursive: true });
    const env = {};
    applyO3ServerEnvironment(env, home);
    assert.equal(env.OMNIGENT_O3_RECOMMENDATION_CATALOG_DIR, catalogue);
    assert.equal(
      env.OMNIGENT_O3_BENCHMARK_REGISTRY,
      path.join(catalogue, "published-benchmark-registry-v2.json"),
    );
  } finally {
    fs.rmSync(home, { recursive: true });
  }
});
