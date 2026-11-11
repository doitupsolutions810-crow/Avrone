"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");
const root=path.resolve(__dirname,"..");
const source=fs.readFileSync(path.join(root,"avrone/static/control-room.js"),"utf8");
const REPO_FULL="doitupsolutions810-crow/Avrone";
const GH="https://api.github.com/repos/"+REPO_FULL;
function extractFunction(name){
 const re=new RegExp("^(?:async\\s+)?function\\s+"+name+"\\s*\\(","m");
 const m=re.exec(source);assert.ok(m,"missing production function "+name);
 const tail=source.slice(m.index+m[0].length);
 const next=/^(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(/m.exec(tail);
 return source.slice(m.index,next?m.index+m[0].length+next.index:source.length);
}
function makeContext(overrides={}){
 const state={workflows:[],jobs:{},artifacts:{},status:null,commitStatus:null,commitStatusSha:"",errors:[],...overrides.state};
 const ctx={state,REPO_FULL,GH,URL,location:{href:"https://control-room.example.test/static/control-room.html"},AbortController,setTimeout,clearTimeout,fetch:overrides.fetch||(async()=>{throw new Error("unexpected fetch");})};
 vm.createContext(ctx);
 const names=["esc","short","date","badge","safeHref","link","showEmpty","apiHeaders","jsonFetch","gh","prLifecycle","prEvidence","isCiRun","latestForSha","currentPR","currentSha","loadSelectedCommitStatus","matchesCommon","ghPages","evidenceData","hasError","listEnvelope","validRecords","workflowDetailsNeedRefresh","renderPRs","renderVerification"];
 vm.runInContext(names.map(extractFunction).join("\n"),ctx);
 return ctx;
}
function response(body,options={}){
 const status=options.status??200;
 const headers=new Map(Object.entries(options.headers||{}).map(([k,v])=>[k.toLowerCase(),v]));
 return {ok:status>=200&&status<300,status,statusText:options.statusText||(status===200?"OK":"Error"),text:async()=>typeof body==="string"?body:JSON.stringify(body),headers:{get:key=>headers.get(String(key).toLowerCase())||null}};
}

test("HTML escaping neutralizes tags, ampersands, and quote types",()=>{
 const c=makeContext();
 assert.equal(c.esc("<script>alert(\"x\") & 'y'</script>"),"&lt;script&gt;alert(&quot;x&quot;) &amp; &#39;y&#39;&lt;/script&gt;");
 assert.equal(c.esc(null),"");assert.equal(c.esc(0),"0");
});
test("safeHref accepts HTTPS and rejects executable or non-web schemes",()=>{
 const c=makeContext();
 assert.equal(c.safeHref("https://github.com/example/repo"),"https://github.com/example/repo");
 assert.equal(c.safeHref("https://preview.vercel.app"),"https://preview.vercel.app/");
 assert.equal(c.safeHref("https://deploy.vercel.com"),"https://deploy.vercel.com/");
 assert.equal(c.safeHref("https://evil.github.com/phish"),"#");
 assert.equal(c.safeHref("https://github.com.evil.example"),"#");
 assert.equal(c.safeHref("https://evil.example/phish"),"#");
 for(const url of ["javascript:alert(1)","data:text/html,hello","ftp://example.com/file","mailto:test@example.com","file:///etc/passwd"]) assert.equal(c.safeHref(url),"#",url);
});
test("safeHref resolves relative links only on HTTPS pages",()=>{
 const c=makeContext();assert.equal(c.safeHref("/actions/runs/12"),"https://control-room.example.test/actions/runs/12");
 c.location.href="http://localhost:8000/static/index.html";assert.equal(c.safeHref("/api/status"),"#");
});
test("GitHub lifecycle keeps draft, merged, and unmerged separate",()=>{
 const c=makeContext();
 assert.deepEqual(JSON.parse(JSON.stringify(c.prLifecycle({state:"open",draft:true,merged:false,merged_at:null}))),{state:"OPEN",draft:true,merged:false,unmerged:true});
 assert.deepEqual(JSON.parse(JSON.stringify(c.prLifecycle({state:"closed",draft:false,merged:true,merged_at:"2026-11-11T00:00:00Z"}))),{state:"CLOSED",draft:false,merged:true,unmerged:false});
 assert.equal(c.prLifecycle({state:"closed",merged:false}).unmerged,true);
});
test("green workflow alone remains UNVERIFIED and absent workflow is NOT RUN",()=>{
 const c=makeContext({state:{status:{},workflows:[{name:"CI",head_sha:"abc123",status:"completed",conclusion:"success",updated_at:"2026-11-11T12:00:00Z"}]}});
 assert.equal(c.prEvidence({head:{sha:"abc123"}}),"UNVERIFIED");
 assert.equal(c.prEvidence({head:{sha:"missing"}}),"NOT RUN");
});
test("PR failure is explicit and successful evidence does not transfer across SHAs",()=>{
 const c=makeContext({state:{status:{},workflows:[
  {name:"CI",head_sha:"sha-a",status:"completed",conclusion:"failure",updated_at:"2026-11-11T12:00:00Z"},
  {name:"CI",head_sha:"sha-b",status:"completed",conclusion:"success",updated_at:"2026-11-11T12:01:00Z"}]}});
 assert.equal(c.prEvidence({head:{sha:"sha-a"}}),"FAIL");
 assert.equal(c.prEvidence({head:{sha:"sha-b"}}),"UNVERIFIED");
 assert.equal(c.prEvidence({head:{sha:"sha-c"}}),"NOT RUN");
});
test("latestForSha selects exact case-sensitive SHA and newest matching run",()=>{
 const c=makeContext({state:{workflows:[
  {id:1,name:"CI",head_sha:"abc",updated_at:"2026-11-10T00:00:00Z"},
  {id:2,name:"CI",head_sha:"def",updated_at:"2026-11-12T00:00:00Z"},
  {id:3,name:"CI",head_sha:"abc",updated_at:"2026-11-11T00:00:00Z"},
  {id:4,name:"squad-round-notify",path:".github/workflows/squad-round-notify.yml",head_sha:"abc",updated_at:"2026-11-13T00:00:00Z"}]}});
 assert.equal(c.latestForSha("abc").id,3);assert.equal(c.latestForSha("def").id,2);
 assert.equal(c.latestForSha("ABC"),null);assert.equal(c.latestForSha("none"),null);
});
test("all requested filters can match a record",()=>{
 const c=makeContext();const rec={number:28,head_sha:"abcdef0123",swarm_round:20,lane:"planner",agent:"lead-1",runtime:"Python 3.12",evidence_status:"UNVERIFIED"};
 assert.equal(c.matchesCommon(rec,{repo:"doitupsolutions810",pr:"28",commit:"abcdef",round:"20",lane:"planner",agent:"lead-1",runtime:"Python 3.12",verification:"UNVERIFIED"}),true);
});
test("each filter dimension rejects a mismatched record",()=>{
 const c=makeContext();const rec={number:28,head_sha:"abcdef0123",swarm_round:20,lane:"planner",agent:"lead-1",runtime:"Python 3.12",evidence_status:"UNVERIFIED"};
 const filters=[{repo:"another/repo"},{pr:"27"},{commit:"ffff"},{round:"19"},{lane:"worker"},{agent:"lead-2"},{runtime:"Python 3.11"},{verification:"VERIFIED"}];
 for(const filter of filters)assert.equal(c.matchesCommon(rec,filter),false,JSON.stringify(filter));
});
test("Actions success/failure vocabulary maps to PASS/FAIL but not VERIFIED",()=>{
 const c=makeContext();assert.equal(c.matchesCommon({conclusion:"success"},{verification:"PASS"}),true);
 assert.equal(c.matchesCommon({conclusion:"failure"},{verification:"FAIL"}),true);
 assert.equal(c.matchesCommon({conclusion:"cancelled"},{verification:"FAIL"}),true);
 assert.equal(c.matchesCommon({conclusion:"success"},{verification:"VERIFIED"}),false);
});
test("stale workflow jobs cannot pass Python matrix gates",()=>{
 const c=makeContext({state:{jobs:{"run-1":[{name:"test (3.11)",conclusion:"success"},{name:"test (3.12)",conclusion:"success"}]}}});
 const g=Object.fromEntries(c.evidenceData({head:{sha:"new"}},{id:"run-1",head_sha:"old"}).gates.map(x=>[x.name,x.state]));
 assert.equal(g["Exact-SHA Python 3.11"],"NOT RUN");assert.equal(g["Exact-SHA Python 3.12"],"NOT RUN");
});
test("Python matrix legs use their own same-SHA conclusions",()=>{
 const c=makeContext({state:{jobs:{"run-2":[{name:"test (3.11)",conclusion:"success"},{name:"test (3.12)",conclusion:"failure"}]}}});
 const g=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"run-2",head_sha:"same"}).gates.map(x=>[x.name,x.state]));
 assert.equal(g["Exact-SHA Python 3.11"],"PASS");assert.equal(g["Exact-SHA Python 3.12"],"FAIL");
});
test("coverage artifacts need both exact-SHA Python matrix legs before passing",()=>{
 const c=makeContext({state:{jobs:{"run-3":[
  {name:"test (3.11)",conclusion:"success"},{name:"test (3.12)",conclusion:"success"}
 ]},artifacts:{"run-3":[
  {name:"coverage-xml-py3.11"},{name:"coverage-xml-py3.12"},
  {name:"coverage-json-py3.11"},{name:"coverage-json-py3.12",expired:true}
 ]}}});
 const g=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"run-3",head_sha:"same"}).gates.map(x=>[x.name,x.state]));
 assert.equal(g["coverage.xml artifact"],"PASS");
 assert.equal(g["coverage.json artifact"],"EXPIRED");
});
test("Docker and Node job success remains NOT RUN without its evidence artifacts",()=>{
 const c=makeContext({state:{jobs:{"run-4":[
  {name:"Docker image verification",conclusion:"success"},
  {name:"Node container lifecycle",conclusion:"success"}
 ]},artifacts:{"run-4":[]}}});
 const g=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"run-4",head_sha:"same"}).gates.map(x=>[x.name,x.state]));
 assert.equal(g["Docker image verification"],"NOT RUN");
 assert.equal(g["In-container Node lifecycle"],"NOT RUN");
});
test("Vercel statuses preserve failure, pending, review, and stale SHA",()=>{
 for(const [state,expected,statusSha] of [["failure","FAIL","same"],["pending","UNVERIFIED","same"],["success","PENDING REVIEW","same"],["success","NOT RUN","old"]]){
  const c=makeContext({state:{commitStatusSha:statusSha,commitStatus:{statuses:[{context:"Vercel",state}]},jobs:{"run-5":[]}}});
  const g=c.evidenceData({head:{sha:"same"}},{id:"run-5",head_sha:"same"}).gates;
  assert.equal(g.find(x=>x.name==="Vercel preview deployment").state,expected);
 }
});
test("missing local runtime blocks the Apple-Silicon Ollama gate",()=>{
 const c=makeContext({state:{status:null,jobs:{"run-6":[]}}});
 const g=c.evidenceData({head:{sha:"same"}},{id:"run-6",head_sha:"same"}).gates;
 assert.equal(g.find(x=>x.name==="Local Apple-Silicon Ollama squad").state,"BLOCKED");
});
test("GitHub pagination follows next links and aggregates pages",async()=>{
 const calls=[];const c=makeContext({fetch:async url=>{
  calls.push(url);
  if(calls.length===1)return response([{id:1}],{headers:{Link:"<"+GH+"/pulls?page=2>; rel=\"next\", <"+GH+"/pulls?page=2>; rel=\"last\""}});
  return response([{id:2}]);
 }});
 const r=await c.ghPages("/pulls?per_page=1",5);
 assert.deepEqual(JSON.parse(JSON.stringify(r.items)),[{id:1},{id:2}]);assert.equal(r.pages,2);assert.equal(r.truncated,false);assert.equal(calls.length,2);
});
test("GitHub pagination signals truncation at the page cap",async()=>{
 const c=makeContext({fetch:async()=>response([{id:1}],{headers:{Link:"<"+GH+"/commits?page=2>; rel=\"next\""}})});
 const r=await c.ghPages("/commits?per_page=1",1);assert.equal(r.pages,1);assert.equal(r.truncated,true);
});
test("GitHub pagination rejects malformed JSON",async()=>{
 const c=makeContext({fetch:async()=>response("{bad-json")});
 await assert.rejects(()=>c.ghPages("/pulls",1),/Invalid JSON from GitHub page 1/);
});
test("GitHub pagination surfaces authorization and rate-limit errors",async()=>{
 for(const status of [401,403,429]){
  const c=makeContext({fetch:async()=>response({message:"API rate limit exceeded"},{status,statusText:"Rejected"})});
  await assert.rejects(()=>c.ghPages("/pulls",1),new RegExp(String(status)));
 }
});
test("GitHub pagination propagates network failure and clears timer",async()=>{
 let cleared=false;const c=makeContext({fetch:async()=>{throw new Error("network disconnected");}});
 c.clearTimeout=()=>{cleared=true;};await assert.rejects(()=>c.ghPages("/pulls",1),/network disconnected/);assert.equal(cleared,true);
});


test("GitHub pagination rejects off-domain next links without following them",async()=>{
 let calls=0;
 const c=makeContext({fetch:async()=>{calls++;return response([{id:1}],{headers:{Link:"<https://evil.example/steal>; rel=\"next\""}});}});
 await assert.rejects(()=>c.ghPages("/pulls",5),/Rejected unsafe GitHub pagination URL/);
 assert.equal(calls,1);
});
test("API health helper distinguishes missing endpoint errors from empty successful results",()=>{
 const c=makeContext({state:{errors:[{key:"runs",message:"503 unavailable"}]}});
 assert.equal(c.hasError("runs"),true);
 assert.equal(c.hasError("checkpoints"),false);
 c.state.errors=[];
 assert.equal(c.hasError("runs"),false);
});


test("workflow API failure is BLOCKED rather than reported as no CI run",()=>{
 const c=makeContext({state:{errors:[{key:"workflows",message:"403 rate limited"}]}});
 assert.equal(c.prEvidence({head:{sha:"target"}}),"BLOCKED");
 const g=c.evidenceData({head:{sha:"target"}},null).gates;
 assert.equal(g.find(x=>x.name==="Exact-SHA Python 3.11").state,"BLOCKED");
 assert.equal(g.find(x=>x.name==="Exact-SHA Python 3.12").state,"BLOCKED");
});
test("job-detail API failure blocks matrix and runtime gates",()=>{
 const c=makeContext({state:{
  jobs:{"run-x":[]},errors:[{key:"jobs-run-x",message:"403 forbidden"}]
 }});
 const g=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"run-x",head_sha:"same"}).gates.map(x=>[x.name,x.state]));
 assert.equal(g["Exact-SHA Python 3.11"],"BLOCKED");
 assert.equal(g["Docker image verification"],"BLOCKED");
 assert.equal(g["In-container Node lifecycle"],"BLOCKED");
});
test("artifact API failure is BLOCKED; expired artifacts are EXPIRED, not passed",()=>{
 const c=makeContext({state:{
  artifacts:{"run-y":[{name:"coverage-xml-py3.11",expired:true}]},
  errors:[{key:"artifacts-run-y",message:"404 expired"}]
 }});
 const g=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"run-y",head_sha:"same"}).gates.map(x=>[x.name,x.state]));
 assert.equal(g["coverage.xml artifact"],"BLOCKED");
 const c2=makeContext({state:{jobs:{"run-y":[{name:"test (3.11)",conclusion:"success"},{name:"test (3.12)",conclusion:"success"}]},artifacts:{"run-y":[{name:"coverage-xml-py3.11",expired:true}]}}});
 const g2=Object.fromEntries(c2.evidenceData({head:{sha:"same"}},{id:"run-y",head_sha:"same"}).gates.map(x=>[x.name,x.state]));
 assert.equal(g2["coverage.xml artifact"],"EXPIRED");
});
test("commit-status API failure blocks the Vercel gate",()=>{
 const c=makeContext({state:{
  commitStatusSha:"same",commitStatus:null,
  errors:[{key:"commit-status",message:"502 unavailable"}],
  jobs:{"run-z":[]}
 }});
 const g=c.evidenceData({head:{sha:"same"}},{id:"run-z",head_sha:"same"}).gates;
 assert.equal(g.find(x=>x.name==="Vercel preview deployment").state,"BLOCKED");
});

test("GitHub pagination rejects same-host links outside the selected repository path",async()=>{
 let calls=0;
 const c=makeContext({fetch:async()=>{calls++;return response([{id:1}],{headers:{Link:"<https://api.github.com/user>; rel=\"next\""}});}});
 await assert.rejects(()=>c.ghPages("/pulls",5),/Rejected unsafe GitHub pagination URL/);
 assert.equal(calls,1);
});
test("GitHub pagination rejects protocol downgrade and repository traversal",async()=>{
 for(const next of ["http://api.github.com/repos/"+REPO_FULL+"/pulls?page=2","https://api.github.com/repos/"+REPO_FULL+"/../../user"]){
  let calls=0;
  const c=makeContext({fetch:async()=>{calls++;return response([{id:1}],{headers:{Link:"<"+next+">; rel=\"next\""}});}});
  await assert.rejects(()=>c.ghPages("/pulls",5),/Rejected unsafe GitHub pagination URL/);
  assert.equal(calls,1);
 }
});

test("non-CI workflow failures cannot mask the exact-SHA CI run",()=>{
 const c=makeContext({state:{status:{},workflows:[
  {id:10,name:"CI",path:".github/workflows/ci.yml",head_sha:"same",status:"completed",conclusion:"success",updated_at:"2026-11-11T10:00:00Z"},
  {id:11,name:"squad-round-notify",path:".github/workflows/squad-round-notify.yml",head_sha:"same",status:"completed",conclusion:"failure",updated_at:"2026-11-11T10:01:00Z"}
 ]}});
 assert.equal(c.isCiRun(c.state.workflows[0]),true);
 assert.equal(c.isCiRun(c.state.workflows[1]),false);
 assert.equal(c.isCiRun({name:"CI",path:".github/workflows/other.yml"}),false);
 assert.equal(c.isCiRun({name:"CI"}),true);
 assert.equal(c.latestForSha("same").id,10);
 assert.equal(c.prEvidence({head:{sha:"same"}}),"UNVERIFIED");
});
test("a notification workflow alone is not mistaken for a Python CI run",()=>{
 const c=makeContext({state:{workflows:[
  {id:12,name:"squad-round-notify",path:".github/workflows/squad-round-notify.yml",head_sha:"only-notify",status:"completed",conclusion:"failure"}
 ]}});
 assert.equal(c.latestForSha("only-notify"),null);
 assert.equal(c.prEvidence({head:{sha:"only-notify"}}),"NOT RUN");
});

test("evidence helpers degrade safely on null and malformed record arguments",()=>{
 const c=makeContext();
 assert.equal(c.prLifecycle(null).state,"UNKNOWN");
 assert.equal(c.prLifecycle("untrusted").unmerged,true);
 assert.equal(c.prEvidence(null),"NOT RUN");
 assert.equal(c.matchesCommon(null,null),true);
 assert.equal(c.matchesCommon("not-an-object",{verification:"PASS"}),false);
});

test("coverage gates pass only with both Python versions and all four exact artifacts",()=>{
 const c=makeContext({state:{
  jobs:{"ci-1":[
   {name:"test (3.11)",conclusion:"success"},
   {name:"test (3.12)",conclusion:"success"}
  ]},
  artifacts:{"ci-1":[
   {name:"coverage-xml-py3.11",expired:false},
   {name:"coverage-xml-py3.12",expired:false},
   {name:"coverage-json-py3.11",expired:false},
   {name:"coverage-json-py3.12",expired:false}
  ]}
 }});
 const gates=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"ci-1",head_sha:"same",status:"completed"}).gates.map(g=>[g.name,g.state]));
 assert.equal(gates["Exact-SHA Python 3.11"],"PASS");
 assert.equal(gates["Exact-SHA Python 3.12"],"PASS");
 assert.equal(gates["coverage.xml artifact"],"PASS");
 assert.equal(gates["coverage.json artifact"],"PASS");
});
test("coverage gates do not pass on one matrix leg, missing or expired per-version artifact",()=>{
 const baseJobs={"ci-1":[{name:"test (3.11)",conclusion:"success"},{name:"test (3.12)",conclusion:"success"}]};
 const missing=makeContext({state:{jobs:baseJobs,artifacts:{"ci-1":[
  {name:"coverage-xml-py3.11"},{name:"coverage-json-py3.11"},{name:"coverage-json-py3.12"}
 ]}}});
 let gates=Object.fromEntries(missing.evidenceData({head:{sha:"same"}},{id:"ci-1",head_sha:"same"}).gates.map(g=>[g.name,g.state]));
 assert.equal(gates["coverage.xml artifact"],"NOT RUN");
 const expired=makeContext({state:{jobs:baseJobs,artifacts:{"ci-1":[
  {name:"coverage-xml-py3.11"},{name:"coverage-xml-py3.12",expired:true},
  {name:"coverage-json-py3.11"},{name:"coverage-json-py3.12"}
 ]}}});
 gates=Object.fromEntries(expired.evidenceData({head:{sha:"same"}},{id:"ci-1",head_sha:"same"}).gates.map(g=>[g.name,g.state]));
 assert.equal(gates["coverage.xml artifact"],"EXPIRED");
 assert.equal(gates["coverage.json artifact"],"PASS");
});
test("Docker and Node gates pass only with exact-SHA jobs and evidence artifacts",()=>{
 const c=makeContext({state:{
  jobs:{"ci-2":[
   {name:"Docker image verification",conclusion:"success"},
   {name:"Node container lifecycle",conclusion:"success"}
  ]},
  artifacts:{"ci-2":[
   {name:"docker-image-evidence",expired:false},
   {name:"node-container-lifecycle-evidence",expired:false}
  ]}
 }});
 const gates=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"ci-2",head_sha:"same"}).gates.map(g=>[g.name,g.state]));
 assert.equal(gates["Docker image verification"],"PASS");
 assert.equal(gates["In-container Node lifecycle"],"PASS");
});
test("Docker and Node gates stay provisional without their artifact or fail on expired artifact",()=>{
 const c=makeContext({state:{
  jobs:{"ci-3":[
   {name:"Docker image verification",conclusion:"success"},
   {name:"Node container lifecycle",conclusion:"success"}
  ]},
  artifacts:{"ci-3":[{name:"docker-image-evidence",expired:true}]}
 }});
 const gates=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"ci-3",head_sha:"same"}).gates.map(g=>[g.name,g.state]));
 assert.equal(gates["Docker image verification"],"EXPIRED");
 assert.equal(gates["In-container Node lifecycle"],"NOT RUN");
});


test("workflow job and artifact cache refreshes while the run is active",()=>{
 const c=makeContext();
 const n=c.workflowDetailsNeedRefresh(
  {status:"in_progress"},[{name:"test (3.11)",status:"completed"},{name:"test (3.12)",status:"in_progress"}],[],false,false
 );
 assert.equal(n.jobs,true);
 assert.equal(n.artifacts,true);
});
test("workflow cache stops refreshing after terminal jobs and final artifact snapshot",()=>{
 const c=makeContext();
 const n=c.workflowDetailsNeedRefresh(
  {status:"completed"},[{name:"test (3.11)",status:"completed"},{name:"test (3.12)",status:"completed"}],[],false,false,true
 );
 assert.deepEqual(JSON.parse(JSON.stringify(n)),{jobs:false,artifacts:false});
});
test("workflow cache retries after previous jobs or artifacts API errors",()=>{
 const c=makeContext();
 const n=c.workflowDetailsNeedRefresh({status:"completed"},[],[],true,true,false);
 assert.deepEqual(JSON.parse(JSON.stringify(n)),{jobs:true,artifacts:true});
});

test("artifact cache receives one final refresh after a workflow completes",()=>{
 const c=makeContext();
 const active=c.workflowDetailsNeedRefresh({status:"in_progress"},[
  {name:"test (3.11)",status:"completed"},{name:"test (3.12)",status:"completed"}
 ],[],false,false,false);
 assert.equal(active.jobs,false);
 assert.equal(active.artifacts,true);
 const completedNotFinal=c.workflowDetailsNeedRefresh({status:"completed"},[
  {name:"test (3.11)",status:"completed"},{name:"test (3.12)",status:"completed"}
 ],[],false,false,false);
 assert.equal(completedNotFinal.jobs,false);
 assert.equal(completedNotFinal.artifacts,true);
 const completedFinal=c.workflowDetailsNeedRefresh({status:"completed"},[
  {name:"test (3.11)",status:"completed"},{name:"test (3.12)",status:"completed"}
 ],[],false,false,true);
 assert.deepEqual(JSON.parse(JSON.stringify(completedFinal)),{jobs:false,artifacts:false});
});


test("safeHref rejects credential-bearing URLs and authenticated host confusion",()=>{
 const c=makeContext();
 for(const url of [
  "https://user:pass@github.com/example/repo",
  "https://user@api.github.com/repos/"+REPO_FULL,
  "https://github.com@evil.example/phish",
  "//evil.example/path"
 ]) assert.equal(c.safeHref(url),"#",url);
 assert.equal(c.safeHref("https://preview.vercel.app/deploy"),"https://preview.vercel.app/deploy");
});

test("date formatting degrades on malformed, non-finite, and throwing values",()=>{
 const c=makeContext();
 assert.equal(c.date(null),"—");
 assert.equal(c.date("not-a-date"),"—");
 assert.equal(c.date(Symbol("invalid")),"—");
});

test("GitHub pagination detects repeated next URLs instead of looping",async()=>{
 let calls=0;
 const repeated=GH+"/pulls?page=2";
 const c=makeContext({fetch:async()=>{calls++;return response([{id:calls}],{headers:{Link:"<"+repeated+">; rel=\"next\""}});}});
 await assert.rejects(()=>c.ghPages("/pulls?page=1",5),/Repeated GitHub pagination URL/);
 assert.equal(calls,2);
});

test("GitHub pagination treats malformed successful envelopes as blocked errors",async()=>{
 for(const body of [{message:"unexpected object"},{items:[{id:1}]},null,"null"]){
  const c=makeContext({fetch:async()=>response(body)});
  await assert.rejects(()=>c.ghPages("/pulls",1),/Unexpected GitHub list response shape/);
 }
});

test("GitHub pagination accepts the documented workflow_runs object envelope",async()=>{
 const c=makeContext({fetch:async()=>response({total_count:1,workflow_runs:[{id:9}]})});
 const result=await c.ghPages("/actions/runs",1);
 assert.equal(result.items[0].id,9);
 assert.equal(result.truncated,false);
});

test("GitHub pagination rejects credential-bearing next links on the trusted API host",async()=>{
 let calls=0;
 const next="https://attacker@api.github.com/repos/"+REPO_FULL+"/pulls?page=2";
 const c=makeContext({fetch:async()=>{calls++;return response([{id:1}],{headers:{Link:"<"+next+">; rel=\"next\""}});}});
 await assert.rejects(()=>c.ghPages("/pulls",5),/Rejected unsafe GitHub pagination URL/);
 assert.equal(calls,1);
});

test("jsonFetch returns null for an empty successful response but rejects malformed JSON",async()=>{
 const c=makeContext({fetch:async()=>response("",{status:204})});
 assert.equal(await c.jsonFetch("/empty"),null);
 const bad=makeContext({fetch:async()=>response("<html>not json</html>")});
 await assert.rejects(()=>bad.jsonFetch("/bad"),e=>e.message.includes("Invalid JSON from /bad"));
});

test("jsonFetch preserves structured HTTP failure details and clears its timeout",async()=>{
 let cleared=false;
 const c=makeContext({fetch:async()=>response({detail:"permission denied"},{status:403,statusText:"Forbidden"})});
 c.clearTimeout=()=>{cleared=true;};
 await assert.rejects(()=>c.jsonFetch("/private"),/403 Forbidden: permission denied/);
 assert.equal(cleared,true);
});

test("matchesCommon survives cyclic and partially unreadable objects",()=>{
 const c=makeContext();
 const cyclic={lane:"planner",agent:"worker-a"};
 cyclic.self=cyclic;
 assert.equal(c.matchesCommon(cyclic,{lane:"planner",agent:"worker-a"}),true);
 const hostile={lane:"worker-b"};
 Object.defineProperty(hostile,"secret",{enumerable:true,get(){throw new Error("getter trap");}});
 assert.equal(c.matchesCommon(hostile,{lane:"worker-b"}),true);
});

test("matchesCommon safely normalizes null and numeric filter values",()=>{
 const c=makeContext();
 assert.equal(c.matchesCommon({number:28,head_sha:"abcdef",runtime:"Python 3.12"},{repo:null,pr:28,commit:"abcdef",round:null,lane:null,agent:null,verification:null}),true);
 assert.equal(c.matchesCommon({number:28},{pr:0}),false);
});

test("latestForSha safely handles null collections and malformed workflow rows",()=>{
 const c=makeContext({state:{workflows:[null,{},42,{head_sha:"target",path:".github/workflows/ci.yml",id:8}]}});
 assert.equal(c.latestForSha("target").id,8);
 c.state.workflows=null;
 assert.equal(c.latestForSha("target"),null);
});

test("hasError treats malformed error collections as no parsed error instead of throwing",()=>{
 const c=makeContext({state:{errors:null}});
 assert.equal(c.hasError("workflows"),false);
 c.state.errors=[null,{key:"jobs-1"}];
 assert.equal(c.hasError("jobs-1"),true);
});

test("evidenceData ignores null records and malformed maps without a false PASS",()=>{
 const c=makeContext({state:{
  jobs:{"run-8":[null,{name:"test (3.11)",conclusion:"success"},{name:"test (3.12)",conclusion:"success"}]},
  artifacts:{"run-8":[null,{}, {name:"coverage-xml-py3.11"},{name:"coverage-xml-py3.12"},{name:"coverage-json-py3.11"},{name:"coverage-json-py3.12"}]},
  commitStatus:{statuses:"not-an-array"},commitStatusSha:"same"
 }});
 const gates=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"run-8",head_sha:"same",status:"completed"}).gates.map(g=>[g.name,g.state]));
 assert.equal(gates["Exact-SHA Python 3.11"],"PASS");
 assert.equal(gates["Exact-SHA Python 3.12"],"PASS");
 assert.equal(gates["coverage.xml artifact"],"PASS");
 assert.equal(gates["coverage.json artifact"],"PASS");
 assert.equal(gates["Vercel preview deployment"],"NOT RUN");
 const malformed=makeContext({state:{jobs:null,artifacts:null,commitStatus:{statuses:{}},commitStatusSha:"same"}});
 assert.doesNotThrow(()=>malformed.evidenceData({head:{sha:"same"}},{id:"empty",head_sha:"same"}));
});

test("evidenceData refuses to choose between duplicate matrix job identities",()=>{
 const c=makeContext({state:{jobs:{"run-9":[
  {name:"test (3.11)",conclusion:"success"},
  {name:"test (3.11)",conclusion:"failure"},
  {name:"test (3.12)",conclusion:"success"}
 ]}}});
 const gates=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"run-9",head_sha:"same",status:"completed"}).gates.map(g=>[g.name,g.state]));
 assert.equal(gates["Exact-SHA Python 3.11"],"NOT RUN");
 assert.equal(gates["Exact-SHA Python 3.12"],"PASS");
});


test("evidenceData refuses ambiguous duplicate artifact identities",()=>{
 const c=makeContext({state:{jobs:{"run-10":[
  {name:"test (3.11)",conclusion:"success"},
  {name:"test (3.12)",conclusion:"success"}
 ]},artifacts:{"run-10":[
  {name:"coverage-xml-py3.11",expired:false},
  {name:"coverage-xml-py3.11",expired:true},
  {name:"coverage-xml-py3.12",expired:false}
 ]}}});
 const gates=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"run-10",head_sha:"same",status:"completed"}).gates.map(g=>[g.name,g.state]));
 assert.equal(gates["coverage.xml artifact"],"NOT RUN");
});

test("jsonFetch aborts a stalled request and clears its timeout handle",async()=>{
 let timerCallback=null,cleared=false,signal=null;
 const c=makeContext({fetch:async(_url,options)=>new Promise((resolve,reject)=>{
  signal=options.signal;
  signal.addEventListener("abort",()=>reject(new Error("request aborted")));
 })});
 c.setTimeout=(fn)=>{timerCallback=fn;return 77;};
 c.clearTimeout=(id)=>{cleared=id===77;};
 const pending=c.jsonFetch("/slow");
 assert.equal(typeof timerCallback,"function");
 timerCallback();
 await assert.rejects(()=>pending,/request aborted/);
 assert.equal(signal.aborted,true);
 assert.equal(cleared,true);
});


test("workflow detail envelopes distinguish valid empty lists from malformed API success",()=>{
 const c=makeContext();
 assert.deepEqual(JSON.parse(JSON.stringify(c.listEnvelope({jobs:[]},"jobs"))),[]);
 assert.deepEqual(JSON.parse(JSON.stringify(c.listEnvelope({artifacts:[{id:1}]},"artifacts"))),[{id:1}]);
 for(const payload of [null,{},[],{jobs:{}},{jobs:null},{jobs:"empty"},{data:[1]}]){
  assert.equal(c.listEnvelope(payload,"jobs"),null);
 }
});

test("workflow cache remains refreshable when a job list contains null or malformed entries",()=>{
 const c=makeContext();
 const result=c.workflowDetailsNeedRefresh(
  {status:"in_progress"},
  [null,{status:"completed"}],
  [],
  false,false,false
 );
 assert.equal(result.jobs,true);
 assert.equal(result.artifacts,true);
});

test("matchesCommon catches throwing getters on selector fields",()=>{
 const c=makeContext();
 const row={};
 Object.defineProperty(row,"number",{enumerable:true,get(){throw new Error("number getter");}});
 Object.defineProperty(row,"head_sha",{enumerable:true,get(){throw new Error("sha getter");}});
 assert.doesNotThrow(()=>c.matchesCommon(row,{pr:"28",commit:"abc"}));
 assert.equal(c.matchesCommon(row,{pr:"28",commit:"abc"}),false);
});


test("validRecords rejects null, arrays, and primitives while preserving object rows",()=>{
 const c=makeContext();
 const result=c.validRecords([null,{},[],0,"bad",false,{id:"kept"}]);
 assert.equal(result.items.length,2);
 assert.equal(result.rejected,5);
 assert.deepEqual(JSON.parse(JSON.stringify(c.validRecords(null))),{items:[],rejected:0});
});

test("GitHub pagination rejects null, primitive, and nested-array records",async()=>{
 for(const body of [[null],[1],["bad"],[[]]]){
  const c=makeContext({fetch:async()=>response(body)});
  await assert.rejects(()=>c.ghPages("/pulls",1),/Malformed GitHub list record on page 1/);
 }
 const empty=makeContext({fetch:async()=>response([])});
 assert.deepEqual(JSON.parse(JSON.stringify((await empty.ghPages("/pulls",1)).items)),[]);
});

test("GitHub pagination timeout remains active while the response body is read",async()=>{
 let timerCallback=null,clearedBeforeBody=false,bodyStarted=false,signal,resolveBodyStarted;
 const bodyStartedSignal=new Promise(resolve=>{resolveBodyStarted=resolve;});
 const c=makeContext({fetch:async(_url,options)=>{
  signal=options.signal;
  return {ok:true,status:200,statusText:"OK",headers:{get:()=>null},text:()=> {
   bodyStarted=true;
   resolveBodyStarted();
   return signal.aborted?Promise.reject(new Error("body aborted")):new Promise((_,reject)=>signal.addEventListener("abort",()=>reject(new Error("body aborted"))));
  }};
 }});
 c.setTimeout=fn=>{timerCallback=fn;return 88;};
 c.clearTimeout=()=>{clearedBeforeBody=!bodyStarted;};
 const pending=c.ghPages("/pulls",1);
 await bodyStartedSignal;
 assert.equal(bodyStarted,true);
 assert.equal(typeof timerCallback,"function");
 timerCallback();
 await assert.rejects(()=>pending,/body aborted/);
 assert.equal(signal.aborted,true);
 assert.equal(clearedBeforeBody,false);
});

test("GitHub requests never receive the local AVRONE admin token",async()=>{
 let seenHeaders=null;
 const c=makeContext({state:{adminToken:"local-only-secret"},fetch:async(_url,opts)=>{seenHeaders=opts.headers;return response({ok:true});}});
 await c.gh("/pulls");
 assert.equal(seenHeaders.Accept,"application/vnd.github+json");
 assert.equal(seenHeaders["X-GitHub-Api-Version"],"2022-11-28");
 assert.equal(seenHeaders["x-admin-token"],undefined);
 assert.equal(c.apiHeaders()["x-admin-token"],"local-only-secret");
});

test("Control Room cache helper retries incomplete or malformed active job records",()=>{
 const c=makeContext();
 const result=c.workflowDetailsNeedRefresh(
  {status:"in_progress"},
  [{status:"completed"},null],
  [],
  false,false,false
 );
 assert.equal(result.jobs,true);
});


test("GitHub pagination enforces a hard maximum even when callers request excessive pages",async()=>{
 let calls=0;
 const c=makeContext({fetch:async(url)=>{calls++;const page=Number(new URL(url).searchParams.get("page")||1);const next=GH+"/pulls?page="+(page+1);return response([{id:page}],{headers:{Link:page<12?"<"+next+">; rel=\"next\"":""}});}});
 const result=await c.ghPages("/pulls?page=1",10000);
 assert.equal(calls,10);
 assert.equal(result.pages,10);
 assert.equal(result.items.length,10);
 assert.equal(result.truncated,true);
});

test("GitHub pagination rejects a malformed next relation instead of silently truncating",async()=>{
 const c=makeContext({fetch:async()=>response([{id:1}],{headers:{Link:'rel="next", broken'}})});
 await assert.rejects(()=>c.ghPages("/pulls",5),/Malformed GitHub next-page Link header/);
});

test("GitHub pagination accepts valid unquoted next relations",async()=>{
 let calls=0;
 const c=makeContext({fetch:async()=>{calls++;return calls===1?response([{id:1}],{headers:{Link:"<"+GH+"/pulls?page=2>; rel=next"}}):response([{id:2}]);}});
 const result=await c.ghPages("/pulls?page=1",5);
 assert.deepEqual(JSON.parse(JSON.stringify(result.items.map(x=>x.id))),[1,2]);
});

test("currentPR ignores malformed rows and resolves numeric identifier representations",()=>{
 const c=makeContext({state:{prs:[null,[],{number:"28",title:"valid"},{number:27,title:"fallback"}],selectedPR:28}});
 assert.equal(c.currentPR().title,"valid");
 c.state.selectedPR=999;
 assert.equal(c.currentPR().number,27);
 c.state.prs=null;
 assert.equal(c.currentPR(),null);
});

test("latestForSha ignores malformed list rows and tolerates invalid update timestamps",()=>{
 const c=makeContext({state:{workflows:[
  null,[],{name:"CI",head_sha:"abc",updated_at:"invalid",id:1},
  {path:".github/workflows/ci.yml",head_sha:"abc",updated_at:"2026-11-11T00:00:00Z",id:2}
 ]}});
 assert.equal(c.latestForSha("abc").id,2);
});

test("commit status response from an old PR cannot overwrite the newly selected SHA",async()=>{
 const resolvers={};
 const c=makeContext({state:{prs:[
  {number:1,head:{sha:"sha-one"}},
  {number:2,head:{sha:"sha-two"}}
 ],selectedPR:1,commitStatus:null,commitStatusSha:"",errors:[]}});
 c.gh=path=>new Promise(resolve=>{resolvers[path]=resolve;});
 const first=c.loadSelectedCommitStatus();
 c.state.selectedPR=2;
 const second=c.loadSelectedCommitStatus();
 resolvers["/commits/sha-two/status"]({sha:"sha-two"});
 await second;
 resolvers["/commits/sha-one/status"]({sha:"sha-one"});
 await first;
 assert.equal(c.state.commitStatusSha,"sha-two");
 assert.equal(c.state.commitStatus.sha,"sha-two");
});

test("PR number is escaped in table cells so malformed remote identifiers cannot inject markup",()=>{
 const c=makeContext({state:{prs:[{
  number:'"><img src=x onerror=alert(1)>',
  title:"untrusted title",
  state:"open",draft:false,merged:false,head:{sha:"abc",ref:"branch"},
  html_url:"https://github.com/doitupsolutions810-crow/Avrone/pull/1"
 }]}});
 const nodes={"pr-count":{textContent:""},"pull-list":{innerHTML:""}};
 c.$=id=>nodes[id]||(nodes[id]={textContent:"",innerHTML:"",value:""});
 c.activeFilters=()=>({});
 c.matchesCommon=()=>true;
 c.badge=value=>"<b>"+c.esc(value)+"</b>";
 c.formatSha=value=>String(value||"").slice(0,8);
 c.renderPRs=vm.runInContext("("+extractFunction("renderPRs")+")",c);
 c.renderPRs();
 assert.equal(nodes["pull-list"].innerHTML.includes("<img"),false);
 assert.equal(nodes["pull-list"].innerHTML.includes("&lt;img"),true);
});

test("validRecords removes malformed entries without mutating the source array",()=>{
 const c=makeContext();
 const input=[{id:1},null,[],0,{id:2}];
 const before=input.slice();
 const out=c.validRecords(input);
 assert.deepEqual(JSON.parse(JSON.stringify(out.items)),[{id:1},{id:2}]);
 assert.equal(out.rejected,3);
 assert.deepEqual(input,before);
});

test("jsonFetch timeout remains active while JSON response body parsing is blocked",async()=>{
 let timerCallback=null,bodyStartedResolve,signal;
 const bodyStarted=new Promise(resolve=>{bodyStartedResolve=resolve;});
 const c=makeContext({fetch:async(_url,options)=>{
  signal=options.signal;
  return {ok:true,status:200,statusText:"OK",text:()=>{bodyStartedResolve();return new Promise((_,reject)=>signal.addEventListener("abort",()=>reject(new Error("json body aborted"))));}};
 }});
 c.setTimeout=fn=>{timerCallback=fn;return 91;};
 c.clearTimeout=()=>{};
 const pending=c.jsonFetch("/stalled-body");
 await bodyStarted;
 timerCallback();
 await assert.rejects(()=>pending,/json body aborted/);
 assert.equal(signal.aborted,true);
});

test("HTTP JSON error responses preserve status and structured detail",async()=>{
 const c=makeContext({fetch:async()=>response({detail:"rate limited"},{status:429,statusText:"Too Many Requests"})});
 await assert.rejects(()=>c.jsonFetch("/limited"),/429 Too Many Requests: rate limited/);
});

test("workflow detail cache error filtering tolerates null error entries",()=>{
 const c=makeContext({state:{errors:[null,{key:"jobs-r1",message:"temporary"}]}});
 assert.equal(c.hasError("jobs-r1"),true);
});


test("listEnvelope suppresses null, array, and primitive workflow evidence rows",()=>{
 const c=makeContext({state:{dataWarnings:[]}});
 const rows=c.listEnvelope({artifacts:[null,[],1,{id:7}]}, "artifacts");
 assert.deepEqual(JSON.parse(JSON.stringify(rows)),[{id:7}]);
 assert.deepEqual(JSON.parse(JSON.stringify(c.state.dataWarnings)),["artifacts: suppressed 3 malformed record(s)"]);
 for(const value of [null,{},[],{artifacts:null},{artifacts:{}}]){
  assert.equal(c.listEnvelope(value,"artifacts"),null);
 }
});

test("verification artifact renderer tolerates null and malformed artifact rows",()=>{
 const c=makeContext({state:{
  prs:[{number:28,head:{sha:"sha-28"}}],selectedPR:28,
  workflows:[{id:5,head_sha:"sha-28",path:".github/workflows/ci.yml",html_url:"https://github.com/run/5"}],
  artifacts:{"5":[null,[],false,{name:"coverage-xml-py3.11",id:8,expired:false}]}
 }});
 c.edgeCases=[];
 const nodes={"evidence-state":{},"verification-list":{innerHTML:""},"artifact-list":{innerHTML:""},"edge-list":{innerHTML:""}};
 c.$=id=>nodes[id]||(nodes[id]={innerHTML:"",textContent:"",value:""});
 c.setPill=()=>{};
 c.currentPR=()=>c.state.prs[0];
 c.latestForSha=()=>c.state.workflows[0];
 c.evidenceData=()=>({same:true,gates:[]});
 c.badge=x=>"<b>"+c.esc(x)+"</b>";
 c.link=()=>"<a>safe</a>";
 c.item=(title,description)=>"<div>"+c.esc(title)+" "+c.esc(description)+"</div>";
 const renderer=vm.runInContext("("+extractFunction("renderVerification")+")",c);
 assert.doesNotThrow(()=>renderer());
 assert.match(nodes["artifact-list"].innerHTML,/coverage-xml-py3\.11/);
 assert.equal(nodes["artifact-list"].innerHTML.includes("undefined"),false);
});

test("loadWorkflowDetails initializes corrupt cache containers before indexing",async()=>{
 const c=makeContext({state:{jobs:null,artifacts:[],artifactFinalized:null,errors:null}});
 c.gh=async path=>path.includes("/jobs")?{jobs:[null,{name:"job",conclusion:"success"}]}:{artifacts:[null,{name:"artifact",id:2}]};
 c.renderTarget=()=>{};
 c.renderPRs=()=>{};
 c.renderOverview=()=>{};
 c.renderVerification=()=>{};
 c.renderRelease=()=>{};
 await c.loadWorkflowDetails({id:42,status:"completed",head_sha:"abc"});
 assert.equal(c.state.jobs[42].length,1);
 assert.equal(c.state.jobs[42][0].name,"job");
 assert.equal(c.state.artifacts[42].length,1);
 assert.equal(c.state.artifacts[42][0].name,"artifact");
 assert.deepEqual(c.state.dataWarnings,undefined);
});
