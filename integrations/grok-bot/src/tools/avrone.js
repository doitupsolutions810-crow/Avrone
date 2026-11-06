import { scalarText, failureText, parseReply } from "./bridge-values.js";

const MAX_PROMPT_CHARS = 8000;
const MAX_RESPONSE_BYTES = 64 * 1024;
const DEFAULT_TIMEOUT_MS = 180000;

function configuredOrigin(value) {
  const raw = typeof value === "string" ? value.trim() : "";
  if (!raw) return { error: "AVRONE_URL not set — the system bridge is disabled." };
  if (/[\x00-\x1f\x7f\\?#]/.test(value)) return { error: "AVRONE_URL must be a valid HTTP(S) origin." };
  let url;
  try { url = new URL(raw); }
  catch { return { error: "AVRONE_URL must be a valid HTTP(S) origin." }; }
  if (!["http:", "https:"].includes(url.protocol) || !url.hostname ||
      url.username || url.password || !["", "/"].includes(url.pathname) ||
      url.search || url.hash) {
    return { error: "AVRONE_URL must be an HTTP(S) origin without credentials, path, query, or fragment." };
  }
  return { origin: url.origin };
}

class ResponseError extends Error {}

async function readBoundedText(response, limit = MAX_RESPONSE_BYTES) {
  if ((response.headers?.get?.("content-encoding") || "identity").trim().toLowerCase() !== "identity") {
    await response.body?.cancel?.().catch(() => {});
    throw new ResponseError("encoded response is not supported.");
  }
  const length = (response.headers?.get?.("content-length") || "").trim();
  if (length && (!/^[0-9]+$/.test(length) || length.length > 20)) throw new ResponseError("invalid response length.");
  const declared = Number(length || 0);
  if (Number.isFinite(declared) && declared > limit) {
    await response.body?.cancel?.().catch(() => {});
    throw new ResponseError("AVRONE response exceeded the 64 KiB limit.");
  }
  if (response.body && typeof response.body.getReader === "function") {
    const reader = response.body.getReader();
    const chunks = [];
    let total = 0;
    let complete = false;
    try {
      while (true) {
        const item = await reader.read();
        if (item.done) break;
        total += item.value.byteLength;
        if (total > limit) {
          await reader.cancel().catch(() => {});
          throw new ResponseError("AVRONE response exceeded the 64 KiB limit.");
        }
        chunks.push(Buffer.from(item.value));
      }
      complete = true;
      return new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks, total));
    } finally {
      if (!complete) await reader.cancel().catch(() => {});
      reader.releaseLock?.();
    }
  }
  const text = await response.text();
  if (Buffer.byteLength(text, "utf8") > limit) {
    throw new ResponseError("AVRONE response exceeded the 64 KiB limit.");
  }
  return text;
}

export function createAvroneTool({
  getBaseUrl = () => process.env.AVRONE_URL,
  fetchImpl = (...args) => fetch(...args),
  timeoutMs = DEFAULT_TIMEOUT_MS
} = {}) {
  return {
    name: "avrone.squad",
    description:
      "Delegate a complete task to AVRONE's independent agent squad. Returns its merged answer and run receipt. Use for multi-step work that benefits from independent agents.",
    parameters: {
      type: "object",
      properties: {
        prompt: { type: "string", description: "The complete, self-contained task for AVRONE's squad" },
        mode: { type: "string", enum: ["squad", "deep"], description: "squad (default) or deep (adds adversarial review waves)" }
      },
      required: ["prompt"]
    },
    async run(args = {}) {
      if (!args || typeof args !== "object" || Array.isArray(args) || !scalarText(args.prompt)) return { error: "prompt must be a string" };
      const prompt = args.prompt.trim();
      if (!prompt) return { error: "prompt required" };
      if (!scalarText(args.prompt, MAX_PROMPT_CHARS)) {
        return { error: "prompt exceeds the 8000-character limit." };
      }
      if (args.mode !== undefined && !["squad", "deep"].includes(args.mode)) return { error: "mode must be squad or deep" };

      const configured = configuredOrigin(getBaseUrl());
      if (configured.error) return { error: configured.error };
      const mode = args.mode === "deep" ? "deep" : "squad";
      const safeTimeout = Math.trunc(Math.max(1000, Math.min(300000, Number(timeoutMs) || DEFAULT_TIMEOUT_MS)));
      let response;
      try {
        response = await fetchImpl(configured.origin + "/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity",
            ...(process.env.AVRONE_ADMIN_TOKEN ? { "X-Admin-Token": process.env.AVRONE_ADMIN_TOKEN } : {}) },
          body: JSON.stringify({ message: prompt, mode }),
          redirect: "manual",
          signal: AbortSignal.timeout(safeTimeout)
        });
      } catch (error) {
        const reason = error?.name === "TimeoutError" || error?.name === "AbortError"
          ? "request timed out"
          : "service unreachable";
        return { error: "AVRONE " + mode + " bridge " + reason + "." };
      }

      if (response.status >= 300 && response.status < 400) {
        return { error: "AVRONE " + mode + " bridge refused HTTP redirect (" + response.status + ")." };
      }
      if (!response.ok) {
        return { error: "AVRONE " + mode + " bridge failed: HTTP " + response.status + "." };
      }

      let raw;
      try { raw = await readBoundedText(response); }
      catch (error) {
        return { error: "AVRONE " + mode + " bridge response rejected: " + (error instanceof ResponseError ? error.message : "invalid response body.") };
      }

      let out;
      try { out = parseReply(raw); }
      catch { return { error: "AVRONE bridge returned invalid JSON." }; }

      const runId = out && typeof out.run_id === "string" ? out.run_id.trim() : "";
      const answer = out && typeof out.answer === "string" ? out.answer.trim() : "";
      if (!runId) return { error: "AVRONE bridge response omitted its run_id receipt." };
      if (!answer) return { error: "AVRONE bridge response omitted a textual answer." };
      if (("ok" in out && out.ok !== true) || ("state" in out && out.state !== "COMPLETE")
          || ("mode" in out && out.mode !== mode)
          || ("status" in out && !["complete","completed","done","success","succeeded"].includes(out.status))) {
        return { error: "AVRONE returned incomplete run metadata." };
      }
      if (!/^[A-Za-z0-9_.-]{1,80}$/.test(runId) || failureText(answer)) return { error: "AVRONE returned an incomplete run." };
      if (out.agents !== undefined && (!Array.isArray(out.agents) || !out.agents.length
          || out.agents.some(a => !a || typeof a !== "object" || failureText(a.result)))) {
        return { error: "AVRONE returned incomplete worker evidence." };
      }
      if (mode === "deep" && ["no_reviews", "lead_failed"].includes(out.stop_reason)) return { error: "AVRONE deep review did not complete." };

      return {
        ok: true,
        mode,
        runId,
        answer: answer.slice(0, 12000),
        truncated: answer.length > 12000
      };
    }
  };
}

export const avroneTool = createAvroneTool();
