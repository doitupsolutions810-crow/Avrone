(function () {
"use strict";
const OWNER = "doitupsolutions810-crow";
const REPO = "Avrone";
const REPO_FULL = OWNER + "/" + REPO;
const GH = "https://api.github.com/repos/" + REPO_FULL;
const $ = (id) => document.getElementById(id);
const state = { prs: [], commits: [], workflows: [], status: null, runs: [], checkpoints: [], telemetry: null, fleet: [], evolve: null, commitStatus: null, commitStatusSha: "", selectedPR: 27, runDetails: {}, artifacts: {}, jobs: {}, adminToken: "", errors: [] };
const edgeCases = [
 ["Duplicate lane assignment","Reject a second live owner for the same exclusive lane; preserve the original lease."],
 ["Lease expiry / stale owner","Fence a stale worker from heartbeat renewal, checkpoint writes, and finalization."],
 ["Checkpoint collision","Concurrent claims yield one owner; incompatible task/mode/owner returns conflict."],
 ["Worker exception","One agent crash is isolated; sibling results and the final failure summary survive."],
 ["Failure propagation","Planner, worker, lead, runner, and cancellation failures remain visible with causal chain."],
 ["Malformed and torn JSONL","Replay survives invalid UTF-8, truncated last line, NaN, lone surrogates, and cycles."],
 ["Path traversal and symlinks","File/workspace/artifact paths cannot escape their authorized root."],
 ["Resource exhaustion","Worker caps, request bounds, memory limits, timeouts, and log replay limits fail closed."],
 ["Exact-SHA binding","Results from a parent, sibling, or newer commit cannot verify the selected SHA."],
 ["Missing / expired artifacts","Absent, expired, corrupt, or mismatched coverage artifacts remain NOT RUN."],
 ["Docker unavailable","No Docker image gate is passed if engine, build, inspect, or digest evidence is missing."],
 ["Node lifecycle","Verify install, in-container start, health, graceful shutdown, and no leaked processes."],
 ["Secret redaction","Admin tokens, provider keys, URL credentials, and log-shaped secrets never render."],
 ["API partial failure","Rate limits, 401/403, 404, 5xx, malformed JSON, and pagination gaps remain visible."],
 ["Untrusted pull-request data","PR text and event payloads are rendered as text, never as executable HTML."]
];
function esc(v) { return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c])); }
function short(v, n) { const s=String(v||""); return s.length>(n||12) ? s.slice(0,n||12)+"…" : s; }
function date(v) { if(!v) return "—"; const d=new Date(v); return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString(); }
function badge(value) {
 const s=String(value||"UNKNOWN").toUpperCase();
 const kind=["VERIFIED","PASS","SUCCESS","MERGED","DONE","COMPLETED"].includes(s)?"good":(["FAIL","FAILURE","BLOCKED","ERROR","TIMED_OUT"].includes(s)?"fail":(["UNVERIFIED","PENDING","IN_PROGRESS","QUEUED","RUNNING"].includes(s)?"pending":"neutral"));
 return '<span class="pill '+kind+'">'+esc(s)+'</span>';
}
function safeHref(value) { try { const u=new URL(String(value||""),location.href); const current=new URL(location.href); const trustedHost=u.hostname==="github.com"||u.hostname.endsWith(".github.com")||u.hostname==="vercel.com"||u.hostname.endsWith(".vercel.com")||u.hostname.endsWith(".vercel.app"); return u.protocol==="https:"&&(u.origin===current.origin||trustedHost)?u.href:"#"; } catch (_) { return "#"; } }
function link(url,label) { return '<a href="'+esc(safeHref(url))+'" target="_blank" rel="noopener noreferrer">'+esc(label||"Open ↗")+'</a>'; }
function showEmpty(id,msg) { $(id).innerHTML='<div class="empty">'+esc(msg)+'</div>'; }
function apiHeaders() { const h={Accept:"application/json"}; if(state.adminToken) h["x-admin-token"]=state.adminToken; return h; }
async function jsonFetch(url,opts) {
 const ctl=new AbortController(); const timer=setTimeout(()=>ctl.abort(),12000);
 try {
  const res=await fetch(url,Object.assign({headers:apiHeaders(),signal:ctl.signal},opts||{}));
  const txt=await res.text(); let body;
  try { body=txt?JSON.parse(txt):null; } catch (_) { throw new Error("Invalid JSON from "+url); }
  if(!res.ok) throw new Error(res.status+" "+res.statusText+(body&&body.detail?": "+body.detail:""));
  return body;
 } finally { clearTimeout(timer); }
}
async function gh(path) {
 return jsonFetch(GH+path,{headers:{Accept:"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}});
}
async function ghPages(path,maxPages) {
 const all=[];let next=GH+path,pages=0;const cap=maxPages||5;
 while(next&&pages<cap){
  const ctl=new AbortController();const timer=setTimeout(()=>ctl.abort(),12000);
  let res;
  try{res=await fetch(next,{headers:{Accept:"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"},signal:ctl.signal});}
  finally{clearTimeout(timer);}
  const txt=await res.text();let data;
  try{data=txt?JSON.parse(txt):null;}catch(_){throw new Error("Invalid JSON from GitHub page "+(pages+1));}
  if(!res.ok)throw new Error(res.status+" "+res.statusText+(data&&data.message?": "+data.message:""));
  const rows=Array.isArray(data)?data:(data&&Array.isArray(data.workflow_runs)?data.workflow_runs:[]);
  all.push(...rows);pages++;
  const header=res.headers.get("Link")||"";
  const match=header.split(",").map(x=>x.trim()).find(x=>/rel="next"/.test(x));
  const candidate=match?(match.match(/<([^>]+)>/)||[])[1]||null:null;
  if(candidate) {
   let parsed;
   try{parsed=new URL(candidate,GH);}catch(_){throw new Error("Invalid GitHub pagination URL");}
   if(parsed.protocol!=="https:"||parsed.hostname!=="api.github.com") throw new Error("Rejected unsafe GitHub pagination URL");
   next=parsed.href;
  } else next=null;
 }
 return {items:all,pages,truncated:!!next};
}
async function local(path) { return jsonFetch(path); }
function prLifecycle(p) {
 return {state:String(p.state||"unknown").toUpperCase(),draft:!!p.draft,merged:!!p.merged_at||!!p.merged,unmerged:!p.merged_at&&!p.merged};
}
function prEvidence(p) {
 const sha=(p.head&&p.head.sha)||"";
 if(hasError("workflows")) return "BLOCKED";
 const run=sha&&latestForSha(sha);
 if(!sha||!run) return "NOT RUN";
 if(run.conclusion==="failure"||run.conclusion==="cancelled"||run.conclusion==="timed_out") return "FAIL";
 if(run.status!=="completed") return "UNVERIFIED";
 return "UNVERIFIED";
}
function latestForSha(sha) { return state.workflows.filter(w=>w.head_sha===sha).sort((a,b)=>new Date(b.updated_at)-new Date(a.updated_at))[0]||null; }
function currentPR() { return state.prs.find(p=>p.number===Number(state.selectedPR))||state.prs.find(p=>p.number===27)||null; }
function runtimeStatusText() { return state.status ? "CONNECTED" : "NOT CONNECTED"; }
function localUnavailableMessage(key) { const e=state.errors.find(x=>x.key===key); return e ? e.message : "No data returned by the local API."; }
function hasError(key) { return state.errors.some(x=>x.key===key); }
function fact(label,value) { return '<div class="fact"><span>'+esc(label)+'</span><b>'+esc(value==null||value===""?"Not reported":value)+'</b></div>'; }
function formatSha(sha) { return sha ? short(sha,16) : "No SHA"; }
function currentSha() { const p=currentPR(); return p&&p.head&&p.head.sha||""; }
async function loadSelectedCommitStatus() {
 const sha=currentSha();state.commitStatus=null;state.commitStatusSha=sha;
 if(!sha)return;
 try{state.commitStatus=await gh("/commits/"+encodeURIComponent(sha)+"/status");}
 catch(e){state.errors.push({key:"commit-status",message:e.message});}
}
function filterValue(id) { return ($(id).value||"").trim().toLowerCase(); }
function activeFilters() {
 return {repo:filterValue("f-repo"),pr:filterValue("f-pr"),commit:filterValue("f-commit"),round:filterValue("f-round"),lane:filterValue("f-lane"),agent:filterValue("f-agent"),runtime:($("f-runtime").value||"").trim(),verification:($("f-verification").value||"").trim()};
}
function matchesCommon(obj, f) {
 const text=JSON.stringify(obj||{}).toLowerCase();
 if(f.repo && !REPO_FULL.toLowerCase().includes(f.repo)) return false;
 if(f.pr && (obj.number!=null||obj.pr_number!=null) && String(obj.number||obj.pr_number)!==f.pr.replace(/^#/,"")) return false;
 if(f.commit && !(String(obj.head_sha||obj.sha||obj.target_sha||"").toLowerCase().includes(f.commit)||text.includes(f.commit))) return false;
 if(f.round && !text.includes(f.round)) return false;
 if(f.lane && !text.includes(f.lane)) return false;
 if(f.agent && !text.includes(f.agent)) return false;
 if(f.runtime==="Python 3.11" && !/3\.11/.test(text)) return false;
 if(f.runtime==="Python 3.12" && !/3\.12/.test(text)) return false;
 if(f.runtime==="Node" && !/node/.test(text)) return false;
 if(f.runtime==="Docker" && !/docker/.test(text)) return false;
 if(f.runtime==="Apple Silicon" && !/(arm64|aarch64|apple silicon)/.test(text)) return false;
 if(f.verification) {
   const raw=String(obj.evidence_status||obj.verification||obj.conclusion||"").toUpperCase();
   const ev=raw==="SUCCESS"?"PASS":(["FAILURE","CANCELLED","TIMED_OUT","ACTION_REQUIRED"].includes(raw)?"FAIL":raw);
   if(ev!==f.verification && !(f.verification==="UNVERIFIED" && !ev)) return false;
 }
 return true;
}
function setPill(id,text,kind) { $(id).textContent=text; $(id).className="pill "+(kind||"neutral"); }
function renderTarget() {
 const p=currentPR();
 if(!p){$("target-pr").textContent="PR target unavailable";$("target-sha").textContent="No observed PR SHA";return;}
 $("target-pr").textContent="PR #"+p.number+" · "+(p.head&&p.head.ref||"unknown branch");
 $("target-sha").textContent=(p.head&&p.head.sha)||"No head SHA";
 const life=prLifecycle(p);
 $("target-state").innerHTML=badge(life.state)+badge(life.draft?"DRAFT":"READY")+badge(life.merged?"MERGED":"UNMERGED")+badge(prEvidence(p));
}
function renderMetrics() {
 const open=state.prs.filter(p=>p.state==="open");
 $("m-open").textContent=open.length;
 $("m-draft").textContent=open.filter(p=>p.draft&&!p.merged_at).length;
 $("m-runs").textContent=state.workflows.length;
 $("m-local").textContent=Array.isArray(state.runs)&&!hasError("runs")?state.runs.length:"—";
 $("m-checkpoints").textContent=Array.isArray(state.checkpoints)?state.checkpoints.length:"—";
 const agents=state.status&&state.status.agents?Object.keys(state.status.agents).length:0;
 $("m-agents").textContent=agents;
}
function item(title,body,meta,href) {
 return '<article class="item '+(href?"clickable":"")+'" '+(href?'data-href="'+esc(safeHref(href))+'"':'')+'><div class="item-head"><div><h3>'+esc(title)+'</h3><p>'+esc(body||"")+'</p></div></div><div class="meta">'+(meta||"")+'</div></article>';
}
function renderPRs() {
 const f=activeFilters();
 const rows=state.prs.filter(p=>matchesCommon(p,f));
 $("pr-count").textContent=rows.length+" records";
 if(!rows.length) return showEmpty("pull-list","No pull requests match these filters.");
 const body=rows.map(p=>{
  const life=prLifecycle(p), ev=prEvidence(p), sha=p.head&&p.head.sha||"";
  return '<tr><td><b>#'+p.number+'</b> '+esc(p.title)+'</td><td>'+badge(life.state)+" "+badge(life.draft?"DRAFT":"READY")+'</td><td>'+badge(life.merged?"MERGED":"UNMERGED")+'</td><td>'+badge(ev)+'</td><td><code>'+esc(formatSha(sha))+'</code></td><td>'+esc(p.head&&p.head.ref||"")+'</td><td>'+link(p.html_url,"Open ↗")+'</td></tr>';
 }).join("");
 $("pull-list").innerHTML='<table class="data-table"><thead><tr><th>Pull request</th><th>GitHub state</th><th>Merge state</th><th>Evidence</th><th>Head SHA</th><th>Branch</th><th>Link</th></tr></thead><tbody>'+body+'</tbody></table>';
}
function renderCommits() {
 const f=activeFilters();
 const rows=state.commits.filter(c=>matchesCommon({sha:c.sha,message:c.commit&&c.commit.message},f));
 $("commit-count").textContent=rows.length+" records";
 if(!rows.length) return showEmpty("commit-list","No commits match these filters.");
 $("commit-list").innerHTML='<table class="data-table"><thead><tr><th>Commit</th><th>Message</th><th>Author</th><th>Date</th><th>Link</th></tr></thead><tbody>'+rows.map(c=>'<tr><td><code>'+esc(formatSha(c.sha))+'</code></td><td>'+esc((c.commit&&c.commit.message||"").split("\n")[0])+'</td><td>'+esc(c.commit&&c.commit.author&&c.commit.author.name||"")+'</td><td>'+esc(date(c.commit&&c.commit.author&&c.commit.author.date))+'</td><td>'+link(c.html_url||c.url,"Open ↗")+'</td></tr>').join("")+'</tbody></table>';
 const options=state.commits.map(c=>'<option value="'+esc(c.sha)+'">'+esc(formatSha(c.sha)+" · "+(c.commit&&c.commit.message||"").split("\n")[0].slice(0,48))+'</option>').join("");
 $("compare-base").innerHTML='<option value="">Base commit…</option>'+options;
 $("compare-head").innerHTML='<option value="">Head commit…</option>'+options;
}
function renderOverview() {
 const top=state.prs.filter(p=>p.state==="open").slice(0,7);
 $("overview-prs").innerHTML=top.length?top.map(p=>item("#"+p.number+" · "+p.title,(p.head&&p.head.ref||"")+" → "+(p.base&&p.base.ref||"main"),badge(p.draft?"DRAFT":"READY")+" "+badge(prEvidence(p)),p.html_url)).join(""):'<div class="empty">No open pull requests observed.</div>';
 const recent=state.commits.slice(0,6);
 $("overview-commits").innerHTML=recent.length?recent.map(c=>item(formatSha(c.sha),(c.commit&&c.commit.message||"").split("\n")[0],'<span class="muted">'+esc(date(c.commit&&c.commit.author&&c.commit.author.date))+'</span>',c.html_url)).join(""):'<div class="empty">No commits available.</div>';
 const s=state.status||{};
 $("runtime-summary").innerHTML=state.status?[
 fact("Runner",s.runner),fact("Provider",s.provider),fact("Model",s.model),fact("Workspace",s.workspace),fact("Squad size",s.squad_size),fact("Max parallel",s.max_parallel),fact("Admin enabled",s.admin_enabled?"yes":"no"),fact("Boot ID",s.boot)
 ].join(""):'<div class="empty">'+esc(localUnavailableMessage("status"))+'</div>';
 setPill("runtime-state",state.status?"CONNECTED":"NOT CONNECTED",state.status?"good":"blocked");
 renderNextGate();
}
function renderNextGate() {
 const p=currentPR(), sha=p&&p.head&&p.head.sha, run=sha&&latestForSha(sha);
 const data=evidenceData(p,run);
 const next=data.gates.find(g=>g.state!=="PASS");
 if(!next) {
  $("next-gate").innerHTML='<span class="pill good">ALL LISTED GATES PASS</span><b>Manual release review remains</b><p>Verify artifacts, provenance, approvals, and browser evidence before changing PR lifecycle.</p>';
  return;
 }
 const vercelStatus=state.commitStatusSha===sha&&state.commitStatus?(state.commitStatus.statuses||[]).find(x=>String(x.context||"").toLowerCase()==="vercel"):null;
 const href=next.name==="Vercel preview deployment"&&vercelStatus&&vercelStatus.target_url?vercelStatus.target_url:(run&&run.html_url);
 const linkHtml=href?link(href,next.name==="Vercel preview deployment"?"Inspect deployment ↗":"Inspect workflow ↗"):"";
 $("next-gate").innerHTML=badge(next.state)+'<b>'+esc(next.name)+'</b><p>'+esc(next.note)+'</p>'+linkHtml;
}
function renderMissions() {
 $("checkpoint-state").textContent=Array.isArray(state.checkpoints)?"CONNECTED":"NOT CONNECTED";
 $("mission-list").innerHTML=Array.isArray(state.checkpoints)&&state.checkpoints.length?state.checkpoints.map(c=>item(c.id||c.checkpoint_id||"Checkpoint", "Run: "+(c.run_id||"unreported")+" · mode: "+(c.mode||"unreported")+" · owner: "+(c.owner||"unreported"),badge(c.status||"RESUMABLE")+" "+'<span class="muted">'+esc(date(c.updated||c.started))+'</span>')).join(""):'<div class="empty">'+esc(localUnavailableMessage("checkpoints"))+' · No synthetic mission records are shown.</div>';
 const accepted=state.evolve&&state.evolve.accepted;
 $("proposal-list").innerHTML=Array.isArray(accepted)&&accepted.length?accepted.map(p=>item(p.name||p.id||"Accepted proposal",p.description||p.summary||"Accepted proposal record",badge("ACCEPTED")+" "+badge(p.validated?"PASS":"UNVERIFIED"))).join(""):'<div class="empty">'+esc(localUnavailableMessage("evolve"))+' · Pending proposal validations are not exposed by the current API.</div>';
}
function renderSwarm() {
 const runsAvailable=Array.isArray(state.runs)&&!hasError("runs");
 setPill("swarm-state",runsAvailable?"CONNECTED":"NOT CONNECTED",runsAvailable?"good":"blocked");
 const f=activeFilters();
 const runs=(Array.isArray(state.runs)?state.runs:[]).filter(r=>matchesCommon(r,f));
 $("swarm-list").innerHTML=runs.length?runs.map(r=>{
  const id=r.run_id||r.id||"unknown", mode=r.mode||"mode unreported";
  return '<article class="item clickable" data-run="'+esc(id)+'"><div class="item-head"><div><h3>'+esc(id)+'</h3><p>'+esc(mode)+' · '+esc(date(r.start||r.started||r.created))+'</p></div>'+badge(r.status||"HISTORY")+'</div><div class="meta">'+badge(r.evidence_status||"UNVERIFIED")+'<span class="muted">Select to inspect event replay</span></div></article>';
 }).join(""):'<div class="empty">'+esc(localUnavailableMessage("runs"))+'</div>';
 const p=currentPR(), sha=p&&p.head&&p.head.sha;
 const ci=sha?state.workflows.filter(w=>w.head_sha===sha):state.workflows.slice(0,8);
 setPill("ci-state",ci.length?"EXACT SHA":"NO MATCH",ci.length?"pending":"neutral");
 $("ci-list").innerHTML=ci.length?ci.map(w=>item("Run #"+w.run_number+" · "+w.name, "SHA "+formatSha(w.head_sha)+" · "+date(w.updated_at),badge(w.status==="completed"?(w.conclusion||"COMPLETED"):w.status),w.html_url)).join(""):'<div class="empty">No workflow run for the selected PR head SHA. No result is inferred from another commit.</div>';
 const sorted=state.workflows.slice().sort((a,b)=>new Date(b.created_at||b.updated_at)-new Date(a.created_at||a.updated_at)).slice(0,40);
 const options=sorted.map(w=>'<option value="'+esc(w.id)+'">#'+esc(w.run_number)+' · '+esc(w.name)+' · '+esc(short(w.head_sha,10))+' · '+esc(w.conclusion||w.status)+'</option>').join("");
 $("compare-run-a").innerHTML='<option value="">Run A…</option>'+options;
 $("compare-run-b").innerHTML='<option value="">Run B…</option>'+options;
}
function renderAgents() {
 const active=state.status&&state.status.agents?state.status.agents:{};
 const agentRows=Object.entries(active);
 const laneCounts={};
 agentRows.forEach(([,a])=>{const lane=String(a.lane||a.focus_lane||"").trim();if(lane&&String(a.status||"").toLowerCase()==="working")laneCounts[lane]=(laneCounts[lane]||0)+1;});
 $("agent-list").innerHTML=agentRows.length?agentRows.map(([name,a])=>{
  const lane=String(a.lane||a.focus_lane||"").trim();
  const duplicate=lane&&laneCounts[lane]>1;
  const workspace=a.agent_dir||a.workspace||a.workspace_path||"Workspace not reported";
  return item(name,a.role||a.focus||a.task||"No focus reported",badge(a.status||"UNKNOWN")+(duplicate?badge("DUPLICATE LANE OBSERVED"):"")+' <span class="muted">'+esc(a.agent_id||"No agent ID")+'</span> · <span class="muted">'+esc(lane||"Lane not reported")+'</span> · <span class="muted">'+esc(workspace)+'</span>');
 }).join(""):'<div class="empty">'+esc(localUnavailableMessage("status"))+' · No live agents are asserted.</div>';
 const rows=Array.isArray(state.fleet)?state.fleet:[];
 $("runner-list").innerHTML=rows.length?rows.map(n=>{
  const env=n.env||{}, dev=env.device||{}, ru=env.runner||{}, container=ru.container||{};
  const containerDetails=[container.name,container.id,container.status,container.image].filter(Boolean).join(" · ")||"Container not reported";
  const leaseDetails=[n.lease_owner,n.lease_until,n.heartbeat_at].filter(Boolean).join(" · ")||"Lease / heartbeat fields not reported";
  return item(n.node_id||"Fleet node", (dev.hostname||"host unknown")+" · "+(dev.os||"OS unknown")+" · "+(dev.arch||"architecture unreported"),badge(n.status||"UNKNOWN")+' <span class="muted">'+esc(ru.kind||"runner unknown")+'</span> <span class="muted">'+esc(containerDetails)+'</span> <span class="muted">'+esc(leaseDetails)+'</span>');
 }).join(""):'<div class="empty">'+esc(localUnavailableMessage("fleet"))+' · Fleet may be disabled or admin access may be required.</div>';
 const s=state.status||{}, t=state.telemetry||{};
 $("resource-list").innerHTML=[fact("Max parallel",s.max_parallel),fact("Squad size",s.squad_size),fact("Runner",s.runner),fact("Workspace",s.workspace),fact("24h runs",t.runs&&t.runs.total),fact("Spawned agents",t.agents&&t.agents.spawned),fact("Errors",t.errors),fact("Fleet nodes",Array.isArray(t.fleet_nodes)?t.fleet_nodes.length:"not reported"),fact("CPU / memory quota","Not exposed by current API"),fact("Lease owner / expiry","Not exposed by current API"),fact("Heartbeat age","Not exposed unless fleet presence reports it"),fact("Apple Silicon","Not observed for local host")].join("");
}
function evidenceData(p,run) {
 const sha=p&&p.head&&p.head.sha||"";
 const same=!!run&&run.head_sha===sha;
 const jobs=run&&state.jobs[run.id]||[];
 const artifacts=run&&state.artifacts[run.id]||[];
 const jobsBlocked=!!run&&hasError("jobs-"+run.id);
 const artifactsBlocked=!!run&&hasError("artifacts-"+run.id);
 const names=artifacts.map(a=>({name:String(a.name||"").toLowerCase(),expired:!!a.expired}));
 const job=(rx)=>same?(jobs.find(j=>rx.test(String(j.name||"").toLowerCase()))||null):null;
 const j311=job(/3\.11/), j312=job(/3\.12/);
 const docker=job(/docker/), node=job(/node.*(lifecycle|container)|(lifecycle|container).*node/);
 const candidate=(rx)=>names.some(a=>rx.test(a.name)&&!a.expired);
 const expired=(rx)=>names.some(a=>rx.test(a.name)&&a.expired);
 const vercelStatus=state.commitStatusSha===sha&&state.commitStatus?(state.commitStatus.statuses||[]).find(x=>String(x.context||"").toLowerCase()==="vercel"):null;
 const workflowBlocked=hasError("workflows");
 const commitStatusBlocked=hasError("commit-status");
 const pythonState=(job)=>workflowBlocked||jobsBlocked?"BLOCKED":!same||!job?"NOT RUN":job.conclusion==="success"?"PASS":job.conclusion==="failure"?"FAIL":"UNVERIFIED";
 const artifactState=(rx)=>artifactsBlocked?"BLOCKED":!same?"NOT RUN":candidate(rx)?"PENDING REVIEW":expired(rx)?"EXPIRED":"NOT RUN";
 const runtimeJobState=(job)=>jobsBlocked?"BLOCKED":!same||!job?"NOT RUN":job.conclusion==="failure"?"FAIL":job.conclusion==="success"?"PENDING REVIEW":"UNVERIFIED";
 const gates=[
  {name:"Exact-SHA Python 3.11",state:pythonState(j311),note:"Required matrix leg for this exact commit."},
  {name:"Exact-SHA Python 3.12",state:pythonState(j312),note:"Required matrix leg for this exact commit."},
  {name:"Vercel preview deployment",state:commitStatusBlocked?"BLOCKED":!vercelStatus?"NOT RUN":vercelStatus.state==="failure"?"FAIL":vercelStatus.state==="success"?"PENDING REVIEW":vercelStatus.state==="pending"?"UNVERIFIED":"NOT RUN",note:"A successful deployment status still requires an inspectable preview URL and a browser smoke test."},
  {name:"coverage.xml artifact",state:artifactState(/coverage.*xml|xml.*coverage/),note:"Artifact metadata is not content validation; inspect file and exact-SHA provenance."},
  {name:"coverage.json artifact",state:artifactState(/coverage.*json|json.*coverage/),note:"Artifact metadata is not content validation; inspect file and exact-SHA provenance."},
  {name:"Docker image verification",state:runtimeJobState(docker),note:"Requires image build, inspect, digest and provenance; job name alone is insufficient."},
  {name:"In-container Node lifecycle",state:runtimeJobState(node),note:"Requires install, start, health, graceful shutdown and process-leak evidence from inside the container."},
  {name:"Local Apple-Silicon Ollama squad",state:state.status?"NOT RUN":"BLOCKED",note:"Must run on the authorized Mac with the actual configured Ollama model and isolated worktree."},
  {name:"Proposal validation & edge cases",state:jobsBlocked?"BLOCKED":!same?"NOT RUN":jobs.some(j=>/proposal validation|edge-case/i.test(j.name||"")&&j.conclusion==="success")?"PENDING REVIEW":"NOT RUN",note:"Requires an explicit validation job and independently inspectable exact-SHA results."}
 ];
 return {sha,same,gates};
}
function renderVerification() {
 const p=currentPR(), sha=p&&p.head&&p.head.sha, run=sha&&latestForSha(sha);
 const data=evidenceData(p,run);
 const failed=data.gates.some(g=>g.state==="FAIL");
 const complete=data.gates.every(g=>g.state==="PASS");
 const blocked=data.gates.some(g=>g.state==="BLOCKED");
 const status=failed?"FAIL":complete?"VERIFIED":blocked?"BLOCKED":"UNVERIFIED";
 setPill("evidence-state",status,status==="VERIFIED"?"good":status==="FAIL"?"fail":status==="BLOCKED"?"blocked":"pending");
 const vercelStatus=state.commitStatusSha===sha&&state.commitStatus?(state.commitStatus.statuses||[]).find(x=>String(x.context||"").toLowerCase()==="vercel"):null;
 const deployLink=vercelStatus&&vercelStatus.target_url?link(vercelStatus.target_url,"Deployment details ↗"):"";
 $("verification-list").innerHTML='<div class="item"><b>Target SHA</b><p>'+esc(sha||"No target SHA")+'</p><div class="meta">'+badge(data.same?"EXACT SHA":"NO MATCH")+' '+(run?link(run.html_url,"Workflow ↗"):"<span class=\\\"muted\\\">No workflow</span>")+'</div></div>'+data.gates.map(g=>'<div class="item"><div class="item-head"><h3>'+esc(g.name)+'</h3>'+badge(g.state)+'</div><p>'+esc(g.note)+'</p>'+(g.name==="Vercel preview deployment"?deployLink:"")+'</div>').join("");
 const artifacts=run&&state.artifacts[run.id]||[];
 $("artifact-list").innerHTML=artifacts.length?artifacts.map(a=>item(a.name,a.expired?"Artifact expired":"Created "+date(a.created_at),badge(a.expired?"EXPIRED":"METADATA AVAILABLE"),run.html_url)).join(""):'<div class="empty">No candidate workflow artifacts are visible for this exact-SHA run. coverage.xml and coverage.json remain NOT RUN.</div>';
 $("edge-list").innerHTML=edgeCases.map(e=>'<div class="edge"><b>'+esc(e[0])+'</b><small>'+esc(e[1])+'</small>'+badge("NOT RUN")+'</div>').join("");
}
function renderRelease() {
 const p=currentPR(), sha=p&&p.head&&p.head.sha, run=sha&&latestForSha(sha), data=evidenceData(p,run);
 const gates=data.gates;
 $("release-list").innerHTML=gates.map((g,i)=>'<div class="gate"><span class="num">'+(i+1)+'</span><div><b>'+esc(g.name)+'</b><p>'+esc(g.note)+'</p></div>'+badge(g.state)+'</div>').join("");
 const next=gates.find(g=>g.state!=="PASS");
 $("release-status").textContent=next?"GATES REMAIN":"ALL LISTED GATES PASS";
 $("release-status").className="pill "+(next?"pending":"good");
}
async function loadRun(id) {
 $("run-detail").innerHTML='<div class="loading">Loading event replay…</div>';
 try {
  const data=await local("/api/runs/"+encodeURIComponent(id));
  const events=Array.isArray(data)?data:(data.events||data.items||[]);
  $("run-detail").innerHTML=events.length?events.slice(-180).map(e=>{
   const d=e.data||{};
   const lane=d.lane||d.focus_lane||"";
   return '<div class="item"><div class="item-head"><h3>'+esc(e.kind||"event")+' · '+esc(e.agent||"system")+'</h3>'+badge(e.kind==="error"?"FAIL":"OBSERVED")+'</div><p>'+esc(d.message||d.tool||d.focus||d.text||JSON.stringify(d).slice(0,400))+'</p><div class="meta"><span class="muted">'+esc(date(e.ts))+'</span>'+(lane?'<span class="muted">lane '+esc(lane)+'</span>':"")+'</div></div>';
  }).join(""):'<div class="empty">No events returned for '+esc(id)+'.</div>';
 } catch(e) { $("run-detail").innerHTML='<div class="empty">Run detail blocked: '+esc(e.message)+'</div>'; }
}
async function loadWorkflowDetails(run) {
 if(!run) return;
 const id=run.id;
 const needsJobs=!Object.prototype.hasOwnProperty.call(state.jobs,id);
 const needsArtifacts=!Object.prototype.hasOwnProperty.call(state.artifacts,id);
 if(!needsJobs&&!needsArtifacts) return;
 const tasks=await Promise.allSettled([
  needsJobs?gh("/actions/runs/"+id+"/jobs?per_page=100"):Promise.resolve({jobs:state.jobs[id]}),
  needsArtifacts?gh("/actions/runs/"+id+"/artifacts?per_page=100"):Promise.resolve({artifacts:state.artifacts[id]})
 ]);
 if(tasks[0].status==="fulfilled") state.jobs[id]=tasks[0].value.jobs||[];
 else state.errors.push({key:"jobs-"+id,message:tasks[0].reason&&tasks[0].reason.message||"Job details unavailable"});
 if(tasks[1].status==="fulfilled") state.artifacts[id]=tasks[1].value.artifacts||[];
 else state.errors.push({key:"artifacts-"+id,message:tasks[1].reason&&tasks[1].reason.message||"Artifact metadata unavailable"});
 renderVerification();renderRelease();
}
async function compareWorkflowRuns() {
 const a=state.workflows.find(w=>String(w.id)===$("compare-run-a").value);
 const b=state.workflows.find(w=>String(w.id)===$("compare-run-b").value);
 if(!a||!b) { $("run-compare").innerHTML='<div class="empty">Select two workflow runs first.</div>'; return; }
 if(a.id===b.id) { $("run-compare").innerHTML='<div class="empty">Choose two different workflow runs.</div>'; return; }
 $("run-compare").innerHTML='<div class="loading">Loading job and artifact evidence for both runs…</div>';
 await Promise.all([loadWorkflowDetails(a),loadWorkflowDetails(b)]);
 const aj=state.jobs[a.id]||[],bj=state.jobs[b.id]||[];
 const aa=state.artifacts[a.id]||[],ba=state.artifacts[b.id]||[];
 const names=[...new Set([...aj.map(x=>x.name),...bj.map(x=>x.name)])].sort();
 const rows=names.map(name=>{
  const x=aj.find(j=>j.name===name),y=bj.find(j=>j.name===name);
  return '<tr><td>'+esc(name)+'</td><td>'+badge(x?(x.conclusion||x.status):"NOT PRESENT")+'</td><td>'+badge(y?(y.conclusion||y.status):"NOT PRESENT")+'</td></tr>';
 }).join("");
 const same=a.head_sha===b.head_sha;
 $("run-compare").innerHTML='<div class="facts">'+fact("Run A","#"+a.run_number+" · "+a.event)+fact("Run B","#"+b.run_number+" · "+b.event)+fact("Run A SHA",formatSha(a.head_sha))+fact("Run B SHA",formatSha(b.head_sha))+fact("Same exact SHA",same?"yes":"no — results are not interchangeable")+fact("Conclusions",(a.conclusion||a.status)+" / "+(b.conclusion||b.status))+fact("Artifact counts",aa.length+" / "+ba.length)+'</div>'+
  '<p class="muted">'+(same?"Both runs target the same exact commit.":"Different commit SHAs: compare diagnostically only; evidence cannot transfer between targets.")+'</p>'+
  (names.length?'<div class="table-wrap"><table class="data-table"><thead><tr><th>Job</th><th>Run A</th><th>Run B</th></tr></thead><tbody>'+rows+'</tbody></table></div>':'<div class="empty">No job detail returned for one or both runs.</div>')+
  '<p class="muted">Artifact names are metadata only; artifact contents and exact-SHA provenance still require inspection.</p>'+
  link(a.html_url,"Open Run A ↗")+' · '+link(b.html_url,"Open Run B ↗");
}
async function refresh() {
 $("connection").textContent="REFRESHING";$("connection").className="pill pending";
 state.errors=[];
 const tasks=[
  ["prs",()=>ghPages("/pulls?state=all&per_page=100&sort=updated&direction=desc",5)],
  ["commits",()=>ghPages("/commits?per_page=100",5)],
  ["workflows",()=>ghPages("/actions/runs?per_page=100",5)],
  ["status",()=>local("/api/status")],
  ["runs",()=>local("/api/runs")],
  ["checkpoints",()=>local("/api/checkpoints")],
  ["telemetry",()=>local("/api/telemetry?hours=24")],
  ["fleet",()=>local("/api/fleet/directory")],
  ["evolve",()=>local("/api/evolve")]
 ];
 state.pagination={};
 await Promise.all(tasks.map(async t=>{try{const val=await t[1]();
  if(["prs","commits","workflows"].includes(t[0])){state[t[0]]=val.items;state.pagination[t[0]]=val;}
  else state[t[0]]=val;
 }catch(e){state[t[0]]=null;state.errors.push({key:t[0],message:e.message});}}));
 if(!Array.isArray(state.prs)) state.prs=[];
 state.prs.sort((a,b)=>new Date(b.updated_at||0)-new Date(a.updated_at||0));
 $("f-pr").innerHTML='<option value="">All PRs</option>'+state.prs.map(p=>'<option value="'+p.number+'">#'+p.number+' · '+esc(p.title).slice(0,65)+'</option>').join("");
 if(state.prs.some(p=>p.number===27)) $("f-pr").value="27";
 else $("f-pr").value="";
 state.selectedPR=Number($("f-pr").value||27);
 await loadSelectedCommitStatus();
 if(!Array.isArray(state.commits)) state.commits=[];
 if(!Array.isArray(state.workflows)) state.workflows=[];
 if(!Array.isArray(state.runs)) state.runs=[];
 if(!Array.isArray(state.checkpoints)) state.checkpoints=null;
 if(!Array.isArray(state.fleet)) state.fleet=null;
 if(state.telemetry===null) state.telemetry=null;
 renderTarget();renderMetrics();renderOverview();renderPRs();renderCommits();renderMissions();renderSwarm();renderAgents();renderVerification();renderRelease();
 const ok=!["prs","commits","workflows","commit-status"].some(hasError);
 $("connection").textContent=ok?"GITHUB LIVE":"GITHUB LIMITED";
 $("connection").className="pill "+(ok?"good":"blocked");
 const trunc=Object.entries(state.pagination||{}).filter(([,v])=>v.truncated).map(([k])=>k);
 $("updated").textContent="Updated "+new Date().toLocaleTimeString()+" · GitHub "+(ok?"connected":"limited")+(trunc.length?" · page cap reached: "+trunc.join(", "):"")+" · AVRONE "+runtimeStatusText();
 const run=latestForSha(currentSha());
 if(run) loadWorkflowDetails(run);
}
document.querySelectorAll(".tab").forEach(b=>b.addEventListener("click",()=>{
 document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
 document.querySelectorAll(".view").forEach(x=>x.classList.remove("active"));
 b.classList.add("active");$("view-"+b.dataset.tab).classList.add("active");
}));
$("refresh").addEventListener("click",refresh);
$("f-pr").addEventListener("change",async()=>{state.selectedPR=Number($("f-pr").value||27);await loadSelectedCommitStatus();renderTarget();renderNextGate();renderSwarm();renderVerification();renderRelease();});
["f-repo","f-commit","f-round","f-lane","f-agent","f-runtime","f-verification"].forEach(id=>$(id).addEventListener("input",()=>{renderPRs();renderCommits();renderSwarm();}));
$("clear-filters").addEventListener("click",()=>{["f-commit","f-round","f-lane","f-agent","f-runtime","f-verification"].forEach(id=>$(id).value="");$("f-pr").value="27";state.selectedPR=27;renderTarget();renderPRs();renderCommits();renderSwarm();renderVerification();renderRelease();});
$("apply-token").addEventListener("click",()=>{state.adminToken=$("admin-token").value.trim();$("admin-token").value="";refresh();});
$("compare-runs").addEventListener("click",compareWorkflowRuns);
$("compare").addEventListener("click",async()=>{
 const base=$("compare-base").value,head=$("compare-head").value;
 if(!base||!head) return $("compare-result").textContent="Select both base and head commits.";
 if(base===head) return $("compare-result").textContent="Choose two different commits.";
 $("compare-result").textContent="Comparing…";
 try{const d=await gh("/compare/"+encodeURIComponent(base)+"..."+encodeURIComponent(head));$("compare-result").innerHTML='<p><b>'+esc(d.status||"comparison")+'</b> · '+(d.ahead_by||0)+' ahead · '+(d.behind_by||0)+' behind · '+(d.total_commits||0)+' commits</p><p>'+esc((d.files||[]).length)+' changed files</p>'+(d.html_url?link(d.html_url,"Open comparison ↗"):"");}catch(e){$("compare-result").textContent="Compare unavailable: "+e.message;}
});
document.addEventListener("click",e=>{
 const a=e.target.closest("[data-run]");
 if(a) loadRun(a.dataset.run);
 const b=e.target.closest("[data-href]");
 if(b&&b.dataset.href) window.open(b.dataset.href,"_blank","noopener");
});
$("close-dialog").addEventListener("click",()=>$("detail-dialog").close());
refresh();
setInterval(refresh,300000);
})();