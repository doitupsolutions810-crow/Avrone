import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";

function waitForMatch(proc, regex, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => {
      proc.kill("SIGKILL");
      reject(new Error("Timed out waiting for child process readiness"));
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
      reject(new Error("Parent exited before readiness: " + code + "/" + signal + " " + output));
    });
  });
}

test("SIGTERM is propagated to a child and parent reaps it before exiting", async t => {
  const childCode = "process.on('SIGTERM', () => process.exit(0)); setInterval(() => {}, 1000)";
  const parentSource = [
    'const { spawn } = require("node:child_process");',
    'const child = spawn(process.execPath, ["-e", ' + JSON.stringify(childCode) + '], { stdio: "ignore" });',
    'child.once("spawn", () => process.stdout.write("CHILD_PID=" + child.pid + "\\n"));',
    'process.once("SIGTERM", () => {',
    '  child.kill("SIGTERM");',
    '  const deadline = setTimeout(() => process.exit(2), 4000);',
    '  child.once("exit", (code, signal) => {',
    '    clearTimeout(deadline);',
    '    process.stdout.write("CHILD_EXITED=" + code + ":" + signal + "\\n");',
    '    process.exit(code === 0 ? 0 : 3);',
    '  });',
    '});',
    'setInterval(() => {}, 1000);'
  ].join("\n");
  const parent = spawn(process.execPath, ["-e", parentSource], { stdio: ["ignore", "pipe", "pipe"] });
  t.after(() => { if (parent.exitCode === null && parent.signalCode === null) parent.kill("SIGKILL"); });
  const ready = await waitForMatch(parent, /CHILD_PID=(\d+)/);
  const childPid = Number(ready.match[1]);
  let output = ready.output;
  parent.stdout.on("data", chunk => { output += chunk.toString(); });
  const outputPromise = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Child exit was not observed")), 5000);
    const check = () => {
      if (/CHILD_EXITED=0:/.test(output)) {
        clearTimeout(timer);
        resolve();
      }
    };
    parent.stdout.on("data", check);
    check();
  });
  const exitPromise = once(parent, "exit");
  parent.kill("SIGTERM");
  const [exitCode, exitSignal] = await exitPromise;
  await outputPromise;
  assert.equal(exitCode, 0);
  assert.equal(exitSignal, null);
  assert.match(output, /CHILD_EXITED=0:/);
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
