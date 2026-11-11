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
 const names=["esc","safeHref","prLifecycle","prEvidence","isCiRun","latestForSha","matchesCommon","ghPages","evidenceData","hasError"];
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
 assert.equal(c.safeHref("https://preview.vercel.app"),"https://preview.vercel.app");
 assert.equal(c.safeHref("https://deploy.vercel.com"),"https://deploy.vercel.com");
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
 const c=makeContext({state:{workflows:[{name:"CI",head_sha:"abc123",status:"completed",conclusion:"success",updated_at:"2026-11-11T12:00:00Z"}]}});
 assert.equal(c.prEvidence({head:{sha:"abc123"}}),"UNVERIFIED");
 assert.equal(c.prEvidence({head:{sha:"missing"}}),"NOT RUN");
});
test("PR failure is explicit and successful evidence does not transfer across SHAs",()=>{
 const c=makeContext({state:{workflows:[
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
test("coverage names remain PENDING REVIEW even when artifacts are expired",()=>{
 const c=makeContext({state:{jobs:{"run-3":[]},artifacts:{"run-3":[{name:"coverage-xml"},{name:"coverage-json",expired:true}]}}});
 const g=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"run-3",head_sha:"same"}).gates.map(x=>[x.name,x.state]));
 assert.equal(g["coverage.xml artifact"],"PENDING REVIEW");assert.equal(g["coverage.json artifact"],"EXPIRED");
});
test("Docker and Node job success remains provisional",()=>{
 const c=makeContext({state:{jobs:{"run-4":[{name:"docker image build",conclusion:"success"},{name:"node container lifecycle",conclusion:"success"}]}}});
 const g=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"run-4",head_sha:"same"}).gates.map(x=>[x.name,x.state]));
 assert.equal(g["Docker image verification"],"PENDING REVIEW");assert.equal(g["In-container Node lifecycle"],"PENDING REVIEW");
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
  artifacts:{"run-y":[{name:"coverage-xml",expired:true}]},
  errors:[{key:"artifacts-run-y",message:"404 expired"}]
 }});
 const g=Object.fromEntries(c.evidenceData({head:{sha:"same"}},{id:"run-y",head_sha:"same"}).gates.map(x=>[x.name,x.state]));
 assert.equal(g["coverage.xml artifact"],"BLOCKED");
 const c2=makeContext({state:{artifacts:{"run-y":[{name:"coverage-xml",expired:true}]}}});
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
 const c=makeContext({state:{workflows:[
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
