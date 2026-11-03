import test from "node:test";
import assert from "node:assert/strict";
import { createAvroneTool } from "../src/tools/avrone.js";
import { createResearchHandler } from "../src/tools/research-bridge.js";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const good = () => new Response(JSON.stringify({ run_id:"run-1", answer:"Independent answer" }));
const res = () => ({statusCode:200,status(n){this.statusCode=n;return this;},json(body){this.body=body;return this;}});
const req = body => ({headers:{authorization:"Bearer test-token"},body:body||{message:"Check",correlation_id:"council-1"}});

test("forward adapter rejects invalid argument shapes and prompt values without I/O", async()=>{
  let calls=0;
  const tool=createAvroneTool({getBaseUrl:()=>"http://avrone:8710",fetchImpl:async()=>{calls++;return good();}});
  for(const args of [null,[],"prompt",true,{prompt:{}},{prompt:12},{prompt:"Check",mode:"shell"}]){
    assert.ok((await tool.run(args)).error);
  }
  assert.equal(calls,0);
});

test("forward adapter never normalizes controls and backslashes into a credential destination",async()=>{
  for(const origin of ["http://avrone:\n8710","http://avrone\t:8710","http://avrone\\evil","http://avrone:8710?","http://avrone:8710#"]){
    let called=false;
    const tool=createAvroneTool({getBaseUrl:()=>origin,fetchImpl:async()=>{called=true;return good();}});
    assert.ok((await tool.run({prompt:"Check"})).error);assert.equal(called,false);
  }
});

test("forward adapter refuses nonfinite JSON, invalid Unicode and service failure answers",async()=>{
  for(const body of ['{"run_id":"r","answer":"x","metric":1e999}',
    '{"run_id":"r","answer":"\\ud800"}',JSON.stringify({run_id:"r",answer:" \nModel backend error: unavailable"}),
    JSON.stringify({run_id:"r",answer:"summary",agents:[]}),
    JSON.stringify({run_id:"r",answer:"summary",agents:[{result:"[Worker\n[One] failed: unavailable]"}]}),
    ...[{ok:false},{ok:null},{state:"HOLD"},{status:"running"},{status:{}},{mode:"chat"}]
      .map(extra=>JSON.stringify({run_id:"r",answer:"summary",...extra}))]){
    const tool=createAvroneTool({getBaseUrl:()=>"http://avrone:8710",fetchImpl:async()=>new Response(body)});
    assert.ok((await tool.run({prompt:"Check"})).error);
  }
});

test("stream exceptions cannot leak upstream details",async()=>{
  const tool=createAvroneTool({getBaseUrl:()=>"http://avrone:8710",fetchImpl:async()=>new Response(new ReadableStream({start(c){c.error(Error("private-stream-token"));}}))});
  const result=await tool.run({prompt:"Check"});
  assert.ok(result.error);assert.ok(!JSON.stringify(result).includes("private-stream-token"));
});

test("research bridge rejects array credentials and malformed Unicode before inference",async()=>{
  let calls=0;
  const handler=createResearchHandler({token:()=>"test-token",tools:[],emit:()=>{},runAgent:async()=>{calls++;return "answer";}});
  const invalid=req();invalid.headers.authorization=["Bearer test-token"];
  const a=res();await handler(invalid,a);assert.equal(a.statusCode,401);
  const b=res();await handler(req({message:"\ud800"}),b);assert.equal(b.statusCode,400);
  assert.equal(calls,0);
});

test("both adapters count Unicode code points consistently",async()=>{
  const text="🚀".repeat(8000);
  const handler=createResearchHandler({token:()=>"test-token",tools:[],emit:()=>{},runAgent:async()=>"answer"});
  const reply=res();await handler(req({message:text}),reply);assert.equal(reply.statusCode,200);
  const tool=createAvroneTool({getBaseUrl:()=>"http://avrone:8710",fetchImpl:async()=>good()});
  assert.equal((await tool.run({prompt:text})).ok,true);
});

test("research bridge enforces concurrent admission and releases failed slots",async()=>{
  let release,entered=0;
  const gate=new Promise(resolve=>{release=resolve;});
  const handler=createResearchHandler({maxInFlight:2,token:()=>"test-token",tools:[],emit:()=>{},runAgent:async()=>{entered++;await gate;throw Error("private-failure");}});
  const a=res(),b=res(),third=res();
  const one=handler(req(),a),two=handler(req(),b);
  const three=handler(req(),third);
  try { assert.equal(third.statusCode,429);assert.equal(entered,2); }
  finally { release();await Promise.all([one,two,three]); }
  assert.equal(a.statusCode,502);
  const next=res();await handler(req(),next);assert.equal(next.statusCode,502);assert.equal(entered,3);
});

test("failed audit writes and throwing audit sinks never produce successful receipts",async()=>{
  for(const emit of [()=>false,()=>{throw Error("private-audit-token");}]){
    let calls=0;
    const handler=createResearchHandler({token:()=>"test-token",tools:[],emit,runAgent:async()=>{calls++;return "answer";}});
    const reply=res();await handler(req(),reply);
    assert.equal(calls,0);assert.equal(reply.statusCode,502);
    assert.ok(!reply.body.receipt);assert.ok(!JSON.stringify(reply.body).includes("private-audit-token"));
  }
});

test("audit completion failure withholds a computed answer",async()=>{
  const handler=createResearchHandler({token:()=>"test-token",tools:[],emit:(_,kind)=>kind!=="run.done",runAgent:async()=>"answer"});
  const reply=res();await handler(req(),reply);assert.equal(reply.statusCode,502);assert.ok(!reply.body.receipt);
});

test("real CodeX storage failure returns an explicit failure acknowledgement",async()=>{
  const directory=mkdtempSync(join(tmpdir(),"avrone-codex-"));const blocked=join(directory,"not-a-directory");
  writeFileSync(blocked,"keep");const before=process.env.RUNS_DIR;const warn=console.warn;
  try{
    process.env.RUNS_DIR=blocked;console.warn=()=>{};
    const codex=await import("../src/agents/codex.js?edge="+Date.now());
    assert.equal(codex.emit("research-test","run.start"),false);
  }finally{
    if(before===undefined)delete process.env.RUNS_DIR;else process.env.RUNS_DIR=before;
    console.warn=warn;rmSync(directory,{recursive:true,force:true});
  }
});

test("oversized declared bodies are cancelled before reading",async()=>{
  let cancelled=false;
  const tool=createAvroneTool({getBaseUrl:()=>"http://avrone:8710",fetchImpl:async()=>new Response(new ReadableStream({cancel(){cancelled=true;}}),{headers:{"content-length":"999999"}})});
  assert.match((await tool.run({prompt:"Check"})).error,/64 KiB/);assert.equal(cancelled,true);
});

test("malformed UTF-8 and response lengths are rejected",async()=>{
  const bodies=[new Response(new Uint8Array([123,34,255,34,58,49,125])),
    ...["-1","NaN","1e9","9".repeat(40)].map(n=>new Response("{}",{headers:{"content-length":n}}))];
  for(const body of bodies){
    const tool=createAvroneTool({getBaseUrl:()=>"http://avrone:8710",fetchImpl:async()=>body});
    assert.ok((await tool.run({prompt:"Check"})).error);
  }
});

test("deterministic argument corpus never invokes inference for malformed prompts",async()=>{
  let calls=0;const invalid=[null,[],{},true,12,NaN,"", "   ","\ud800"];
  const tool=createAvroneTool({getBaseUrl:()=>"http://avrone:8710",fetchImpl:async()=>{calls++;return good();}});
  for(let i=0;i<1000;i++)assert.ok((await tool.run({prompt:invalid[i%invalid.length]})).error);
  assert.equal(calls,0);
});

test("configuration failures do not strand a research admission slot",async()=>{
  const handler=createResearchHandler({maxInFlight:1,token:()=>"test-token",tools:null,emit:()=>{},runAgent:async()=>"answer"});
  for(let i=0;i<3;i++){
    const reply=res();await handler(req(),reply);assert.equal(reply.statusCode,502);
  }
  const unavailable=createResearchHandler({token:()=>{throw Error("private-config");},tools:[],emit:()=>{},runAgent:async()=>"answer"});
  const reply=res();await unavailable(req(),reply);assert.equal(reply.statusCode,503);assert.ok(!JSON.stringify(reply.body).includes("private-config"));
});
