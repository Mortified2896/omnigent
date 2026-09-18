import assert from "node:assert/strict";
import {pathToFileURL} from "node:url";
import {join} from "node:path";
import {canaryEligibility} from "./canary-profile.mjs";
const {auditTaskDefinition}=await import(pathToFileURL(join(process.env.SELF_BENCH_SOURCE,"src/audit.ts")));
const definition={difficulty:"easy",workdir:".",testPaths:["test_arithmetic.py"],passToPass:[],
 failToPass:["test_arithmetic.py"],testCommand:"python3 -m unittest {tests}",testSelection:{mode:"base-only"}};
const patch="diff --git a/arithmetic.py b/arithmetic.py\n--- a/arithmetic.py\n+++ b/arithmetic.py\n@@ -1 +1 @@\n-return a - b\n+return a + b\n";
const easy=auditTaskDefinition(definition,patch,"");
assert.equal(easy.accepted,false);
const evidence={baseExit:1,oracleExit:0,assertionFailure:true,oraclePassed:true};
assert.equal(canaryEligibility(easy,evidence).accepted,true);
assert.equal(canaryEligibility(easy,{...evidence,baseExit:0}).accepted,false);
assert.equal(canaryEligibility(easy,{...evidence,oracleExit:1}).accepted,false);
assert.equal(canaryEligibility(easy,{...evidence,assertionFailure:false}).accepted,false);
for(const [difficulty,lines] of [["easy",20],["medium",50],["hard",100]]) {
 const report=auditTaskDefinition({...definition,difficulty},patch,"");
 assert(report.blockers.some(b=>b.includes("at least "+lines+" changed implementation lines")));
}
assert.equal(canaryEligibility({...easy,blockers:[...easy.blockers,"held-out test error"]},evidence).accepted,false);
console.log("canary eligibility: tiny passes; easy fails; 20/50/100 unchanged; fail-to-pass mandatory");
