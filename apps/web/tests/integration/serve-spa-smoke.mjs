// Exercise the same serve -s entry point used by Web start/preview, locally.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";

test("Web start serves SPA deep links and preserves static assets", { timeout: 20000 }, async () => {
  const require = createRequire(import.meta.url);
  const cli = join(dirname(require.resolve("serve/package.json")), "build/main.js");
  const directory = await mkdtemp(join(tmpdir(), "plane-serve-smoke-"));
  const probe = createServer();
  probe.listen(0, "127.0.0.1");
  await once(probe, "listening");
  const port = probe.address().port;
  await new Promise((resolve) => probe.close(resolve));
  const document = "<!doctype html><title>Plane SPA fixture</title>";
  await writeFile(join(directory, "index.html"), document);
  await writeFile(join(directory, "asset.js"), "/* static fixture */");
  const child = spawn(process.execPath, [cli, "-s", directory, "-l", `tcp://127.0.0.1:${port}`, "--no-clipboard"], {
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, NO_UPDATE_CHECK: "1" },
  });
  let output = "";
  child.stdout.on("data", (value) => (output += value));
  child.stderr.on("data", (value) => (output += value));
  try {
    let ready = false;
    /* eslint-disable no-await-in-loop -- Readiness polling must observe each attempt before retrying. */
    for (let i = 0; i < 100; i++) {
      assert.equal(child.exitCode, null, output);
      try {
        const response = await fetch(`http://127.0.0.1:${port}/`);
        ready = response.status === 200;
        await response.arrayBuffer();
      } catch {}
      if (ready) break;
      await delay(50);
    }
    /* eslint-enable no-await-in-loop */
    assert.ok(ready, output);
    await Promise.all(
      ["/fixture/projects/project/teams", "/fixture/projects/project/builds"].map(async (route) => {
        const response = await fetch(`http://127.0.0.1:${port}${route}`);
        assert.equal(response.status, 200, route);
        assert.equal(await response.text(), document);
      })
    );
    const asset = await fetch(`http://127.0.0.1:${port}/asset.js`);
    assert.equal(asset.status, 200);
    assert.equal(await asset.text(), "/* static fixture */");
    assert.equal(child.exitCode, null, output);
  } finally {
    if (child.exitCode === null) {
      const exited = once(child, "exit");
      child.kill("SIGTERM");
      await exited;
    }
    await rm(directory, { recursive: true, force: true });
  }
});
