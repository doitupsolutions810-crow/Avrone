(() => {
  "use strict";
  const paths = {
    grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    branch:
      '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M6 7v10m12-10v3a4 4 0 0 1-4 4H6"/>',
    commit: '<circle cx="12" cy="12" r="4"/><path d="M3 12h5m8 0h5"/>',
    target:
      '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
    shield:
      '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    box: '<path d="m12 3 9 5v9l-9 5-9-5V8l9-5ZM3 8l9 5 9-5m-9 5v9M7 5l10 6"/>',
    flag: '<path d="M5 21V3m0 1h13l-3 4 3 4H5"/>',
    code: '<path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 20"/>',
    terminal:
      '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 8 4 4-4 4m7 0h3"/>',
    settings:
      '<circle cx="12" cy="12" r="3"/><path d="m9 3-1 3-3 1-2 4 2 2v4l4 3 3-1 3 1 4-3v-4l2-2-2-4-3-1-1-3Z"/>',
    refresh:
      '<path d="M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 14 6M4 12a8 8 0 0 0 14 6"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    repository:
      '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M5 17h14M9 7h6M9 10h4"/>',
    search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
    sliders:
      '<path d="M4 6h4m4 0h8M4 12h9m4 0h3M4 18h2m4 0h10M8 3v6m5 0v6m-7 0v6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    alert: '<path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5m0 3v1"/>',
  };
  const icon = (name) =>
    `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.grid}</svg>`;
  document.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon);
  });
  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const link = (url, label, cls = "") => {
    try {
      const u = new URL(url);
      if (u.protocol !== "https:" || u.hostname !== "github.com")
        return esc(label);
    } catch {
      return esc(label);
    }
    return `<a class="${esc(cls)}" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>`;
  };
  const badge = (state) =>
    `<span class="badge ${["verified", "blocked", "unverified", "merged", "unmerged", "draft", "open"].includes(state) ? state : ""}">${esc(state)}</span>`;
  const short = (sha) => String(sha || "").slice(0, 7);
  const date = (value) =>
    value && !Number.isNaN(Date.parse(value))
      ? new Date(value).toLocaleDateString("en-US", {
          month: "short",
          day: "numeric",
          year: "numeric",
          timeZone: "UTC",
        })
      : "Unknown date";
  const titles = {
    overview: [
      "Every change. Every gate.",
      "A clear view of what shipped, what passed, and what needs proof.",
    ],
    pulls: [
      "Pull requests",
      "Repository state and verification state. Never confused.",
    ],
    commits: [
      "Commit history",
      "Immutable revisions from the default branch. Verification stays bound to the full SHA.",
    ],
    missions: [
      "Durable missions",
      "Inspect persisted checkpoints and resume work without repeating completed steps.",
    ],
    agents: [
      "Agent swarm",
      "Execution environments, resource limits, and the evidence behind each worker.",
    ],
    verification: [
      "Test runs",
      "Exact-commit evidence from GitHub Actions. Skipped is not successful.",
    ],
    artifacts: [
      "Evidence & artifacts",
      "Trace every claim back to an inspectable source.",
    ],
    gates: [
      "Release gates",
      "A release is ready only when every required acceptance gate has evidence.",
    ],
  };
  let data = null,
    view = "overview",
    selected = new Set(),
    runtime = null,
    token = "",
    activePr = null;
  let loading = false;
  function toast(text) {
    $("toast").textContent = text;
    $("toast").hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => ($("toast").hidden = true), 4500);
  }
  async function request(url, options = {}) {
    const response = await fetch(url, {
      cache: "no-store",
      ...options,
      headers: {
        ...(token ? { "X-Admin-Token": token } : {}),
        ...options.headers,
      },
    });
    let body;
    try {
      body = await response.json();
    } catch {
      throw new Error(`Server returned ${response.status} without valid JSON.`);
    }
    if (!response.ok)
      throw new Error(
        typeof body.detail === "string"
          ? body.detail
          : `Request rejected (${response.status}). Check the entered fields.`,
      );
    return body;
  }
  function advancedMissing() {
    return ["round-filter", "lane-filter", "agent-filter"].some((id) =>
      $(id).value.trim(),
    );
  }
  function prs() {
    if (advancedMissing()) return [];
    const query = $("search").value.trim().toLowerCase(),
      state = $("state-filter").value,
      rt = $("runtime-filter").value;
    return data.pullRequests.filter((pr) => {
      const text =
        `${pr.number} ${pr.title} ${pr.headRefOid} ${pr.headRefName}`.toLowerCase();
      const matchesState =
        state === "all" ||
        pr.verification === state ||
        pr.mergeState === state ||
        (state === "draft" && pr.isDraft) ||
        (state === "open" && pr.state === "OPEN");
      const hasRuntime =
        rt === "all" ||
        (["3.11", "3.12"].includes(rt) &&
          data.runs.some(
            (r) =>
              r.headSha === pr.headRefOid &&
              (r.jobs || []).some((j) => j.name === `test (${rt})`),
          ));
      return (
        text.includes(query.replace(/^#/, "")) && matchesState && hasRuntime
      );
    });
  }
  function gateSummary(pr) {
    return pr.gates
      .map(
        (g, i) =>
          `<div class="gate"><span class="gate-icon ${esc(g.state)}">${g.state === "verified" ? "✓" : g.state === "blocked" ? "!" : String(i + 1).padStart(2, "0")}</span><div><div class="gate-name">${esc(g.title)}</div><div class="gate-description">${g.state === "blocked" ? "Blocked · CI evidence failed" : g.state === "verified" ? "Verified · exact SHA" : "Awaiting accepted evidence"}</div></div></div>`,
      )
      .join("");
  }
  function gatesPanel(pr) {
    if (!pr) return "";
    return `<section class="panel gate-panel"><div class="panel-head"><h2>${icon("shield")} Acceptance gates</h2>${badge(pr.verification)}</div><div class="gate-context"><strong>PR #${pr.number} · ${esc(pr.title)}</strong><span class="sha">${esc(short(pr.headRefOid))}</span> &nbsp; Exact-commit verification</div><div class="gate-list">${gateSummary(pr)}</div><div class="gate-bottom">${pr.gates.filter((g) => g.state === "verified").length} of ${pr.gates.length} gates verified. No merge action is enabled.<br><button class="text-button" data-detail="${pr.number}">Inspect full evidence →</button></div></section>`;
  }
  function prTable(rows, overview = false) {
    const display = overview ? rows.slice(0, 6) : rows;
    return `<section class="panel"><div class="panel-head"><div><h2>${overview ? "Work awaiting acceptance" : "Repository pull requests"} <span class="count-chip">${rows.length}</span></h2>${overview ? "" : "<p>Merge history is reported independently of release readiness.</p>"}</div><button class="text-button" id="compare" ${selected.size !== 2 ? "disabled" : ""}>Compare ${selected.size ? `(${selected.size}/2)` : ""} ${icon("layers")}</button></div><div class="table-wrap"><table><thead><tr><th class="check-col"><span class="sr-only"></span></th><th>PULL REQUEST</th><th>VERIFICATION</th><th class="merge-cell">MERGE STATE</th><th></th></tr></thead><tbody>${display.map((pr) => `<tr><td class="check-col"><input type="checkbox" data-select="${pr.number}" aria-label="Compare PR ${pr.number}" ${selected.has(pr.number) ? "checked" : ""}></td><td><button class="pr-title" data-detail="${pr.number}">${esc(pr.title)}</button><div class="pr-meta"><span>#${pr.number}</span><span class="sha">${esc(short(pr.headRefOid))}</span><span>${pr.isDraft ? "Draft" : pr.state === "OPEN" ? "Open" : pr.state === "MERGED" ? "Merged" : "Closed"}</span></div></td><td>${badge(pr.verification)}</td><td class="merge-cell">${badge(pr.mergeState)}</td><td><button class="row-open" data-detail="${pr.number}" aria-label="Inspect PR ${pr.number}">↗</button></td></tr>`).join("") || '<tr><td colspan="5"><div class="empty">No pull requests match these filters.<br>Unavailable metadata is not inferred.</div></td></tr>'}</tbody></table></div><div class="panel-foot"><span>${display.length} of ${rows.length} matching pull requests</span>${overview ? '<button class="text-button" data-view="pulls">View all pull requests →</button>' : "<span>Select two revisions to compare</span>"}</div></section>`;
  }
  function pipeline() {
    return `<section class="panel pipeline-panel"><div class="panel-head"><h2>${icon("layers")} Verification workflow</h2><span class="mono" style="font-size:9px;color:var(--muted)">POLICY · NOT LIVE EXECUTION</span></div><div class="pipeline">${[
      ["01", "Checkpoint", "Persist task and progress"],
      ["02", "Assign lanes", "Reject duplicate ownership"],
      ["03", "Execute", "Capture worker outcomes"],
      ["04", "Verify", "Bind evidence to exact SHA"],
      ["05", "Accept", "All gates must pass"],
    ]
      .map(
        (s, i) =>
          `<div class="pipeline-step ${i === 3 ? "current" : ""}"><span>${s[0]}</span><strong>${s[1]}</strong><p>${s[2]}</p></div>`,
      )
      .join(
        "",
      )}</div><div class="pipeline-foot">${icon("shield")} No execution is implied by this workflow. Worker failures must propagate to acceptance.</div></section>`;
  }
  function overview() {
    const open = data.pullRequests.filter((p) => p.state === "OPEN"),
      blocked = open.filter((p) => p.verification === "blocked"),
      ready = open.filter((p) => p.releaseReady),
      drafts = open.filter((p) => p.isDraft);
    const rows = prs().filter((p) => p.state === "OPEN");
    const pr = rows.find((p) => p.number === activePr) || rows[0];
    const stat = (label, value, note, ic, cls = "") =>
      `<div class="stat"><div class="stat-label">${label}${icon(ic)}</div><div class="stat-value ${cls}">${value}</div><div class="stat-note">${note}</div></div>`;
    return `<div class="stats">${stat("Open pull requests", open.length, `${drafts.length} draft · ${open.length - drafts.length} open for review`, "branch")}${stat("Release-ready revisions", ready.length, "All acceptance gates required", "shield")}${stat("Blocked revisions", blocked.length, "Latest exact-SHA CI failed", "alert", "orange")}${stat("Runtime connection", runtime ? "Linked" : "—", runtime ? "Authenticated local runtime" : "No execution telemetry connected", "layers")}</div><div class="release-banner"><span class="banner-icon">${icon("flag")}</span><div><h2>Next release gate: independent exact-SHA verification</h2><p>Require Python 3.11 + 3.12 results, coverage artifacts, and real container lifecycle evidence.</p></div><button class="text-button" data-view="gates">Review gates →</button></div><div class="overview-grid">${prTable(rows, true)}${gatesPanel(pr)}</div>${pipeline()}`;
  }
  function empty(title, text, action = "") {
    return `<div class="empty"><span class="empty-symbol">${icon("layers")}</span><h3>${title}</h3><p>${text}</p>${action}</div>`;
  }
  function runtimeView() {
    const cards =
      view === "missions"
        ? [
            [
              "Checkpointed execution",
              "Persisted steps are read from the native checkpoint store. Completed steps are reused on resume.",
              "checkpoint.py",
            ],
            [
              "Exclusive ownership",
              "The existing checkpoint claim returns a conflict when the same mission is already active.",
              "409 on active checkpoint",
            ],
            [
              "Failure propagation",
              "Failed worker results are not saved as completed steps. Resume retries incomplete work.",
              "No synthetic completions",
            ],
          ]
        : [
            [
              "Agent environments",
              "Per-agent containers and workspaces are available in the native runtime. No container is claimed to be running without telemetry.",
              "agentcontainer.py",
            ],
            [
              "Leases & heartbeats",
              "Inspect lease expiry and originating-node heartbeat evidence before trusting a worker. This snapshot contains no heartbeat measurements.",
              "Telemetry unavailable",
            ],
            [
              "Apple-Silicon squads",
              "Local Mac squad runs must supply evidence from the Mac itself; this Linux preview does not attest to them.",
              "scripts/mac-local-squad.sh",
            ],
          ];
    let body = `<div class="feature-grid">${cards.map(([title, text, file]) => `<div class="feature-card">${icon("layers")}<h3>${title}</h3><p>${text}</p><span class="mono">${file}</span></div>`).join("")}</div>`;
    if (!runtime)
      return (
        body +
        `<section class="panel">${empty("Connect the originating runtime", "Runtime state is protected by the existing AVRONE admin-token policy. Connect to inspect actual missions, agents and configured limits.", '<button class="button secondary" data-action="connect">Connect runtime</button>')}</section>` +
        pipeline()
      );
    if (view === "missions") {
      let checkpoints = runtime.checkpoints;
      const q = $("search").value.toLowerCase();
      checkpoints = checkpoints.filter((c) =>
        `${c.id} ${c.task}`.toLowerCase().includes(q),
      );
      if (
        advancedMissing() ||
        $("state-filter").value !== "all" ||
        $("runtime-filter").value !== "all"
      )
        checkpoints = [];
      return (
        body +
        `<section class="panel"><div class="panel-head"><h2>Persisted checkpoints <span class="count-chip">${checkpoints.length}</span></h2></div>${checkpoints.map((c) => `<div class="list-card"><div><h3>${esc(c.task || c.id)}</h3><p><span class="mono">${esc(c.id)}</span> · ${c.done_steps} saved steps · ${c.busy ? "Claimed" : c.done ? "Completed" : "Resumable"}</p></div><button class="button secondary push-right" data-checkpoint="${esc(c.id)}">Inspect checkpoint</button><button class="button secondary" data-resume="${esc(c.id)}" ${c.busy || c.done ? "disabled" : ""}>Resume</button></div>`).join("") || empty("No matching checkpoints", "No persisted missions match the selected filters. Create a mission only when the model backend and execution environment are configured.")}</section>`
      );
    }
    return (
      body +
      `<section class="panel"><div class="panel-head"><h2>Connected runtime</h2>${badge("unverified")}</div><div style="padding:20px"><p class="small-copy">${esc(runtime.notice)}</p><pre>${esc(JSON.stringify({ runner: runtime.runner, platform: runtime.platform, architecture: runtime.architecture, workspace: runtime.workspace, limits: runtime.limits, agents: runtime.agents }, null, 2))}</pre><p class="small-copy">Agent records shown as reported; this is not an attestation of container health, lane ownership or heartbeat freshness.</p></div></section>`
    );
  }
  function commitView() {
    const q = $("search").value.toLowerCase(),
      state = $("state-filter").value;
    const rows =
      advancedMissing() ||
      $("runtime-filter").value !== "all" ||
      !["all", "unverified"].includes(state)
        ? []
        : data.commits.filter((c) =>
            `${c.sha} ${c.title}`.toLowerCase().includes(q),
          );
    return `<section class="panel"><div class="panel-head"><h2>Default-branch commits <span class="count-chip">${rows.length}</span></h2><span class="mono">${esc(data.repository.defaultBranchRef.name)}</span></div>${rows.map((c) => `<div class="list-card">${icon("commit")}<div><h3>${link(c.url, c.title)}</h3><p><span class="sha">${esc(short(c.sha))}</span> &nbsp; ${date(c.date)}</p></div><div class="push-right">${badge("unverified")}</div></div>`).join("") || empty("No matching commits", "Try a different SHA, title or filter.")}</section><div class="help-note">A commit existing on the default branch does not establish release acceptance. Complete gate evidence is still required.</div>`;
  }
  function runView() {
    const q = $("search").value.trim().toLowerCase(),
      rt = $("runtime-filter").value,
      state = $("state-filter").value;
    const relevantPr = data.pullRequests.find(
      (p) => String(p.number) === q.replace(/^#/, ""),
    );
    const rows = data.runs.filter((r) => {
      const status = [
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
      ].includes(r.conclusion)
        ? "blocked"
        : "unverified";
      return (
        !advancedMissing() &&
        (state === "all" || state === status) &&
        (`${r.headSha} ${r.name} ${r.databaseId}`.toLowerCase().includes(q) ||
          relevantPr?.headRefOid === r.headSha) &&
        (rt === "all" || (r.jobs || []).some((j) => j.name === `test (${rt})`))
      );
    });
    return `<section class="panel"><div class="panel-head"><h2>Workflow runs <span class="count-chip">${rows.length}</span></h2><span class="mono" style="font-size:9px">LATEST 50 · SNAPSHOT</span></div><div class="table-wrap"><table><thead><tr><th>WORKFLOW</th><th>EXACT SHA</th><th>GITHUB CONCLUSION</th><th>JOBS / ARTIFACTS</th><th></th></tr></thead><tbody>${rows.map((r) => `<tr><td><strong>${esc(r.name)}</strong><div class="pr-meta">#${r.databaseId} · ${date(r.createdAt)}</div></td><td><span class="sha">${esc(short(r.headSha))}</span></td><td>${badge(r.conclusion || r.status)}</td><td>${r.evidenceComplete ? `${(r.jobs || []).length} jobs / ${(r.artifacts || []).length} artifacts` : "Not collected"}</td><td><button class="text-button" data-run="${r.databaseId}">Inspect →</button></td></tr>`).join("") || '<tr><td colspan="5"><div class="empty">No workflow evidence matches these filters.</div></td></tr>'}</tbody></table></div></section><div class="help-note">Workflow success is not release acceptance. Only exact-SHA, completed, named test jobs can satisfy Python gates; other gates need independently inspected evidence.</div>`;
  }
  function artifactsView() {
    const artifacts = data.runs.flatMap((r) =>
      (r.artifacts || []).map((a) => ({ ...a, run: r })),
    );
    const q = $("search").value.toLowerCase();
    const rows = artifacts.filter(
      (a) =>
        !advancedMissing() &&
        $("runtime-filter").value === "all" &&
        ["all", "unverified"].includes($("state-filter").value) &&
        `${a.name} ${a.run.headSha} ${a.id}`.toLowerCase().includes(q),
    );
    return `<div class="feature-grid"><div class="feature-card">${icon("repository")}<h3>Repository snapshot</h3><p>Public GitHub metadata captured ${date(data.capturedAt)}. ${esc(data.scope)}</p><button class="text-button" data-action="download">Download captured evidence ↓</button></div><div class="feature-card">${icon("shield")}<h3>Runtime hotfix v0.82.1</h3><p>Repository documentation reports local tests. The archive is not present in this repository; deployment remains unverified.</p>${link("https://github.com/doitupsolutions810-crow/Avrone/blob/main/docs/RUNTIME_GATEWAY_VERIFICATION_v0.82.1.md", "Read source note", "text-button")}</div><div class="feature-card">${icon("box")}<h3>Coverage requirements</h3><p>Require coverage.xml and coverage.json, validate their contents and tie both to the tested SHA. Artifact filenames alone never pass a gate.</p>${badge("unverified")}</div></div><section class="panel"><div class="panel-head"><h2>GitHub Actions artifacts <span class="count-chip">${rows.length}</span></h2></div>${rows.map((a) => `<div class="list-card">${icon("box")}<div><h3>${esc(a.name)}</h3><p>${esc(short(a.run.headSha))} · ${a.size_in_bytes} bytes · ${a.expired ? "Expired; cannot use as evidence" : "Contents not independently verified"}</p></div><span class="push-right">${link(a.run.url, "Open source run", "text-button")}</span></div>`).join("") || empty("No matching published artifacts", "No coverage or build artifact was returned for the captured CI runs under these filters. This does not mean coverage passed.")}</section>`;
  }
  function gateView() {
    const pr = prs().find((p) => p.state === "OPEN") || prs()[0];
    if (!pr)
      return empty(
        "No matching revision",
        "Choose a PR or commit with available source evidence.",
      );
    return `<div class="release-banner"><span class="banner-icon">${icon("shield")}</span><div><h2>Release acceptance is fail-closed</h2><p>Inspecting PR #${pr.number}, SHA ${esc(pr.headRefOid)}. Next required gate: ${esc(pr.nextGate)}.</p></div></div><div class="gate-grid">${pr.gates.map((g, i) => `<div class="feature-card"><div class="eyebrow">GATE ${String(i + 1).padStart(2, "0")}</div><h3 style="margin-top:12px">${esc(g.title)}</h3>${badge(g.state)}<p>${esc(g.requirement)}</p><p>${esc(g.reason)}</p>${g.url ? link(g.url, "Inspect source run", "text-button") : '<span class="mono">No accepted evidence attached</span>'}</div>`).join("")}</div><div class="help-note">This control room cannot merge a PR or override an acceptance gate. Existing GitHub merge history is preserved, not rewritten. Draft status remains a separate review blocker.</div>`;
  }
  function render() {
    if (!data) return;
    $("page-title").textContent = titles[view][0];
    $("page-description").textContent = titles[view][1];
    $("breadcrumb").textContent =
      view === "overview"
        ? "Overview"
        : {
            pulls: "Pull requests",
            commits: "Commits",
            missions: "Missions",
            agents: "Agent swarm",
            verification: "Test runs",
            artifacts: "Evidence & artifacts",
            gates: "Release gates",
          }[view];
    document.querySelectorAll(".nav-item[data-view]").forEach((el) => {
      el.classList.toggle("active", el.dataset.view === view);
      if (el.dataset.view === view) el.setAttribute("aria-current", "page");
      else el.removeAttribute("aria-current");
    });
    $("content").innerHTML =
      view === "overview"
        ? overview()
        : view === "pulls"
          ? prTable(prs())
          : view === "commits"
            ? commitView()
            : view === "missions" || view === "agents"
              ? runtimeView()
              : view === "verification"
                ? runView()
                : view === "artifacts"
                  ? artifactsView()
                  : gateView();
  }
  function openDialog(content, eyebrow = "EVIDENCE INSPECTOR") {
    $("dialog-eyebrow").textContent = eyebrow;
    $("dialog-content").innerHTML = content;
    if (!$("detail-dialog").open) $("detail-dialog").showModal();
  }
  function detailGates(pr) {
    return pr.gates
      .map(
        (g) =>
          `<div class="detail-gate">${badge(g.state)}<div><strong>${esc(g.title)}</strong><p>${esc(g.reason)}</p><p>${esc(g.requirement)}</p>${g.url ? link(g.url, "View source") : ""}</div></div>`,
      )
      .join("");
  }
  function detail(number) {
    const pr = data.pullRequests.find((p) => p.number === number);
    if (!pr) return;
    activePr = number;
    openDialog(
      `<h2 class="dialog-title">${esc(pr.title)}</h2><div class="detail-meta">${badge(pr.isDraft ? "draft" : pr.state.toLowerCase())}${badge(pr.mergeState)}${badge(pr.verification)}<span class="mono">#${pr.number}</span></div><p class="mono">${esc(pr.headRefOid)}</p>${link(pr.url, "Open pull request", "button secondary")}<div class="dialog-section"><h3>Required acceptance evidence</h3>${detailGates(pr)}</div><div class="help-note">Next gate: ${esc(pr.nextGate)}. ${pr.isDraft ? "This PR is also a draft and is not review-ready." : ""} Merge state comes from GitHub, not from verification labels.</div>`,
    );
  }
  function connect() {
    openDialog(
      `<h2 class="dialog-title">Connect runtime</h2><p class="small-copy">Use the admin token configured on this AVRONE server. It stays in this tab's memory and is never persisted in browser storage. Repository snapshots need no token.</p><form id="connect-form"><label class="form-field">Admin token<input name="token" type="password" required autocomplete="off"></label><p id="form-error" class="form-error" role="alert"></p><div class="form-actions">${runtime ? '<button type="button" class="button secondary" data-action="disconnect">Disconnect</button>' : ""}<button class="button primary" type="submit">Connect runtime</button></div></form>`,
      "OPERATOR ACCESS",
    );
  }
  function mission(checkpoint = "") {
    if (!runtime) {
      connect();
      return;
    }
    openDialog(
      `<h2 class="dialog-title">${checkpoint ? "Resume" : "New"} durable mission</h2><p class="small-copy">This starts real agent work on the connected runtime. A working model backend is required. ${checkpoint ? "Enter the original full task exactly; the server rejects incompatible resumes." : "Choose a unique checkpoint ID to persist progress and resume later."}</p><form id="mission-form"><label class="form-field">Checkpoint ID<input name="checkpoint_id" required pattern="[A-Za-z0-9_-]{1,64}" maxlength="64" value="${esc(checkpoint)}" ${checkpoint ? "readonly" : ""} placeholder="verification-round-21"></label><label class="form-field">Task<textarea name="message" required maxlength="200000" placeholder="Describe the work and acceptance requirements"></textarea></label><label class="form-field">Mode<select name="mode"><option value="squad">Squad</option><option value="deep">Deep analysis</option></select></label><label class="form-field">Squad size<input name="squad_size" type="number" min="1" max="12" value="3" required></label><p id="form-error" class="form-error" role="alert"></p><div class="form-actions"><button class="button primary" type="submit">${checkpoint ? "Resume mission" : "Start mission"}</button></div></form>`,
      "CHECKPOINTED EXECUTION",
    );
  }
  async function reloadRuntime() {
    runtime = await request("/api/control-room/runtime");
    $("connection-label").textContent = "Runtime connected";
  }
  async function load() {
    if (loading) return;
    loading = true;
    $("refresh").disabled = true;
    try {
      data = await request("/api/control-room/repository");
      $("nav-pr-count").textContent = data.pullRequests.filter(
        (p) => p.state === "OPEN",
      ).length;
      $("snapshot-date").textContent =
        `CAPTURED ${date(data.capturedAt).toUpperCase()}`;
      $("footer-source").textContent =
        `PUBLIC GITHUB SNAPSHOT · ${date(data.capturedAt).toUpperCase()}`;
      if (runtime) {
        try {
          await reloadRuntime();
        } catch (e) {
          runtime = null;
          token = "";
          $("connection-label").textContent = "Runtime disconnected";
          toast(e.message);
        }
      }
      render();
    } catch (e) {
      $("content").innerHTML = empty(
        "Evidence unavailable",
        esc(e.message),
        '<button class="button secondary" data-action="retry">Retry loading</button>',
      );
    } finally {
      loading = false;
      $("refresh").disabled = false;
    }
  }
  document.addEventListener("click", async (event) => {
    const b = event.target.closest("button");
    if (!b) return;
    if (b.dataset.view) {
      view = b.dataset.view;
      render();
    } else if (b.dataset.detail) detail(Number(b.dataset.detail));
    else if (b.dataset.action === "connect" || b.id === "connect-runtime")
      connect();
    else if (b.id === "new-mission") mission();
    else if (b.dataset.resume) mission(b.dataset.resume);
    else if (b.id === "close-dialog") $("detail-dialog").close();
    else if (b.id === "more-filters") {
      $("extended-filters").hidden = !$("extended-filters").hidden;
      b.setAttribute("aria-expanded", String(!$("extended-filters").hidden));
    } else if (b.id === "clear-filters") {
      ["round-filter", "lane-filter", "agent-filter", "search"].forEach(
        (id) => ($(id).value = ""),
      );
      $("runtime-filter").value = "all";
      $("state-filter").value = "all";
      render();
    } else if (b.id === "refresh" || b.dataset.action === "retry") {
      await load();
      if (data)
        toast("Persisted snapshot reloaded. This does not refresh GitHub.");
    } else if (b.id === "compare") {
      const p = [...selected].map((id) =>
        data.pullRequests.find((pr) => pr.number === id),
      );
      if (p.length !== 2) return;
      openDialog(
        `<h2 class="dialog-title">Compare revisions</h2><p class="small-copy">Evidence is evaluated separately for each exact SHA.</p><div class="compare">${p.map((pr) => `<section><h3>#${pr.number} · ${esc(pr.title)}</h3><p class="mono">${esc(short(pr.headRefOid))}</p><div class="state-stack">${badge(pr.mergeState)}${badge(pr.verification)}</div>${detailGates(pr)}</section>`).join("")}</div>`,
        "REVISION COMPARISON",
      );
    } else if (b.dataset.run) {
      const r = data.runs.find((r) => r.databaseId === Number(b.dataset.run));
      openDialog(
        `<h2 class="dialog-title">${esc(r.name)} · ${r.databaseId}</h2><p class="mono">${esc(r.headSha)}</p><div class="detail-meta">${badge(r.status)}${badge(r.conclusion || "pending")}</div>${link(r.url, "Open GitHub run", "button secondary")}<div class="dialog-section"><h3>Recorded jobs</h3>${r.jobs?.length ? r.jobs.map((j) => `<div class="list-card"><div><h3>${esc(j.name)}</h3>${link(j.html_url, "View job")}</div><span class="push-right">${badge(j.conclusion || j.status)}</span></div>`).join("") : '<p class="small-copy">No jobs were returned. A failed workflow without job evidence is not proof that tests ran.</p>'}</div><div class="help-note">${r.evidenceComplete ? "Job and artifact listings were fully captured for this run." : "Evidence has not been completely collected for this run."} Artifact contents have not been independently validated.</div>`,
      );
    } else if (b.dataset.checkpoint) {
      try {
        const c = await request(
          `/api/checkpoints/${encodeURIComponent(b.dataset.checkpoint)}`,
        );
        openDialog(
          `<h2 class="dialog-title">Checkpoint ${esc(c.id)}</h2><pre>${esc(JSON.stringify(c, null, 2))}</pre>`,
          "DURABLE CHECKPOINT",
        );
      } catch (e) {
        toast(e.message);
      }
    } else if (b.dataset.action === "disconnect") {
      token = "";
      runtime = null;
      $("connection-label").textContent = "Runtime not connected";
      $("detail-dialog").close();
      render();
    } else if (b.dataset.action === "download") {
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "avrone-repository-evidence.json";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  });
  document.addEventListener("change", (event) => {
    if (event.target.dataset.select) {
      const id = Number(event.target.dataset.select);
      if (event.target.checked) {
        if (selected.size >= 2) {
          event.target.checked = false;
          toast("Select only two revisions for comparison.");
          return;
        }
        selected.add(id);
      } else selected.delete(id);
      render();
    }
  });
  for (const id of ["state-filter", "runtime-filter", "repository"])
    $(id).addEventListener("change", render);
  for (const id of ["search", "round-filter", "lane-filter", "agent-filter"])
    $(id).addEventListener("input", render);
  document.addEventListener("submit", async (event) => {
    if (!["connect-form", "mission-form"].includes(event.target.id)) return;
    event.preventDefault();
    const form = event.target,
      button = form.querySelector("[type=submit]");
    button.disabled = true;
    try {
      const values = Object.fromEntries(new FormData(form));
      if (form.id === "connect-form") {
        token = values.token;
        try {
          await reloadRuntime();
        } catch (e) {
          token = "";
          throw e;
        }
        $("detail-dialog").close();
        render();
        toast("Connected to the authenticated AVRONE runtime.");
      } else {
        const result = await request("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...values,
            squad_size: Number(values.squad_size),
            background: true,
          }),
        });
        $("detail-dialog").close();
        view = "missions";
        await reloadRuntime();
        render();
        toast(
          `Mission ${result.run_id} submitted. Submission is not completion or verification.`,
        );
      }
    } catch (e) {
      if ($("form-error")) $("form-error").textContent = e.message;
      else toast(e.message);
    } finally {
      button.disabled = false;
    }
  });
  $("detail-dialog").addEventListener("click", (event) => {
    if (event.target === $("detail-dialog")) {
      const r = $("detail-dialog").getBoundingClientRect();
      if (
        event.clientX < r.left ||
        event.clientX > r.right ||
        event.clientY < r.top ||
        event.clientY > r.bottom
      )
        $("detail-dialog").close();
    }
  });
  load();
})();
