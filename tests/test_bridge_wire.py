"""Real Python↔Node HTTP and disk contracts; the model is an explicit test double."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

from avrone import grokbot

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def node_peer(tmp_path, monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node runtime is unavailable")
    if int(subprocess.check_output([node,"--version"],text=True).strip().lstrip("v").split(".")[0]) < 22:
        pytest.skip("requires Node 22+")
    script = tmp_path / "peer.mjs"
    script.write_text('''
import { createServer } from "node:http";
import { setTimeout as delay } from "node:timers/promises";
import { createResearchHandler } from @HANDLER@;
import { emit } from @CODEX@;
let active=0, peak=0;
const handler=createResearchHandler({ token:()=>"wire-test-token", tools:[], emit,
  maxInFlight:4, runAgent:async(agent, message)=>{
    active++; peak=Math.max(active,peak);
    try {
      if(message==="slow") await delay(200);
      if(message==="fail") throw Error("private-provider-token");
      return message==="long" ? "x".repeat(13000) : "Test double report: "+message;
    } finally {active--;}
  }});
const server=createServer(async(req,res)=>{
  if(req.url==="/stats"){res.end(JSON.stringify({active,peak}));return;}
  const chunks=[];for await(const chunk of req) chunks.push(chunk);
  req.body=JSON.parse(Buffer.concat(chunks));
  res.status=n=>{res.statusCode=n;return res;};
  res.json=body=>{res.setHeader("content-type","application/json");res.end(JSON.stringify(body));};
  await handler(req,res);
});
server.listen(0,"127.0.0.1",()=>console.log(JSON.stringify({port:server.address().port})));
'''.replace("@HANDLER@",json.dumps((ROOT/"integrations/grok-bot/src/tools/research-bridge.js").as_uri()))
       .replace("@CODEX@",json.dumps((ROOT/"integrations/grok-bot/src/agents/codex.js").as_uri())))
    proc = subprocess.Popen([node,str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env={**os.environ,"RUNS_DIR":str(tmp_path/"runs")})
    try:
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("Node contract peer exited: " + proc.stderr.read()[:1500])
        port = json.loads(line)["port"]
        base = f"http://127.0.0.1:{port}"
        monkeypatch.setenv("AVRONE_GROKBOT_URL",base)
        monkeypatch.setenv("GROK_BRIDGE_TOKEN","wire-test-token")
        yield base, tmp_path/"runs"
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=5)
        proc.stdout.close();proc.stderr.close()


@pytest.mark.asyncio
async def test_real_wire_receipt_matches_full_answer_and_durable_codex(node_peer):
    _, runs = node_peer
    result = await grokbot.ask("long",correlation_id="wire-council")
    assert result["ok"] and len(result["answer"]) == 13000
    receipt = result["receipt"]
    assert receipt["answer_sha256"] == hashlib.sha256(result["answer"].encode()).hexdigest()
    events = [json.loads(line) for line in (runs/(receipt["run_id"]+".jsonl")).read_text().splitlines()]
    assert [event["kind"] for event in events] == ["run.start","run.done"]
    assert events[-1]["correlation_id"] == "wire-council"
    assert events[-1]["answer"] == result["answer"]


@pytest.mark.asyncio
async def test_real_wire_burst_enforces_admission_and_recovers(node_peer):
    base, _ = node_peer
    results = await asyncio.gather(*(grokbot.ask("slow",correlation_id=f"burst-{i}") for i in range(12)))
    assert any(r.get("ok") for r in results)
    assert any(r.get("error") == "HTTP 429" for r in results)
    async with httpx.AsyncClient(trust_env=False) as client:
        stats = (await client.get(base+"/stats")).json()
    assert stats["peak"] <= 4 and stats["active"] == 0
    assert (await grokbot.ask("healthy",correlation_id="after-burst"))["ok"]


@pytest.mark.asyncio
async def test_real_wire_model_error_is_sanitized_and_audited(node_peer):
    _, runs = node_peer
    result = await grokbot.ask("fail",correlation_id="wire-failure")
    assert not result["ok"] and result["error"] == "HTTP 502"
    events = [json.loads(line) for file in runs.glob("*.jsonl") for line in file.read_text().splitlines()]
    assert any(event["kind"] == "run.error" for event in events)
    assert "private-provider-token" not in json.dumps(events) + json.dumps(result)


@pytest.mark.asyncio
async def test_sustained_wire_bursts_keep_receipts_unique_and_audit_complete(node_peer):
    base, runs = node_peer
    accepted, rejected, ids = 0, 0, set()
    async with asyncio.timeout(45):
        for batch in range(20):
            results = await asyncio.gather(*(grokbot.ask("slow",correlation_id=f"soak-{batch}-{i}") for i in range(12)))
            for i, result in enumerate(results):
                if result.get("ok"):
                    accepted += 1
                    receipt = result["receipt"]
                    assert receipt["run_id"] not in ids
                    ids.add(receipt["run_id"])
                    assert receipt["correlation_id"] == f"soak-{batch}-{i}"
                    assert receipt["answer_sha256"] == hashlib.sha256(result["answer"].encode()).hexdigest()
                else:
                    rejected += 1
                    assert result["error"] == "HTTP 429"
    async with httpx.AsyncClient(trust_env=False) as client:
        stats = (await client.get(base+"/stats")).json()
    assert stats["active"] == 0 and stats["peak"] <= 4
    assert accepted + rejected == 240 and accepted > 0 and rejected > 0
    files = list(runs.glob("*.jsonl"))
    assert len(files) == accepted
    for file in files:
        events = [json.loads(line) for line in file.read_text().splitlines()]
        assert [event["kind"] for event in events] == ["run.start","run.done"]
