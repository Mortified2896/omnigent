/** Build a local integration task using pinned upstream SelfBench components. */
import {readFileSync,writeFileSync,mkdirSync,existsSync,copyFileSync,chmodSync} from "node:fs";
import {join,resolve} from "node:path";
import {pathToFileURL} from "node:url";
import {execFileSync,spawnSync} from "node:child_process";
import {createHash} from "node:crypto";
import {canaryEligibility} from "./canary-profile.mjs";

const [captureArg, outputArg, image] = process.argv.slice(2);
if (!captureArg || !outputArg || !image) throw new Error("capture output pinned-image required");
const source=process.env.SELF_BENCH_SOURCE;
if (!source) throw new Error("SELF_BENCH_SOURCE required");
const pinned="8376c776c29006fe56a01f3ac882f3488db8b6dc";
if(execFileSync("git",["-C",source,"rev-parse","HEAD"],{encoding:"utf8"}).trim()!==pinned)
 throw new Error("SelfBench source revision mismatch");
const upstream=async name=>import(pathToFileURL(join(source,"src",name)).href);
const {auditTaskDefinition}=await upstream("audit.ts");
const {extractProvenanceMessages}=await upstream("provenance/session.ts");
const render=await upstream("harbor-task/render.ts");
const {testScript,solutionScript}=await upstream("harbor-task/verifier.ts");
const {verifierRuntimeFiles}=await upstream("harbor-task/runtime-assets.ts");
const {assertEnvironmentPolicy,assertEnvironmentEvidence}=await upstream("environment.ts");
const capture=resolve(captureArg), output=resolve(outputArg);
if(existsSync(output)) throw new Error("output must not exist");
const manifest=JSON.parse(readFileSync(join(capture,"manifest.json"),"utf8"));
if(manifest.terminal_state!=="completed" || manifest.errors.length) throw new Error("successful capture required");
if(!manifest.start || !manifest.end) throw new Error("both snapshots required");
mkdirSync(output,{recursive:true,mode:0o700});
const git=(dir,...args)=>execFileSync("git",["-C",dir,...args],{encoding:"utf8",stdio:["ignore","pipe","pipe"]});
function reconstruct(stage) {
 const dest=join(output,stage);
 execFileSync("git",["clone","--no-hardlinks",manifest.repo_root,dest],{stdio:"pipe"});
 git(dest,"checkout","--detach",manifest[stage].head);
 for(const key of ["staged_patch","unstaged_patch","untracked_archive"]) {
  const artifact=manifest[stage].artifacts[key], file=join(capture,stage,artifact.path);
  const bytes=readFileSync(file);
  if(createHash("sha256").update(bytes).digest("hex")!==artifact.sha256) throw new Error("digest mismatch");
  if(key==="untracked_archive") {
   execFileSync("python3",["-c","import sys,tarfile; t=tarfile.open(sys.argv[1]); t.extractall(sys.argv[2],filter='data')",file,dest]);
  } else if(bytes.length) git(dest,"apply","--binary",file);
 }
 return dest;
}
const base=reconstruct("start"), oracle=reconstruct("end");
const run=dir=>spawnSync("python3",["-m","unittest","-v","test_arithmetic.py"],{cwd:dir,encoding:"utf8"});
const baseTest=run(base),oracleTest=run(oracle);
writeFileSync(join(output,"base-test.txt"),baseTest.stdout+baseTest.stderr);
writeFileSync(join(output,"oracle-test.txt"),oracleTest.stdout+oracleTest.stderr);
const evidence={baseExit:baseTest.status,oracleExit:oracleTest.status,
 assertionFailure:/FAILED \(failures=\d+\)/.test(baseTest.stderr),oraclePassed:/\bOK\b/.test(oracleTest.stderr)};
const native=join(capture,"trajectory",manifest.native_rollout.artifact.path);
const messages=extractProvenanceMessages(readFileSync(native,"utf8"),"codex");
const input=JSON.parse(readFileSync(join(capture,"input.json"),"utf8"));
const prompt=input.messages.filter(m=>m.role==="user").at(-1).content;
if(!messages.some(m=>m.content.includes(prompt))) throw new Error("native prompt provenance mismatch");
const gold=git(oracle,"diff","--binary","HEAD");
const testPath="test_arithmetic.py";
if(readFileSync(join(base,testPath),"utf8")!==readFileSync(join(oracle,testPath),"utf8"))
 throw new Error("held-out test changed");
const definition={
 schemaVersion:2,difficulty:"canary",taskId:"capture-"+manifest.capture_id,
 repo:"local-capture:"+manifest.capture_id,baseCommit:manifest.start.head,workdir:".",
 sourcePr:"none-local-capture",sourceUrl:pathToFileURL(join(capture,"manifest.json")).href,
 prompt,testCommand:"python3 -m unittest -v {tests}",failToPass:[testPath],passToPass:[],
 testPaths:[testPath],testSelection:{mode:"base-only",reused:[testPath],added:[],excluded:[],
 coverage:"Integration-only arithmetic assertion from the captured base."},
 timeouts:{setupSeconds:300,agentSeconds:600,testsSeconds:60},
 resources:{cpus:2,memoryMb:2048,storageMb:8192},
 environment:{schemaVersion:1,baseImage:image,
 rootSetupCommand:"apt-get update && apt-get install -y --no-install-recommends bash git procps util-linux curl ca-certificates && rm -rf /var/lib/apt/lists/*",
 setupCommand:"python3 --version",smokeCommand:"python3 -c 'import unittest'",
 environmentVariables:{},services:[],source:"generated",
 evidence:[{path:testPath,reason:"Standard library unittest fixture needs Python only."}]}
};
assertEnvironmentPolicy(definition.environment);
assertEnvironmentEvidence(definition.environment,new Set(git(base,"ls-tree","-r","--name-only","HEAD").trim().split("\n")));
const standard=auditTaskDefinition({...definition,difficulty:"easy"},gold,"");
const eligibility=canaryEligibility(standard,evidence);
writeFileSync(join(output,"eligibility.json"),JSON.stringify({eligibility,standardEasy:standard,evidence},null,2));
if(!eligibility.accepted) throw new Error(JSON.stringify(eligibility.blockers));
const task=join(output,"harbor-task"),environment=join(task,"environment"),tests=join(task,"tests"),solution=join(task,"solution");
for(const dir of [environment,tests,solution,join(tests,"runtime")]) mkdirSync(dir,{recursive:true});
git(base,"archive","--format=tar.gz","--output="+join(environment,"repo.tar.gz"),manifest.start.head);
copyFileSync(join(environment,"repo.tar.gz"),join(tests,"repo.tar.gz"));
writeFileSync(join(task,"instruction.md"),prompt+"\n");
writeFileSync(join(task,"definition.json"),JSON.stringify(definition,null,2));
writeFileSync(join(task,"task.toml"),render.taskToml(definition).replace('[metadata]','[metadata]\nbenchmark_eligible = false\npurpose = "pipeline integration only"'));
writeFileSync(join(solution,"gold.patch"),gold);
writeFileSync(join(solution,"solve.sh"),solutionScript());
for(const [name,text] of Object.entries(verifierRuntimeFiles())) writeFileSync(join(tests,name),text);
for(const name of ["test.sh","task-test.sh"]) writeFileSync(join(tests,name),testScript(definition,""));
writeFileSync(join(tests,"test.patch"),"");
writeFileSync(join(environment,"Dockerfile"),render.agentDockerfile(definition));
writeFileSync(join(tests,"Dockerfile"),render.verifierDockerfile(definition,""));
await Promise.all([...render.environmentContextFiles(environment,definition),...render.environmentContextFiles(tests,definition)]);
for(const dir of [environment,tests]) for(const name of ["root-setup.sh","setup.sh","smoke.sh"]) chmodSync(join(dir,name),0o755);
for(const file of [join(solution,"solve.sh"),join(tests,"test.sh"),join(tests,"task-test.sh")]) chmodSync(file,0o755);
const result={generator:"selfbench-local-canary-adapter",upstream:pinned,profile:"canary",
 benchmarkEligible:false,benchmarkDifficulty:null,capture_id:manifest.capture_id,
 source_manifest:join(capture,"manifest.json"),nativeProvenanceRecovered:true,
 historicalTrajectoryInSolverInput:false,eligibility,evidence};
writeFileSync(join(task,".selfbench-manifest.json"),JSON.stringify(result,null,2));
writeFileSync(join(output,"result.json"),JSON.stringify(result,null,2));
console.log(JSON.stringify(result));
