import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";

function waitForMatch(proc, regex, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => {
      proc.kill("SIGKILL");
      reject(new Error("Timed out waiting for readiness; output=" + output));
    }, timeoutMs);
    const onData = chunk => {
      output += chunk.toString();
      const match = output.match(regex);
      if (match) {
        clearTimeout(timer);
        proc.stdout.off("data", onData);
        resolve({ match, output });
      }
    };
    proc.stdout.on("data", onData);
    proc.once("error", error => { clearTimeout(timer); reject(error); });
    proc.once("exit", (code, signal) => {
      clearTimeout(timer);
      reject(new Error("Supervisor exited before readiness: " + code + "/" + signal + " " + output));
    });
  });
}

test("supervisor observes child SIGTERM exit and reaps the child", async t => {
  const workerSource = "process.on('SIGTERM', () => process.exit(0)); setInterval(() => {}, 1000)";
  const supervisorSource = [
    'const { spawn } = require("node:child_process");',
    'const child = spawn(process.execPath, ["-e", ' + JSON.stringify(workerSource) + '], { stdio: "ignore" });',
    'child.once("exit", (code, signal) => {',
    '  process.stdout.write("CHILD_EXITED=" + code + ":" + signal + "\\n", () => process.exit(code === 0 ? 0 : 3));',
    '});',
    'child.once("spawn", () => process.stdout.write("CHILD_PID=" + child.pid + "\\n"));',
    'setInterval(() => {}, 1000);'
  ].join("\n");
  const supervisor = spawn(process.execPath, ["-e", supervisorSource], { stdio: ["ignore", "pipe", "pipe"] });
  t.after(() => { if (supervisor.exitCode === null && supervisor.signalCode === null) supervisor.kill("SIGKILL"); });
  const ready = await waitForMatch(supervisor, /CHILD_PID=(\\d+)/);
  const childPid = Number(ready.match[1]);
  const exitPromise = once(supervisor, "exit");
  process.kill(childPid, "SIGTERM");
  const [code, signal] = await exitPromise;
  assert.equal(code, 0);
  assert.equal(signal, null);
  assert.throws(() => process.kill(childPid, 0), error => error && error.code === "ESRCH");
});

test("a hanging Node worker exits on SIGTERM within a bounded deadline", async t => {
  const worker = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" });
  t.after(() => { if (worker.exitCode === null && worker.signalCode === null) worker.kill("SIGKILL"); });
  await once(worker, "spawn");
  const exitPromise = once(worker, "exit");
  worker.kill("SIGTERM");
  const [code, signal] = await Promise.race([
    exitPromise,
    new Promise((_, reject) => setTimeout(() => reject(new Error("SIGTERM deadline exceeded")), 3000))
  ]);
  assert.equal(code, null);
  assert.equal(signal, "SIGTERM");
});

test("Node process exit codes are preserved for supervisor failure propagation", async t => {
  const worker = spawn(process.execPath, ["-e", "process.exit(19)"], { stdio: "ignore" });
  t.after(() => { if (worker.exitCode === null && worker.signalCode === null) worker.kill("SIGKILL"); });
  const [code, signal] = await once(worker, "exit");
  assert.equal(signal, null);
  assert.equal(code, 19);
});
