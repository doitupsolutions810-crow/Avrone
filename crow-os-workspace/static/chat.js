async function showChat() {
  const agents = await api("/api/chat/agents");
  const threads = await api("/api/chat/threads");
  if (!state.agentId && agents.agents.length) state.agentId = agents.agents[0].id;
  const agentThreads = threads.threads.filter((thread) => !state.agentId || thread.agent_id === state.agentId);
  if (!state.threadId && agentThreads.length) state.threadId = agentThreads[0].id;
  const detail = state.threadId ? await api("/api/chat/threads/" + encodeURIComponent(state.threadId)) : null;
  const connectors = await api("/api/connectors");
  const messages = (detail && detail.messages || []).map((message) =>
    `<article class="bubble ${esc(message.role)}"><strong>${esc(message.role)}</strong>\n${esc(message.content)}</article>`
  ).join("") || `<p class="muted">Ask an agent to run a test or propose a patch. Patches stay unmerged and unverified until the gates pass.</p>`;
  const activities = (detail && detail.activities || []).map((item) =>
    `<article class="card"><span class="chip chip-open">${esc(item.kind)}</span> <span class="muted">${esc(item.ts)}</span><div>${esc(item.summary)}</div></article>`
  ).join("") || `<p class="muted">Tool activity shows up here.</p>`;
  const change = detail && detail.change;
  const banner = connectorsBanner(connectors);
  $("#main").innerHTML = `${banner}<div class="chat-shell">
    <aside class="sidebar">
      <h2>Agents</h2>
      <div class="agent-list">${agents.agents.map((agent) =>
        `<button type="button" data-agent="${esc(agent.id)}" class="${agent.id === state.agentId ? "active" : ""}">${esc(agent.name)}<div class="muted">${esc(agent.backend)} ${esc(agent.model || "")}</div></button>`
      ).join("")}</div>
      <button type="button" id="new-thread">New thread</button>
      <h3>Threads</h3>
      <div class="thread-list">${agentThreads.map((thread) =>
        `<button type="button" data-thread="${esc(thread.id)}" class="${thread.id === state.threadId ? "active" : ""}">${esc(thread.title)}<div class="muted">${esc(thread.status)} · ${esc(thread.branch)}</div></button>`
      ).join("") || `<p class="muted">No threads yet.</p>`}</div>
    </aside>
    <section>
      <div class="transcript" id="transcript">${messages}</div>
      <form class="composer" id="composer">
        <textarea name="text" placeholder="Ask the agent to build or test something in this repo"></textarea>
        <div>
          <button class="primary" type="submit">Send</button>
          <button type="button" class="danger" id="cancel-chat">Cancel</button>
        </div>
      </form>
    </section>
    <aside class="activity">
      <h2>Activity</h2>
      ${change ? `<p>${chips(change.chips)}</p><p class="hash">${esc(change.sha || "")}</p>` : `<p class="muted">No patch yet.</p>`}
      ${activities}
      <h3>Live log</h3>
      <pre id="chat-log">${esc(state.chatLog || "")}</pre>
    </aside>
  </div>`;
  $("#main").querySelectorAll("[data-agent]").forEach((button) => {
    button.onclick = () => { state.agentId = button.dataset.agent; state.threadId = null; state.chatLog = ""; showChat(); };
  });
  $("#main").querySelectorAll("[data-thread]").forEach((button) => {
    button.onclick = () => { state.threadId = button.dataset.thread; state.chatLog = ""; showChat(); };
  });
  $("#new-thread").onclick = async () => {
    const thread = await api("/api/chat/threads", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ agent_id: state.agentId, title: "Thread" }),
    });
    state.threadId = thread.id;
    state.chatLog = "";
    showChat();
  };
  $("#composer").onsubmit = async (event) => {
    event.preventDefault();
    const text = event.target.text.value.trim();
    if (!text) return;
    let threadId = state.threadId;
    if (!threadId) {
      const thread = await api("/api/chat/threads", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ agent_id: state.agentId, title: text.slice(0, 48) }),
      });
      threadId = thread.id;
      state.threadId = threadId;
    }
    await api(`/api/chat/threads/${threadId}/messages`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    event.target.text.value = "";
    state.chatLog = "";
    watchChat(threadId);
  };
  $("#cancel-chat").onclick = async () => {
    if (state.threadId) await api(`/api/chat/threads/${state.threadId}/cancel`, { method: "POST" });
  };
  if (detail && detail.status === "running") watchChat(detail.id);
}

function connectorsBanner(connectors) {
  const parts = [];
  const ollama = connectors.ollama || {};
  if (ollama.state !== "reachable") {
    const binary = ollama.binary && ollama.binary.found === "yes" ? `binary ${ollama.binary.path}` : "ollama binary not on PATH";
    parts.push(`Ollama unreachable at ${ollama.host || "http://localhost:11434"} (${binary}).`);
  }
  if (connectors.cursor && connectors.cursor.state !== "ok") parts.push(connectors.cursor.detail);
  if (connectors.xai && connectors.xai.state !== "ok") parts.push(connectors.xai.detail);
  return parts.length ? `<div class="banner">${esc(parts.join(" "))}</div>` : "";
}

function watchChat(id) {
  if (state.chatSource) state.chatSource.close();
  const source = new EventSource(`/api/chat/threads/${encodeURIComponent(id)}/stream`);
  state.chatSource = source;
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.type === "token" && event.text) {
      const transcript = document.getElementById("transcript");
      let live = document.getElementById("live-bubble");
      if (!live && transcript) {
        live = document.createElement("article");
        live.id = "live-bubble";
        live.className = "bubble assistant";
        live.textContent = "assistant\n";
        transcript.appendChild(live);
      }
      if (live) live.textContent += event.text;
    }
    if ((event.type === "log" && event.line) || (event.type === "token" && event.text)) {
      state.chatLog += event.line || event.text;
      const node = $("#chat-log");
      if (node) node.textContent = state.chatLog.slice(-12000);
    }
    if (event.type === "status" && ["idle", "failed", "cancelled"].includes(event.status)) {
      source.close();
      showChat();
    }
  };
}

async function showSettings() {
  const data = await api("/api/connectors");
  const agents = await api("/api/chat/agents");
  const ollama = data.ollama || {};
  const binary = ollama.binary || {};
  const models = (ollama.models || []).map((model) =>
    `<tr><td>${esc(model.name)}</td><td>${esc(model.size ?? "")}</td><td class="hash">${esc(model.digest || "")}</td><td>${esc(model.modified_at || "")}</td></tr>`
  ).join("");
  const agentRows = agents.agents.map((agent) => `<li>
    ${esc(agent.name)} · ${esc(agent.backend)}
    <input data-model="${esc(agent.id)}" value="${esc(agent.model || "")}" placeholder="default model">
    <button type="button" data-save="${esc(agent.id)}">Save model</button>
  </li>`).join("");
  $("#main").innerHTML = `
    <div class="card"><h2>Ollama health</h2>
      ${ollama.state === "reachable" ? `<span class="chip chip-verified">reachable</span>` : `<span class="chip chip-failed">unreachable</span>`}
      <p>${esc(ollama.detail || "Ollama unreachable")} · host ${esc(ollama.host || "")}</p>
      <p>Binary ${binary.found === "yes" ? esc(binary.path) : "not on PATH"} ${esc(binary.version || "")}</p>
      <p class="muted">${esc(ollama.error || "")}</p>
    </div>
    <div class="card"><h2>Installed models</h2>
      <table><thead><tr><th>Name</th><th>Size</th><th>Digest</th><th>Modified</th></tr></thead><tbody>${models || `<tr><td colspan="4">No models. Ollama is unreachable or has none installed.</td></tr>`}</tbody></table>
      <form id="pull" class="row"><input name="name" placeholder="model to pull, for example llama3.2"><button class="primary" type="submit">Pull model</button></form>
      <pre id="pull-log"></pre>
    </div>
    <div class="card"><h2>Default model per agent</h2><ul>${agentRows}</ul>
      <form id="new-agent" class="row">
        <input name="name" placeholder="agent name" required>
        <select name="backend"><option value="ollama">ollama</option><option value="cursor">cursor cloud</option><option value="xai">xai grok</option></select>
        <input name="model" placeholder="model id">
        <button type="submit">Add agent</button>
      </form>
    </div>
    <div class="card"><h2>Connectors</h2>
      <p><span class="chip ${data.cursor.state === "ok" ? "chip-verified" : "chip-unverified"}">Cursor</span> ${esc(data.cursor.detail)}</p>
      <p><span class="chip ${data.xai.state === "ok" ? "chip-verified" : "chip-unverified"}">xAI</span> ${esc(data.xai.detail)}</p>
    </div>`;
  $("#main").querySelectorAll("[data-save]").forEach((button) => {
    button.onclick = async () => {
      const input = document.querySelector(`[data-model="${button.dataset.save}"]`);
      await api(`/api/chat/agents/${button.dataset.save}/model`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ model: input.value }),
      });
      showSettings();
    };
  });
  $("#new-agent").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    await api("/api/chat/agents", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: form.name.value, backend: form.backend.value, model: form.model.value }),
    });
    showSettings();
  };
  $("#pull").onsubmit = async (event) => {
    event.preventDefault();
    const name = event.target.name.value.trim();
    const log = $("#pull-log");
    log.textContent = "";
    const response = await fetch("/api/ollama/pull", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!response.ok || !response.body) {
      log.textContent = await response.text();
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      log.textContent += decoder.decode(chunk.value);
    }
  };
}

async function showSystem() {
  const data = await api("/api/system");
  const tool = data.toolchain || {};
  const gitRows = (data.git_config || []).map((row) => `<tr><td>${esc(row.key)}</td><td>${esc(row.value)}</td></tr>`).join("");
  const envRows = Object.entries(data.env || {}).map(([key, value]) => `<tr><td>${esc(key)}</td><td>${esc(value)}</td></tr>`).join("");
  const files = (data.config_files || []).map((file) => `<h3>${esc(file.path)}</h3><pre>${esc(file.text || file.error || "")}</pre>`).join("");
  const ollama = (data.ollama && data.ollama.probe) || {};
  $("#main").innerHTML = `
    <div class="card"><h2>System configuration</h2>
      <p class="muted">${esc(data.inheritance || "")} Secret-like values are masked.</p>
      <button type="button" id="system-refresh">Re-read host</button>
      <div class="grid">
        <div class="stat"><b>${esc(data.python || "")}</b><span>python</span></div>
        <div class="stat"><b>${esc(tool.git || "")}</b><span>git</span></div>
        <div class="stat"><b>${esc(tool.pytest || "")}</b><span>pytest</span></div>
        <div class="stat"><b>${esc(tool.ollama || "not installed")}</b><span>ollama</span></div>
      </div>
      <p>${esc(data.platform || "")}</p>
      <p>Ollama ${esc(ollama.state || "")} at ${esc((data.ollama && data.ollama.host) || "")}. Models: ${esc((ollama.models || []).join(", ") || "none")}.</p>
    </div>
    <div class="card"><h2>Git config</h2><table><tbody>${gitRows}</tbody></table></div>
    <div class="card"><h2>Environment (${esc(data.env_count || 0)} process variables, sensitive ones masked)</h2>
      <table><tbody>${envRows}</tbody></table></div>
    <div class="card"><h2>Repo config files</h2>${files || `<p class="muted">No .env or pyproject.toml in the checkout root.</p>`}</div>`;
  $("#system-refresh").onclick = async () => {
    await api("/api/system/refresh", { method: "POST" });
    showSystem();
  };
}
