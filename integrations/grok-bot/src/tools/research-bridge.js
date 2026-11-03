import { randomUUID, createHash, timingSafeEqual } from "node:crypto";
import { scalarText, failureText } from "./bridge-values.js";

// This endpoint never routes through findAgent's permissive fallback to grok-main.
export function createResearchHandler({ runAgent, tools, emit, token = () => process.env.GROK_BRIDGE_TOKEN, maxInFlight = 4 }) {
  if (!Number.isInteger(maxInFlight) || maxInFlight < 1 || maxInFlight > 16) throw RangeError("invalid research admission limit");
  let active = 0;
  return async (req, res) => {
    let expected;
    try { expected = token(); }
    catch { return res.status(503).json({ error: "research bridge is not configured" }); }
    if (typeof expected !== "string" || !expected || /[\s\x00-\x1f\x7f]/.test(expected)) return res.status(503).json({ error: "research bridge is not configured" });
    const supplied = req.headers.authorization;
    if (typeof supplied !== "string") return res.status(401).json({ error: "unauthorized" });
    const a = Buffer.from(supplied), b = Buffer.from(`Bearer ${expected}`);
    if (a.length !== b.length || !timingSafeEqual(a, b)) {
      return res.status(401).json({ error: "unauthorized" });
    }
    const { message, correlation_id = "" } = req.body || {};
    if (!scalarText(message, 8000) || !message.trim() ||
        typeof correlation_id !== "string" || !/^[A-Za-z0-9_.-]{0,80}$/.test(correlation_id)) {
      return res.status(400).json({ error: "invalid message or correlation id" });
    }
    if (active >= maxInFlight) return res.status(429).json({ error: "research bridge is busy" });
    active++;
    const run_id = `research-${randomUUID()}`;
    const agent = {
      id: "grok-researcher",
      systemPrompt: "Independently research the task. Use read-only sources, cite URLs, distinguish observations from inference and report unavailable evidence.",
      tools: ["browser", "web.fetch"]
    };
    try {
      const available = tools.filter(t => t && agent.tools.includes(t.name)).map(t => t.name === "browser"
        ? { ...t, run: (args, ctx) => t.run({ ...args, action: "read" }, ctx) } : t);
      if (emit(run_id, "run.start", { mode: "bridge-research", correlation_id, agent: agent.id }) === false) throw Error("audit unavailable");
      const answer = await runAgent(agent, message.trim(), available, { runId: run_id, maxSteps: 8 });
      if (failureText(answer)) {
        throw new Error("research incomplete");
      }
      if (Buffer.byteLength(answer) > 48000) throw new Error("research answer exceeds receipt limit");
      const receipt = { run_id, correlation_id, agent: agent.id,
        answer_sha256: createHash("sha256").update(answer).digest("hex"), completed_at: new Date().toISOString() };
      if (emit(run_id, "run.done", { ...receipt, answer }) === false) throw Error("audit unavailable");
      return res.json({ ok: true, answer, receipt });
    } catch {
      try { emit(run_id, "run.error", { correlation_id, reason: "research unavailable" }); } catch {}
      return res.status(502).json({ error: "research unavailable", run_id, correlation_id });
    } finally {
      active--;
    }
  };
}
