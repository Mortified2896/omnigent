// Canonical Mac-local profile for the custom O3 routing-review desktop app.

"use strict";

const os = require("node:os");
const fs = require("node:fs");
const path = require("node:path");

const CANONICAL_LOCAL_PORT = 6768;
const CANONICAL_LOCAL_URL = `http://127.0.0.1:${CANONICAL_LOCAL_PORT}/`;
const CANONICAL_LOCAL_ORIGIN = CANONICAL_LOCAL_URL.replace(/\/$/, "");

function isLoopbackUrl(value) {
  try {
    const hostname = new URL(value).hostname;
    return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]";
  } catch {
    return false;
  }
}

/** Collapse every local spelling and port onto the one app-owned endpoint. */
function canonicalizeLocalUrl(value) {
  return isLoopbackUrl(value) ? CANONICAL_LOCAL_URL : value;
}

/**
 * Remove legacy local aliases from persisted desktop settings. Remote servers
 * remain available, but this custom app exposes exactly one local destination.
 */
function canonicalizeLocalSettings(settings) {
  const next = { ...settings };
  if (typeof next.server_url !== "string" || isLoopbackUrl(next.server_url)) {
    next.server_url = CANONICAL_LOCAL_URL;
  }

  const recents = Array.isArray(next.recent_servers) ? next.recent_servers : [];
  const remoteRecents = recents.filter(
    (value) => typeof value === "string" && !isLoopbackUrl(value),
  );
  next.recent_servers = [CANONICAL_LOCAL_URL, ...new Set(remoteRecents)];

  const allowed = Array.isArray(next.allowed_hosting_origins) ? next.allowed_hosting_origins : [];
  const remoteAllowed = allowed.filter(
    (value) => typeof value === "string" && !isLoopbackUrl(value),
  );
  next.allowed_hosting_origins = [CANONICAL_LOCAL_ORIGIN, ...new Set(remoteAllowed)];
  return next;
}

/** Apply the feature inputs inherited by `omnigent server --background`. */
function applyO3ServerEnvironment(env = process.env, homeDir = os.homedir()) {
  const legacyCatalogDir = path.join(
    homeDir,
    "GitHub",
    "omniroute-customizations",
    ".worktrees",
    "o3-noncodex-readiness",
    "catalogs",
    "o3",
  );
  const relocatedCatalogDir = path.join(
    homeDir,
    "GitHub",
    "_worktrees",
    "omniroute-customizations",
    "o3-noncodex-readiness",
    "catalogs",
    "o3",
  );
  const catalogDir = fs.existsSync(legacyCatalogDir)
    ? legacyCatalogDir
    : fs.existsSync(relocatedCatalogDir)
      ? relocatedCatalogDir
      : legacyCatalogDir;
  env.OMNIGENT_O3_ROUTING_REVIEW = "1";
  env.OMNIGENT_O3_OMNIROUTE_BASE_URL = "http://127.0.0.1:20128";
  env.OMNIGENT_O3_RECOMMENDATION_CATALOG_DIR = catalogDir;
  env.OMNIGENT_O3_BENCHMARK_REGISTRY = path.join(
    catalogDir,
    "published-benchmark-registry-v2.json",
  );
  env.OMNIGENT_O3_ADVISER_MODEL = "opencode/big-pickle";
  return env;
}

module.exports = {
  CANONICAL_LOCAL_PORT,
  CANONICAL_LOCAL_URL,
  CANONICAL_LOCAL_ORIGIN,
  isLoopbackUrl,
  canonicalizeLocalUrl,
  canonicalizeLocalSettings,
  applyO3ServerEnvironment,
};
