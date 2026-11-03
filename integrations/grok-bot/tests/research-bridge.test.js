import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createResearchHandler } from "../src/tools/research-bridge.js";

function reply() { return { statusCode: 200, status(c) { this.statusCode=c; return this; }, json(d) { this.body=d; return this; } }; }
const request = (body={message:"Check",correlation_id:"council-1"}, auth="Bearer test-token") => ({body,headers:{authorization:auth}});

test("bridge uses fixed researcher, read-only tools and correlated answer digest", async () => {
  const events=[]; let observed;
  const handler=createResearchHandler({token:()=>"test-token",emit:(...e)=>events.push(e),
    tools:[{name:"terminal"},{name:"avrone.squad"},{name:"team.spawn"},{name:"browser",run:a=>a}],
    runAgent:async(agent,message,tools,opts)=>{ observed={agent,tools,opts};
      assert.equal((await tools[0].run({action:"click",url:"https://example.com"})).action,"read");
      return "Independent research"; }});
  const res=reply();await handler(request(),res);
  assert.equal(res.body.ok,true);assert.deepEqual(observed.agent.tools,["browser","web.fetch"]);
  assert.deepEqual(observed.tools.map(t=>t.name),["browser"]);
  assert.equal(res.body.receipt.correlation_id,"council-1");
  assert.equal(res.body.receipt.answer_sha256,createHash("sha256").update(res.body.answer).digest("hex"));
  assert.equal(events.at(-1)[1],"run.done");
});

test("bridge fails closed on absent or incorrect credentials before inference", async () => {
  for(const [token,auth,code] of [["","",503],["test-token","bad-token",401]]){
    const handler=createResearchHandler({token:()=>token,tools:[],emit:()=>{},runAgent:()=>{throw Error("must not run");}});
    const res=reply();await handler(request(undefined,auth),res);assert.equal(res.statusCode,code);
  }
});

test("invalid tasks and correlation ids never invoke inference", async () => {
  const handler=createResearchHandler({token:()=>"test-token",tools:[],emit:()=>{},runAgent:()=>{throw Error("must not run");}});
  for(const body of [{message:{}},{message:""},{message:"x".repeat(8001)},{message:"x",correlation_id:"../stale"}]){
    const res=reply();await handler(request(body),res);assert.equal(res.statusCode,400);
  }
});

test("errors and step exhaustion cannot become successful research receipts", async () => {
  for(const runAgent of [async()=>{throw Error("private-secret");},async()=>"Reached max tool steps without a final answer."]){
    const handler=createResearchHandler({token:()=>"test-token",tools:[],emit:()=>{},runAgent});
    const res=reply();await handler(request(),res);assert.equal(res.statusCode,502);
    assert.equal(res.body.ok,undefined);assert.ok(!JSON.stringify(res.body).includes("private-secret"));
  }
});
