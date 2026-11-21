const VIEWS = [
  ["chat", "Chat"],
  ["overview", "Overview"],
  ["branches", "Branches"],
  ["pulls", "Pull requests"],
  ["commits", "Commits"],
  ["rounds", "Test runs"],
  ["suites", "Edge suites"],
  ["agents", "Assignments"],
  ["checkpoints", "Checkpoints"],
  ["propagation", "Failures"],
  ["artifacts", "Artifacts"],
  ["changes", "Gates"],
  ["settings", "Settings"],
  ["system", "System"],
];

const state = { view: "chat", overview: null, roundId: null, log: "", threadId: null, agentId: null, chatLog: "" };

const $ = (sel) => document.querySelector(sel);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function chips(list) {
  return (list || []).map((chip) => `<span class="chip chip-${esc(chip)}">${esc(chip)}</span>`).join("");
}

async function api(path, options) {
  const response = await fetch(path, options);
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { error: text }; }
  if (!response.ok) {
    const reason = body && (body.reasons ? body.reasons.join(" ") : body.error);
    throw new Error(reason || response.statusText);
  }
  return body;
}

function nav() {
  $("#nav").innerHTML = VIEWS.map(([id, label]) =>
    `<button type="button" data-view="${id}" class="${state.view === id ? "active" : ""}">${label}</button>`
  ).join("");
  $("#nav").onclick = (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    state.view = button.dataset.view;
    nav();
    render();
  };
}

function renderNext(overview) {
  const gate = overview.next_gate || {};
  const item = gate.gate || {};
  $("#next").innerHTML = `
    <p class="kicker">Next required verification gate</p>
    <h2>${esc(item.name || "No gate")}</h2>
    <div>${chips(gate.chips)}</div>
    <p><strong>${esc(gate.change_title || "No change ingested")}</strong></p>
    <p>${esc(item.detail || "")}</p>
    <p class="muted">${gate.others_waiting || 0} other change(s) still unverified.
    A change stays unverified until the full suite passes and every acceptance gate passes.
    The verified chip is computed; it cannot be switched on by hand.</p>`;
}

function renderPills(overview) {
  const github = overview.github || {};
  const ollama = overview.ollama || {};
  const ghClass = github.state === "ok" ? "chip-merged" : "chip-unverified";
  const olClass = ollama.state === "reachable" ? "chip-verified" : "chip-failed";
  $("#pills").innerHTML = `
    <span class="chip ${ghClass}">GitHub ${esc(github.state || "unknown")}</span>
    <span class="chip ${olClass}">${esc(ollama.detail || "Ollama")}</span>
    <span class="chip chip-open">${esc(overview.branch || "")} ${esc((overview.head || "").slice(0, 10))}</span>`;
  const dirty = overview.worktree_dirty ? " Working tree has uncommitted files; ingest reads committed objects only." : "";
  $("#foot").textContent = "No example records are seeded. " +
    Object.entries(overview.honesty || {}).filter(([key]) => key !== "example_data")
      .map(([key, value]) => `${key}: ${value}`).join(" · ") + dirty;
}

async function showOverview() {
  const overview = state.overview;
  const counts = overview.counts || {};
  const cards = Object.entries(counts).map(([key, value]) =>
    `<div class="stat"><b>${esc(value)}</b><span>${esc(key)}</span></div>`).join("");
  const changes = (overview.changes || []).slice(0, 12).map((change) =>
    `<tr><td>${esc(change.title)}</td><td>${chips(change.chips)}</td><td>${esc(change.next_gate && change.next_gate.name)}</td></tr>`
  ).join("");
  const github = overview.github || {};
  const banner = github.state === "ok" ? "" : `<div class="banner">${esc(github.detail || "")}</div>`;
  const ollama = overview.ollama || {};
  const ollamaBanner = ollama.state === "reachable" ? "" : `<div class="banner bad">${esc(ollama.detail || "Ollama unreachable")} at ${esc(ollama.host || "")}</div>`;
  $("#main").innerHTML = `
    ${banner}${ollamaBanner}
    <div class="grid">${cards}</div>
    <div class="card"><h2>Changes</h2>
      <table><thead><tr><th>Change</th><th>Status</th><th>Next gate</th></tr></thead><tbody>${changes || `<tr><td colspan="3">No changes yet.</td></tr>`}</tbody></table>
    </div>`;
}

async function showBranches() {
  const data = await api("/api/branches");
  const rows = data.branches.map((branch) => `<tr>
    <td>${esc(branch.name)}</td>
    <td class="hash">${esc(branch.sha)}</td>
    <td>${esc(branch.subject)}</td>
    <td>${esc(branch.committer_date || "")}</td>
    <td>${branch.ahead ?? "—"} / ${branch.behind ?? "—"}</td>
    <td>${branch.is_remote ? "remote" : "local"}</td>
  </tr>`).join("");
  $("#main").innerHTML = `<div class="card"><h2>Repository branches</h2>
    <table><thead><tr><th>Branch</th><th>SHA</th><th>Subject</th><th>Committer date</th><th>Ahead / behind main</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

async function showPulls() {
  const data = await api("/api/pulls");
  const github = data.github || {};
  const banner = github.state === "ok" ? "" : `<div class="banner">${esc(github.detail || "GitHub data is not loaded.")}</div>`;
  const rows = (data.pulls || []).map((pull) => `<tr>
    <td><a href="${esc(pull.html_url)}">#${esc(pull.number)}</a></td>
    <td>${esc(pull.title)}</td>
    <td>${chips(pull.chips)} <span class="muted">${esc(pull.state)}${pull.draft ? " draft" : ""}${pull.merged ? " merged" : ""}</span></td>
    <td>${esc(pull.head_ref)}</td>
    <td class="hash">${esc(pull.head_sha)}</td>
  </tr>`).join("");
  $("#main").innerHTML = `${banner}<div class="card"><h2>Pull requests</h2>
    <table><thead><tr><th></th><th>Title</th><th>Status</th><th>Head</th><th>SHA</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="5">No pull requests loaded.</td></tr>`}</tbody></table></div>`;
}

async function showCommits() {
  const branches = await api("/api/branches");
  const names = branches.branches.map((branch) => branch.name);
  const selected = state.commitBranch || names.find((name) => name === "main" || name === "origin/main") || names[0] || "";
  state.commitBranch = selected;
  const data = await api("/api/commits?branch=" + encodeURIComponent(selected));
  const options = names.map((name) => `<option ${name === selected ? "selected" : ""}>${esc(name)}</option>`).join("");
  const rows = data.commits.map((commit) => `<tr>
    <td class="hash">${esc(commit.sha)}</td><td>${esc(commit.subject)}</td><td>${esc(commit.author)}</td><td>${esc(commit.committed_at)}</td>
  </tr>`).join("");
  $("#main").innerHTML = `<div class="card"><h2>Commits</h2>
    <div class="row"><label>Branch <select id="commit-branch">${options}</select></label></div>
    <table><thead><tr><th>SHA</th><th>Subject</th><th>Author</th><th>Date</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  $("#commit-branch").onchange = (event) => { state.commitBranch = event.target.value; showCommits(); };
}

async function showSuites() {
  const data = await api("/api/suites");
  const refs = [...new Set(data.files.map((file) => file.ref))];
  const selected = state.suiteRef || refs.find((ref) => ref.includes("avrone-a12lr-daemon")) || refs[0] || "";
  state.suiteRef = selected;
  const query = (state.suiteQuery || "").toLowerCase();
  const rows = data.files.filter((file) => file.ref === selected && (!query || file.path.toLowerCase().includes(query) || (file.syntax_error || "").toLowerCase().includes(query)))
    .map((file) => `<tr>
      <td>${esc(file.suite)}</td>
      <td>${esc(file.path)}</td>
      <td>${file.syntax_ok ? `<span class="chip chip-open">compiles</span>` : `<span class="chip chip-failed">failed</span> ${esc(file.syntax_error)}`}</td>
      <td>${esc(file.case_count ?? "—")}</td>
      <td class="hash">${esc(file.sha256)}</td>
    </tr>`).join("");
  $("#main").innerHTML = `<div class="card"><h2>Edge-case suites</h2>
    <div class="row">
      <label>Ref <select id="suite-ref">${refs.map((ref) => `<option ${ref === selected ? "selected" : ""}>${esc(ref)}</option>`).join("")}</select></label>
      <input id="suite-q" placeholder="Filter path or error" value="${esc(state.suiteQuery || "")}">
    </div>
    <table id="suite-table"><thead><tr><th>Suite</th><th>File</th><th>Compile</th><th>Cases</th><th>SHA-256</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  $("#suite-ref").onchange = (event) => { state.suiteRef = event.target.value; showSuites(); };
  $("#suite-q").oninput = (event) => {
    const q = event.target.value.toLowerCase();
    document.querySelectorAll("#suite-table tbody tr").forEach((row) => {
      row.style.display = row.textContent.toLowerCase().includes(q) ? "" : "none";
    });
  };
}

async function showArtifacts() {
  const data = await api("/api/artifacts");
  const refs = [...new Set(data.artifacts.map((item) => item.ref))];
  const selected = state.artifactRef || refs.find((ref) => ref === "origin/avrone-a12lr-daemon") || refs[0] || "";
  state.artifactRef = selected;
  const rows = data.artifacts.filter((item) => item.ref === selected).map((item) => `<tr class="clickable" data-path="${esc(item.path)}">
    <td>${esc(item.kind)}</td><td>${esc(item.path)}</td><td>${esc(item.size)}</td><td class="hash">${esc(item.sha256)}</td>
  </tr>`).join("");
  $("#main").innerHTML = `<div class="card"><h2>Artifacts and evidence</h2>
    <div class="row"><label>Ref <select id="art-ref">${refs.map((ref) => `<option ${ref === selected ? "selected" : ""}>${esc(ref)}</option>`).join("")}</select></label></div>
    <p class="muted">Select a row to read a preview. The SHA-256 is of the git blob.</p>
    <table><thead><tr><th>Kind</th><th>Path</th><th>Bytes</th><th>SHA-256</th></tr></thead><tbody>${rows}</tbody></table>
    <div id="art-body"></div></div>`;
  $("#art-ref").onchange = (event) => { state.artifactRef = event.target.value; showArtifacts(); };
  $("#main").querySelector("tbody").onclick = async (event) => {
    const row = event.target.closest("tr");
    if (!row) return;
    const body = await api(`/api/artifacts/text?ref=${encodeURIComponent(selected)}&path=${encodeURIComponent(row.dataset.path)}`);
    $("#art-body").innerHTML = `<h3>${esc(body.path)}</h3><p class="hash">${esc(body.sha256)}</p><pre>${esc(body.binary ? "(binary)" : body.text)}</pre>`;
  };
}

function roundOptions(rounds, selected) {
  return rounds.map((round) => `<option value="${esc(round.id)}" ${round.id === selected ? "selected" : ""}>${esc(round.id)} · ${esc(round.status)} · ${esc(round.title)}</option>`).join("");
}

async function showRounds() {
  const [rounds, branches, changes, suites] = await Promise.all([
    api("/api/rounds"), api("/api/branches"), api("/api/changes"), api("/api/suites"),
  ]);
  const ref = state.launchRef || (branches.branches.find((item) => item.name === "main") || branches.branches[0] || {}).name || "HEAD";
  state.launchRef = ref;
  const files = suites.files.filter((file) => file.ref === ref);
  const fileBoxes = files.slice(0, 400).map((file) =>
    `<label><input type="checkbox" value="${esc(file.path)}" ${state.picked && state.picked.has(file.path) ? "checked" : ""}> ${esc(file.path)}${file.syntax_ok ? "" : " (syntax error)"}</label>`
  ).join("") || `<p class="muted">No test files indexed for this ref. Refresh ingest.</p>`;
  const changeOptions = `<option value="">(none)</option>` + changes.changes.map((change) =>
    `<option value="${esc(change.change_id)}">${esc(change.title)}</option>`).join("");
  const rows = rounds.rounds.map((round) => `<tr class="clickable" data-id="${esc(round.id)}">
    <td>${esc(round.title)}</td><td>${esc(round.runner)}</td><td>${esc(round.status)}</td><td>${chips(round.chips)}</td>
    <td class="hash">${esc(round.commit_sha)}</td><td class="hash">${esc(round.log_sha256 || "")}</td>
  </tr>`).join("");
  $("#main").innerHTML = `<div class="split">
    <form id="launch" class="card">
      <h2>Launch a round</h2>
      <label>Title <input name="title" value="Robust round"></label>
      <label>Ref <select name="ref">${branches.branches.map((item) => `<option ${item.name === ref ? "selected" : ""}>${esc(item.name)}</option>`).join("")}</select></label>
      <label>Runner
        <select name="runner"><option value="python">local python</option><option value="ollama">local Ollama</option></select>
      </label>
      <label>Change <select name="change_id">${changeOptions}</select></label>
      <label><input type="checkbox" name="acceptance" checked> Acceptance run for that change</label>
      <input id="file-filter" placeholder="Filter files">
      <div class="files" id="file-list">${fileBoxes}</div>
      <div class="row"><button class="primary" type="submit">Launch round</button></div>
      <p class="muted">Python runs the file in a detached worktree. There is no harness timeout. Ollama is advisory and cannot mark a suite passed.</p>
    </form>
    <div class="card"><h2>Rounds</h2>
      <table><thead><tr><th>Title</th><th>Runner</th><th>State</th><th>Status</th><th>Commit</th><th>Log SHA-256</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6">No rounds yet.</td></tr>`}</tbody></table>
    </div>
  </div>
  <div class="card" id="round-detail">${state.roundId ? "" : `<p class="muted">Select a round to inspect evidence, cancel, or resume.</p>`}</div>
  <div class="card"><h2>Compare two rounds</h2>
    <div class="row">
      <select id="cmp-a">${roundOptions(rounds.rounds, "")}</select>
      <select id="cmp-b">${roundOptions(rounds.rounds, "")}</select>
      <button type="button" id="cmp-go">Compare</button>
    </div>
    <div id="cmp-out"></div>
  </div>`;
  $("#launch").querySelector("[name=ref]").onchange = (event) => {
    state.launchRef = event.target.value;
    state.picked = selectedFiles();
    showRounds();
  };
  $("#file-filter").oninput = (event) => {
    const q = event.target.value.toLowerCase();
    document.querySelectorAll("#file-list label").forEach((label) => {
      label.style.display = label.textContent.toLowerCase().includes(q) ? "" : "none";
    });
  };
  $("#launch").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    const picked = [...form.querySelectorAll("#file-list input:checked")].map((node) => node.value);
    try {
      const round = await api("/api/rounds", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          title: form.title.value,
          ref: form.ref.value,
          runner: form.runner.value,
          change_id: form.change_id.value || null,
          acceptance: form.acceptance.checked,
          files: picked,
        }),
      });
      state.roundId = round.id;
      state.log = "";
      await showRounds();
      watchRound(round.id);
    } catch (error) {
      alert(error.message);
    }
  };
  $("#main").querySelector("tbody").onclick = (event) => {
    const row = event.target.closest("tr");
    if (!row || !row.dataset.id) return;
    state.roundId = row.dataset.id;
    state.log = "";
    loadRound(row.dataset.id);
  };
  $("#cmp-go").onclick = compareRounds;
  if (state.roundId) loadRound(state.roundId);
}

function selectedFiles() {
  return new Set([...document.querySelectorAll("#file-list input:checked")].map((node) => node.value));
}

async function loadRound(id) {
  const round = await api("/api/rounds/" + encodeURIComponent(id));
  const running = round.status === "running" || round.status === "open";
  const resumable = round.status === "interrupted" || round.status === "cancelled";
  const files = (round.files || []).map((file) => `<tr>
    <td>${esc(file.path)}</td><td>${esc(file.status)}</td><td>${esc(file.exit_code ?? "")}</td>
    <td>${esc(file.started_at || "")}</td><td>${esc(file.finished_at || "")}</td><td class="hash">${esc(file.log_sha256 || "")}</td>
  </tr>`).join("");
  const cases = (round.cases || []).map((item) => `<tr class="clickable" data-case="${esc(item.id)}">
    <td>${esc(item.file_path)}</td><td>${esc(item.name)}</td><td>${esc(item.outcome)}</td>
  </tr>`).join("");
  const anomalies = (round.anomalies || []).map((item) =>
    `<li><span class="muted">${esc(item.ts)}</span> ${esc(item.message)}</li>`).join("");
  const fixes = (round.fixes || []).map((item) =>
    `<li>${chips(item.chips)} ${esc(item.case_name)} · ${esc(item.status)} · ${esc(item.file_path)}</li>`).join("");
  $("#round-detail").innerHTML = `
    <h2>${esc(round.title)} ${chips(round.chips)}</h2>
    <p class="muted">Immutable inputs. Ref ${esc(round.ref)} commit <span class="hash">${esc(round.commit_sha)}</span>
    runner ${esc(round.runner)} log <span class="hash">${esc(round.log_sha256 || "(not sealed)")}</span></p>
    <div class="row">
      ${running ? `<button type="button" class="danger" id="cancel-round">Cancel round</button>` : ""}
      ${resumable ? `<button type="button" id="resume-round">Resume from checkpoint</button>` : ""}
    </div>
    <h3>Environment</h3><pre>${esc(JSON.stringify(round.env, null, 2))}</pre>
    <h3>Inputs</h3><pre>${esc(JSON.stringify(round.inputs, null, 2))}</pre>
    <h3>Files</h3><table><thead><tr><th>Path</th><th>Status</th><th>Exit</th><th>Started</th><th>Finished</th><th>SHA-256</th></tr></thead><tbody>${files}</tbody></table>
    <h3>Cases</h3><table><thead><tr><th>File</th><th>Case</th><th>Outcome</th></tr></thead><tbody>${cases}</tbody></table>
    <div id="evidence"></div>
    <h3>Anomalies</h3><ul>${anomalies || "<li class='muted'>None.</li>"}</ul>
    <h3>Fix tracking</h3><ul>${fixes || "<li class='muted'>None.</li>"}</ul>
    <h3>Live log</h3><pre class="log" id="live-log">${esc(state.log)}</pre>`;
  const cancel = $("#cancel-round");
  if (cancel) cancel.onclick = async () => { await api(`/api/rounds/${id}/cancel`, { method: "POST" }); };
  const resume = $("#resume-round");
  if (resume) resume.onclick = async () => {
    await api(`/api/rounds/${id}/resume`, { method: "POST" });
    watchRound(id);
  };
  $("#round-detail").querySelector("tbody") && ($("#round-detail").querySelectorAll("tbody")[1].onclick = (event) => {
    const row = event.target.closest("tr");
    if (!row) return;
    const found = (round.cases || []).find((item) => String(item.id) === row.dataset.case);
    if (!found) return;
    $("#evidence").innerHTML = `<h3>Evidence · ${esc(found.name)}</h3>
      <p class="muted">Outcome ${esc(found.outcome)}. Trace, stdout, and stderr are the captured process output.</p>
      <h3>Trace</h3><pre>${esc(found.trace || "(empty)")}</pre>
      <h3>stdout</h3><pre>${esc(found.stdout || "(empty)")}</pre>
      <h3>stderr</h3><pre>${esc(found.stderr || "(empty)")}</pre>`;
  });
  if (running) watchRound(id);
}

function watchRound(id) {
  if (state.source) state.source.close();
  const source = new EventSource(`/api/rounds/${encodeURIComponent(id)}/stream`);
  state.source = source;
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.type === "line" && event.line) {
      state.log += event.line;
      const node = $("#live-log");
      if (node) {
        node.textContent = state.log.slice(-20000);
        node.scrollTop = node.scrollHeight;
      }
    }
    if (event.type === "status" && ["passed", "failed", "advisory", "cancelled", "interrupted"].includes(event.status)) {
      source.close();
      loadRound(id);
      refreshOverview();
    }
  };
}

async function compareRounds() {
  try {
    const result = await api("/api/compare", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ round_a: $("#cmp-a").value, round_b: $("#cmp-b").value }),
    });
    const rows = result.rows.map((row) => `<tr><td>${esc(row.key)}</td><td>${esc(row.a)}</td><td>${esc(row.b)}</td><td>${esc(row.delta)}</td></tr>`).join("");
    $("#cmp-out").innerHTML = `<p>Pass delta ${esc(result.pass_delta)}, fail delta ${esc(result.fail_delta)}.
      Fixed ${result.fixed.length}, regressed ${result.regressed.length}, new failures ${result.new_failures.length}.</p>
      <table><thead><tr><th>Case</th><th>A</th><th>B</th><th>Delta</th></tr></thead><tbody>${rows}</tbody></table>`;
  } catch (error) {
    $("#cmp-out").innerHTML = `<div class="banner bad">${esc(error.message)}</div>`;
  }
}

async function showAgents() {
  const [data, changes] = await Promise.all([api("/api/assignments"), api("/api/changes")]);
  const rows = data.assignments.map((item) => `<tr>
    <td>${esc(item.lane)}</td><td>${esc(item.agent_kind)}</td><td>${esc(item.agent_ref)}</td><td>${esc(item.note)}</td><td>${esc(item.change_id || "")}</td><td>${esc(item.created_at)}</td>
  </tr>`).join("");
  $("#main").innerHTML = `<form id="assign" class="card"><h2>Assign an agent</h2>
    <div class="row">
      <select name="lane">
        <option value="coverage">coverage/tests</option>
        <option value="tools">new tools</option>
        <option value="skills">agent skills</option>
        <option value="robustness">robustness</option>
      </select>
      <select name="agent_kind">
        <option value="human">human</option>
        <option value="cursor_cloud">Cursor cloud agent id</option>
        <option value="ollama">local Ollama agent</option>
      </select>
      <input name="agent_ref" placeholder="name or cloud agent id" required>
      <input name="note" placeholder="note">
      <select name="change_id"><option value="">(no change)</option>${changes.changes.map((change) => `<option value="${esc(change.change_id)}">${esc(change.title)}</option>`).join("")}</select>
      <button class="primary" type="submit">Assign</button>
    </div></form>
    <div class="card"><h2>Lanes</h2>
      <table><thead><tr><th>Lane</th><th>Kind</th><th>Agent</th><th>Note</th><th>Change</th><th>When</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6">No assignments yet. Nothing is invented here.</td></tr>`}</tbody></table></div>`;
  $("#assign").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    await api("/api/assignments", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        lane: form.lane.value,
        agent_kind: form.agent_kind.value,
        agent_ref: form.agent_ref.value,
        note: form.note.value,
        change_id: form.change_id.value || null,
      }),
    });
    showAgents();
  };
}

async function showCheckpoints() {
  const data = await api("/api/checkpoints");
  const rows = data.checkpoints.map((item) => `<tr>
    <td>${esc(item.round_id)}</td><td>${esc(item.ts)}</td><td>${esc(item.cursor_index)}</td><td>${esc(item.note)}</td><td>${esc(item.round_status)}</td>
  </tr>`).join("");
  $("#main").innerHTML = `<div class="card"><h2>Checkpoints</h2>
    <table><thead><tr><th>Round</th><th>Time</th><th>Cursor</th><th>Note</th><th>Round status</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="5">No checkpoints yet.</td></tr>`}</tbody></table></div>`;
}

async function showPropagation() {
  const graph = await api("/api/propagation");
  const nodes = Object.fromEntries(graph.nodes.map((node) => [node.id, node]));
  const incoming = {};
  graph.edges.forEach((edge) => { (incoming[edge.to] ||= []).push(edge); });
  const changes = graph.nodes.filter((node) => node.type === "change");
  const blocks = changes.map((change) => {
    const gates = (incoming[change.id] || []).filter((edge) => edge.reason.startsWith("blocks"));
    if (!gates.length) return "";
    const inner = gates.map((edge) => {
      const gate = nodes[edge.from];
      const failures = (incoming[edge.from] || []).map((failEdge) => {
        const failure = nodes[failEdge.from];
        return `<li><span class="chip chip-failed">failure</span> ${esc(failure ? failure.label : failEdge.from)}
          <div class="muted">${esc(failure ? failure.detail : "")}</div></li>`;
      }).join("");
      return `<li><span class="chip chip-blocked">gate</span> ${esc(gate ? gate.label : edge.from)}<ul>${failures}</ul></li>`;
    }).join("");
    return `<li><span class="chip chip-unmerged">change</span> ${esc(change.label)}<ul>${inner}</ul></li>`;
  }).join("");
  $("#main").innerHTML = `<div class="card"><h2>Failure propagation</h2>
    <p class="muted">${graph.nodes.length} nodes, ${graph.edges.length} edges. A failure blocks its gate, and that gate blocks the change.</p>
    <ul class="tree">${blocks || "<li>No blocking failures in the ingested trees.</li>"}</ul></div>`;
}

async function showChanges() {
  const data = await api("/api/changes");
  const fixes = await api("/api/fixes");
  const cards = data.changes.map((change) => {
    const gates = (change.gates || []).map((gate) => `<li>
      ${gate.passed ? `<span class="chip chip-verified">pass</span>` : `<span class="chip chip-unverified">open</span>`}
      <strong>${esc(gate.name)}</strong> <span class="muted">${esc(gate.kind)}</span>
      <div>${esc(gate.detail)}</div>
      ${gate.kind === "human" ? `<button type="button" data-attest="${esc(change.change_id)}" data-gate="${esc(gate.gate_id)}" data-passed="${gate.passed ? "0" : "1"}">${gate.passed ? "Withdraw attestation" : "Attest evidence"}</button>` : `<span class="muted">Computed from runs. Not toggleable.</span>`}
    </li>`).join("");
    return `<article class="card" id="change-${esc(change.change_id)}">
      <h2>${esc(change.title)}</h2>
      <div>${chips(change.chips)}</div>
      <p class="hash">${esc(change.sha || "")}</p>
      <p class="muted">${(change.required_files || []).length} acceptance file(s). Ref ${esc(change.ref || "")}.</p>
      <ul>${gates}</ul>
      <button type="button" class="primary" data-verify="${esc(change.change_id)}">Record verification</button>
      <p class="muted" data-msg="${esc(change.change_id)}"></p>
    </article>`;
  }).join("");
  const fixRows = fixes.fixes.map((fix) => `<tr>
    <td>${esc(fix.id)}</td><td>${chips(fix.chips)}</td><td>${esc(fix.file_path)}</td><td>${esc(fix.case_name)}</td>
    <td>${esc(fix.status)}</td><td class="hash">${esc(fix.fix_commit || "")}</td><td>${esc(fix.fix_pr || "")}</td>
    <td>${esc(fix.reverify_round_id || "")}</td>
    <td><button type="button" data-link="${esc(fix.id)}">Link commit</button></td>
  </tr>`).join("");
  $("#main").innerHTML = `${cards || `<div class="card">No changes.</div>`}
    <div class="card"><h2>Fix tracking</h2>
      <p class="muted">Linking a commit or PR does not clear the failure. A later passing run has to.</p>
      <table><thead><tr><th>Id</th><th>Status</th><th>File</th><th>Case</th><th>State</th><th>Fix commit</th><th>PR</th><th>Re-verified by</th><th></th></tr></thead>
      <tbody>${fixRows || `<tr><td colspan="9">No fix items.</td></tr>`}</tbody></table></div>`;
  $("#main").onclick = async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.verify) {
      const slot = document.querySelector(`[data-msg="${button.dataset.verify}"]`);
      try {
        await api(`/api/changes/${button.dataset.verify}/verify`, { method: "POST" });
        slot.textContent = "Verification recorded. The chip follows the gates, not this click.";
        await refreshOverview();
        showChanges();
      } catch (error) {
        slot.textContent = error.message;
      }
    }
    if (button.dataset.attest) {
      await api(`/api/changes/${button.dataset.attest}/gates/${button.dataset.gate}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ passed: button.dataset.passed === "1" }),
      });
      await refreshOverview();
      showChanges();
    }
    if (button.dataset.link) {
      const commit = prompt("Fix commit SHA or ref (empty to skip)");
      if (commit === null) return;
      const pr = prompt("Fix pull request number (empty to skip)");
      if (pr === null) return;
      try {
        await api(`/api/fixes/${button.dataset.link}/link`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ commit: commit || null, pr: pr || null }),
        });
        showChanges();
      } catch (error) {
        alert(error.message);
      }
    }
  };
}

const RENDER = {
  chat: showChat,
  settings: showSettings,
  system: showSystem,
  overview: showOverview,
  branches: showBranches,
  pulls: showPulls,
  commits: showCommits,
  rounds: showRounds,
  suites: showSuites,
  agents: showAgents,
  checkpoints: showCheckpoints,
  propagation: showPropagation,
  artifacts: showArtifacts,
  changes: showChanges,
};

async function render() {
  $("#main").innerHTML = `<p class="muted">Loading…</p>`;
  try {
    await RENDER[state.view]();
  } catch (error) {
    $("#main").innerHTML = `<div class="banner bad">${esc(error.message)}</div>`;
  }
}

async function refreshOverview() {
  state.overview = await api("/api/overview");
  renderNext(state.overview);
  renderPills(state.overview);
}

async function boot() {
  nav();
  $("#refresh").onclick = async () => {
    $("#refresh").textContent = "Refreshing…";
    try {
      await api("/api/ingest", { method: "POST" });
      await refreshOverview();
      await render();
    } catch (error) {
      alert(error.message);
    } finally {
      $("#refresh").textContent = "Refresh ingest";
    }
  };
  await refreshOverview();
  await render();
}

boot();
