import { randomUUID, createHash, timingSafeEqual } from "node:crypto";

// This endpoint never routes through findAgent's permissive fallback to grok-main.
export function createResearchHandler({ runAgent, tools, emit, token = () => process.env.GROK_BRIDGE_TOKEN }) {
  return async (req, res) => {
    const expected = String(token() || "");
    if (!expected) return res.status(503).json({ error: "research bridge is not configured" });
    const supplied = String(req.headers.authorization || "");
    const a = Buffer.from(supplied), b = Buffer.from(`Bearer ${expected}`);
    if (a.length !== b.length || !timingSafeEqual(a, b)) {
      return res.status(401).json({ error: "unauthorized" });
    }
    const { message, correlation_id = "" } = req.body || {};
    if (typeof message !== "string" || !message.trim() || message.length > 8000 ||
        typeof correlation_id !== "string" || !/^[A-Za-z0-9_.-]{0,80}$/.test(correlation_id)) {
      return res.status(400).json({ error: "invalid message or correlation id" });
    }
    const run_id = `research-${randomUUID()}`;
    const agent = {
      id: "grok-researcher",
      systemPrompt: "Independently research the task. Use read-only sources, cite URLs, distinguish observations from inference and report unavailable evidence.",
      tools: ["browser", "web.fetch"]
    };
    const available = tools.filter(t => agent.tools.includes(t.name)).map(t => t.name === "browser"
      ? { ...t, run: (args, ctx) => t.run({ ...args, action: "read" }, ctx) } : t);
    try {
      emit(run_id, "run.start", { mode: "bridge-research", correlation_id, agent: agent.id });
      const answer = await runAgent(agent, message.trim(), available, { runId: run_id, maxSteps: 8 });
      if (typeof answer !== "string" || !answer.trim() || answer.startsWith("Reached max tool steps")) {
        throw new Error("research incomplete");
      }
      if (Buffer.byteLength(answer) > 48000) throw new Error("research answer exceeds receipt limit");
      const receipt = { run_id, correlation_id, agent: agent.id,
        answer_sha256: createHash("sha256").update(answer).digest("hex"), completed_at: new Date().toISOString() };
      emit(run_id, "run.done", { ...receipt, answer });
      return res.json({ ok: true, answer, receipt });
    } catch {
      emit(run_id, "run.error", { correlation_id, reason: "research unavailable" });
      return res.status(502).json({ error: "research unavailable", run_id, correlation_id });
    }
  };
}
