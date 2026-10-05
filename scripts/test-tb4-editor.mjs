// Dependency-free tests for the pure editor helpers, not browser acceptance.
// Node >=22.16: node --experimental-strip-types --test scripts/test-tb4-editor.mjs
import assert from "node:assert/strict";
import test from "node:test";
import {
  TB4_POOL_MODES, parseTB4Floor, copyConcreteDefaults, setTB4Pool,
  setAllowedPaidCandidates, validateTB4Draft, advisorUsageNotice,
} from "../web/src/model-advisor/tb4Editor.ts";

const legacy = {
  schema_version: 1, enabled: true, allowed_candidate_ids: ["paid"],
  advisor_candidate_id: "advisor", human_probability_percent: 50,
};
const options = [
  { candidate_id: "paid", access_class: "chatgpt_plan", available: true, label: "Paid model / high" },
  { candidate_id: "advisor", access_class: "glm_plan", available: true, label: "Advisor / high" },
  { candidate_id: "free", access_class: "free", available: true, label: "Free connection" },
  { candidate_id: "unknown", access_class: "unknown", available: true, label: "Unknown billing" },
];

test("the two execution modes are explicit", () => {
  assert.deepEqual(TB4_POOL_MODES.map((mode) => mode.label), ["Paid only", "Free only"]);
});

test("literal floors preserve precision and exact boundaries", () => {
  for (const [input, expected] of [["0", 0], [".5", 0.5], ["40.00001", 40.00001], ["100", 100]]) {
    assert.equal(parseTB4Floor(input), expected);
  }
});

test("blank, malformed and nonfinite floors never turn into a default", () => {
  for (const input of ["", " ", "NaN", "Infinity", "-1", "101", "40garbage", "0x28", "4e1"]) {
    assert.throws(() => parseTB4Floor(input));
  }
});

test("opt-in conversion copies the original allowlist and advisor", () => {
  const draft = copyConcreteDefaults(legacy);
  assert.equal(draft.pool_mode, "paid_only");
  assert.deepEqual(draft.allowed_paid_candidate_ids, ["paid"]);
  assert.notEqual(draft.allowed_paid_candidate_ids, legacy.allowed_candidate_ids);
  assert.equal(draft.advisor_candidate_id, "advisor");
  assert.equal(draft.human_probability_percent, 50);
  assert.deepEqual(validateTB4Draft(draft, options), []);
});

test("mode roundtrip preserves paid picks and the separate advisor", () => {
  const draft = copyConcreteDefaults(legacy);
  const free = setTB4Pool(draft, "free_only");
  assert.deepEqual(setTB4Pool(free, "paid_only"), draft);
  assert.deepEqual(validateTB4Draft(free, options), []);
  assert.match(advisorUsageNotice(free, options), /paid-plan allowance/);
  assert.equal(advisorUsageNotice(draft, options), null);
});

test("stale paid choices stay invalid rather than selecting the first option", () => {
  const draft = setAllowedPaidCandidates(copyConcreteDefaults(legacy), ["gone"]);
  assert.ok(validateTB4Draft(draft, options).some((message) => message.includes("gone")));
  assert.deepEqual(draft.allowed_paid_candidate_ids, ["gone"]);
});

test("unknown routes do not rescue an empty free pool", () => {
  const draft = setTB4Pool(copyConcreteDefaults(legacy), "free_only");
  const noFree = options.filter((option) => option.candidate_id !== "free");
  assert.ok(validateTB4Draft(draft, noFree).some((message) => message.includes("Paid fallback is disabled")));
});

test("free routes cannot be selected as a paid-plan candidate", () => {
  const draft = setAllowedPaidCandidates(copyConcreteDefaults(legacy), ["free"]);
  assert.ok(validateTB4Draft(draft, options).length > 0);
});

test("missing advisor and invalid balance are exposed", () => {
  const draft = { ...copyConcreteDefaults(legacy), advisor_candidate_id: "gone", human_probability_percent: 101 };
  assert.equal(validateTB4Draft(draft, options).length, 2);
});

test("duplicate catalog IDs or paid choices are rejected", () => {
  const draft = copyConcreteDefaults(legacy);
  assert.ok(validateTB4Draft(draft, [...options, options[0]]).length > 0);
  assert.ok(validateTB4Draft(setAllowedPaidCandidates(draft, ["paid", "paid"]), options).length > 0);
});
