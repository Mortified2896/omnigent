/** Integration validation only; never a benchmark difficulty profile. */
export function canaryEligibility(standardEasyAudit, evidence) {
  const blockers = standardEasyAudit.blockers.filter(message =>
    !/^easy mode requires at least 20 changed implementation lines; found \d+$/.test(message));
  if (standardEasyAudit.metrics.implementationFiles < 1 ||
      standardEasyAudit.metrics.implementationChangedLines < 2)
    blockers.push("canary requires one changed implementation line (two diff lines)");
  if (!evidence || evidence.baseExit !== 1 || evidence.oracleExit !== 0 ||
      evidence.assertionFailure !== true || evidence.oraclePassed !== true)
    blockers.push("canary requires observed base assertion failure and oracle success");
  return {profile:"canary",purpose:"pipeline integration only",benchmarkEligible:false,
    benchmarkDifficulty:null,accepted:blockers.length===0,blockers,
    metrics:standardEasyAudit.metrics};
}
