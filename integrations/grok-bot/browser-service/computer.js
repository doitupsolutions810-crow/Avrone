const { chromium } = require("playwright");
const http = require("node:http");
const net = require("node:net");
(async () => {
  const context = await chromium.launchPersistentContext("/profile", {
    headless: true, viewport: { width: 1280, height: 800 },
    args: ["--remote-debugging-port=9222"]
  });
  // Chromium binds CDP to loopback. Publish it only inside the Compose network.
  const server = http.createServer((req, res) => {
    const upstream = http.request({ host: "127.0.0.1", port: 9222, path: req.url,
      headers: { host: "127.0.0.1:9222" } }, r => {
      const chunks = [];
      r.on("data", c => chunks.push(c));
      r.on("end", () => {
        let body = Buffer.concat(chunks);
        if (req.url?.startsWith("/json")) {
          body = Buffer.from(body.toString().replaceAll("127.0.0.1:9222", req.headers.host));
        }
        res.writeHead(r.statusCode, { "content-type": r.headers["content-type"] || "application/json" });
        res.end(body);
      });
    });
    upstream.on("error", () => { res.writeHead(503); res.end("CDP unavailable"); });
    upstream.end();
  });
  server.on("upgrade", (req, socket, head) => {
    const upstream = net.connect(9222, "127.0.0.1", () => {
      const lines = [`GET ${req.url} HTTP/1.1`, "Host: 127.0.0.1:9222"];
      for (const [key, value] of Object.entries(req.headers)) {
        if (key !== "host" && key !== "origin") lines.push(`${key}: ${value}`);
      }
      upstream.write(lines.join("\r\n") + "\r\n\r\n");
      if (head.length) upstream.write(head);
      socket.pipe(upstream).pipe(socket);
    });
    upstream.on("error", () => socket.destroy());
    socket.on("error", () => upstream.destroy());
    socket.on("close", () => upstream.destroy());
  });
  server.listen(3000, "0.0.0.0", () => console.log("persistent CDP browser ready on :3000"));
  process.on("SIGTERM", async () => { server.close(); await context.close(); process.exit(0); });
})().catch(error => { console.error(error.name); process.exit(1); });
