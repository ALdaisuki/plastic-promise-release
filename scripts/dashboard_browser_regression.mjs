#!/usr/bin/env node
/**
 * Execute a real loopback-only Dashboard browser regression.
 *
 * This intentionally starts the deterministic `dashboard_browser_smoke.py`
 * fixture, not a Plastic Promise service. It uses a disposable headless
 * Chromium-family profile and the browser's DevTools protocol directly, so
 * no browser automation dependency needs to enter the runtime package.
 *
 * Required:
 *   PP_PYTHON=.venv/bin/python node scripts/dashboard_browser_regression.mjs
 *
 * Optional:
 *   PP_BROWSER_BIN=/path/to/edge-or-chromium
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const FIXTURE = join(ROOT, "scripts", "dashboard_browser_smoke.py");
const DASHBOARD_PORT = 19020;
const CONTROL_PORT = 19040;
const FIXTURE_TOKEN = "browser-smoke-token";
const TIMEOUT_MS = 15_000;

function fail(message) {
  throw new Error(`dashboard browser regression failed: ${message}`);
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitFor(check, description, timeoutMs = TIMEOUT_MS) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const value = await check();
      if (value) {
        return value;
      }
    } catch (error) {
      lastError = error;
    }
    await sleep(75);
  }
  const detail = lastError instanceof Error ? ` (${lastError.message})` : "";
  fail(`timed out waiting for ${description}${detail}`);
}

async function assertPortFree(port) {
  await new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", () => reject(new Error(`127.0.0.1:${port} is already in use`)));
    server.listen(port, "127.0.0.1", () => server.close(resolve));
  });
}

async function waitForPortRelease(port) {
  await waitFor(
    async () => {
      await assertPortFree(port);
      return true;
    },
    `release of 127.0.0.1:${port}`,
    3_000
  );
}

async function acquireLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close(() => reject(new Error("could not reserve a loopback port")));
        return;
      }
      server.close(() => resolve(address.port));
    });
  });
}

function browserBinary() {
  const configured = process.env.PP_BROWSER_BIN;
  if (configured) {
    if (!existsSync(configured)) {
      fail("PP_BROWSER_BIN does not exist");
    }
    return configured;
  }
  const candidates = process.platform === "darwin"
    ? [
      "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Chromium.app/Contents/MacOS/Chromium"
    ]
    : process.platform === "win32"
      ? [
        join(process.env.PROGRAMFILES || "", "Microsoft", "Edge", "Application", "msedge.exe"),
        join(process.env.PROGRAMFILES || "", "Google", "Chrome", "Application", "chrome.exe")
      ]
      : ["/usr/bin/microsoft-edge", "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"];
  const selected = candidates.find((candidate) => candidate && existsSync(candidate));
  if (!selected) {
    fail("no Chromium-family browser found; set PP_BROWSER_BIN to run this regression");
  }
  return selected;
}

function terminate(child) {
  if (child && child.exitCode === null && !child.killed) {
    child.kill("SIGTERM");
  }
}

async function stop(child) {
  if (!child || child.exitCode !== null || child.killed) {
    return;
  }
  terminate(child);
  await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    sleep(3_000).then(() => child.kill("SIGKILL"))
  ]);
}

class CdpClient {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.nextId = 0;
    this.pending = new Map();
    this.listeners = new Set();
    this.socket.addEventListener("message", (message) => this.#receive(message));
    this.socket.addEventListener("error", () => this.#closePending("websocket error"));
    this.socket.addEventListener("close", () => this.#closePending("websocket closed"));
  }

  async open() {
    await new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", () => reject(new Error("CDP connection failed")), { once: true });
    });
  }

  on(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  call(method, params = {}) {
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression) {
    const response = await this.call("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true
    });
    if (response.result.exceptionDetails) {
      fail(`page evaluation threw ${response.result.exceptionDetails.text || "an exception"}`);
    }
    return response.result.result.value;
  }

  close() {
    this.socket.close();
  }

  #receive(message) {
    let payload;
    try {
      payload = JSON.parse(String(message.data));
    } catch {
      return;
    }
    if (payload.id) {
      const pending = this.pending.get(payload.id);
      if (!pending) {
        return;
      }
      this.pending.delete(payload.id);
      if (payload.error) {
        pending.reject(new Error(`${payload.error.code}: ${payload.error.message}`));
      } else {
        pending.resolve(payload);
      }
      return;
    }
    this.listeners.forEach((listener) => listener(payload));
  }

  #closePending(reason) {
    this.pending.forEach(({ reject }) => reject(new Error(reason)));
    this.pending.clear();
  }
}

async function startFixture() {
  await assertPortFree(DASHBOARD_PORT);
  await assertPortFree(CONTROL_PORT);
  const python = process.env.PP_PYTHON || "python3";
  const child = spawn(
    python,
    [FIXTURE, "--dashboard-port", String(DASHBOARD_PORT), "--control-port", String(CONTROL_PORT)],
    { cwd: ROOT, stdio: ["ignore", "ignore", "ignore"] }
  );
  child.once("error", (error) => fail(`could not start fixture: ${error.message}`));
  await waitFor(async () => (await fetch(`http://127.0.0.1:${DASHBOARD_PORT}/dashboard`)).ok, "fixture");
  return child;
}

async function startBrowser() {
  const port = await acquireLoopbackPort();
  const profile = await mkdtemp(join(tmpdir(), "plastic-promise-dashboard-browser-"));
  const downloadDirectory = join(profile, "downloads");
  await mkdir(downloadDirectory);
  const child = spawn(browserBinary(), [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "about:blank"
  ], { cwd: ROOT, stdio: ["ignore", "ignore", "ignore"] });
  child.once("error", (error) => fail(`could not start browser: ${error.message}`));
  await waitFor(async () => (await fetch(`http://127.0.0.1:${port}/json/version`)).ok, "browser CDP");
  return { child, port, profile, downloadDirectory };
}

async function openBrowserControl(port) {
  const response = await fetch(`http://127.0.0.1:${port}/json/version`);
  if (!response.ok) {
    fail(`could not open browser CDP (${response.status})`);
  }
  const browserInfo = await response.json();
  const client = new CdpClient(browserInfo.webSocketDebuggerUrl);
  await client.open();
  return client;
}

async function openDashboard(port) {
  const address = `http://127.0.0.1:${DASHBOARD_PORT}/dashboard#/control-nodes`;
  const response = await fetch(
    `http://127.0.0.1:${port}/json/new?${encodeURIComponent(address)}`,
    { method: "PUT" }
  );
  if (!response.ok) {
    fail(`could not create browser page (${response.status})`);
  }
  const target = await response.json();
  const client = new CdpClient(target.webSocketDebuggerUrl);
  await client.open();
  await Promise.all([
    client.call("Page.enable"),
    client.call("Runtime.enable"),
    client.call("Log.enable"),
    client.call("Network.enable")
  ]);
  return client;
}

async function runRegression(client, downloadDirectory) {
  const consoleErrors = [];
  const nodeRequests = [];
  const diagnosticRequests = [];
  const downloads = [];
  client.on((event) => {
    if (event.method === "Runtime.exceptionThrown") {
      consoleErrors.push(event.params.exceptionDetails.text || "runtime exception");
    }
    if (event.method === "Log.entryAdded" && ["error", "warning"].includes(event.params.entry.level)) {
      const sourceUrl = event.params.entry.url ? ` (${event.params.entry.url})` : "";
      consoleErrors.push(`${event.params.entry.text || event.params.entry.level}${sourceUrl}`);
    }
    if (
      event.method === "Network.requestWillBeSent"
      && event.params.request.url.endsWith("/api/control/v1/nodes")
    ) {
      nodeRequests.push(event.params.request.method);
    }
    if (
      event.method === "Network.requestWillBeSent"
      && event.params.request.url.endsWith("/api/control/v1/diagnostics/bundle")
    ) {
      diagnosticRequests.push(event.params.request.method);
    }
    if (event.method === "Page.downloadWillBegin") {
      downloads.push(event.params.suggestedFilename);
    }
  });

  await waitFor(
    () => client.evaluate("Boolean(document.querySelector('input[data-control-secret=\"token\"]'))"),
    "control login form"
  );
  await client.evaluate(`(() => {
    const input = document.querySelector('input[data-control-secret="token"]');
    input.value = ${JSON.stringify(FIXTURE_TOKEN)};
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.closest("form").requestSubmit();
    return true;
  })()`);
  await waitFor(
    () => client.evaluate("document.body.innerText.includes('节点观测')"),
    "node observability projection"
  );

  const titleState = await client.evaluate(`(() => {
    const icon = document.querySelector("#topbar-view-icon");
    const use = document.querySelector("#topbar-view-icon-use");
    return {
      title: document.querySelector("#topbar-title")?.textContent,
      hidden: icon?.hidden,
      href: use?.getAttribute("href")
    };
  })()`);
  if (titleState.title !== "推理节点" || titleState.hidden || titleState.href !== "#icon-cpu") {
    fail("node page title icon did not render as the CPU icon");
  }

  const initialScroll = await client.evaluate(`(() => {
    const panel = document.querySelector("#main-panel");
    panel.scrollTop = Math.min(500, Math.max(0, panel.scrollHeight - panel.clientHeight));
    return panel.scrollTop;
  })()`);
  if (initialScroll < 100) {
    fail("node fixture is not scrollable enough for the regression");
  }
  const beforeRefresh = nodeRequests.length;
  await client.evaluate("document.querySelector('#refresh-button').click(); true");
  await waitFor(() => nodeRequests.length > beforeRefresh, "manual node refresh request");
  await waitFor(
    () => client.evaluate("document.querySelector('#view-root')?.getAttribute('aria-busy') === 'false'"),
    "manual refresh completion"
  );
  const restoredScroll = await client.evaluate("document.querySelector('#main-panel').scrollTop");
  if (restoredScroll !== initialScroll) {
    fail(`refresh did not preserve scroll position (${initialScroll} -> ${restoredScroll})`);
  }

  await client.evaluate("window.location.hash = '#/control-diagnostics'; true");
  await waitFor(
    () => client.evaluate("document.body.innerText.includes('生成并下载诊断包')"),
    "diagnostics view"
  );
  if (diagnosticRequests.length !== 0) {
    fail("diagnostics bundle was requested without an explicit click");
  }
  await client.evaluate(`(() => {
    const button = [...document.querySelectorAll("button")]
      .find((candidate) => candidate.textContent === "生成并下载诊断包");
    if (!button) throw new Error("diagnostic button missing");
    button.click();
    return true;
  })()`);
  await waitFor(() => diagnosticRequests.includes("POST"), "explicit diagnostics POST");
  await waitFor(
    () => downloads.includes("plastic-promise-diagnostic-bundle.json"),
    "diagnostics browser download"
  );
  const downloadedBundle = join(downloadDirectory, "plastic-promise-diagnostic-bundle.json");
  await waitFor(() => existsSync(downloadedBundle), "diagnostics file in disposable download directory");
  const bundle = JSON.parse(await readFile(downloadedBundle, "utf8"));
  if (bundle.schema !== "plastic-promise/diagnostic-bundle/v1") {
    fail("diagnostics browser download did not contain the strict fixture bundle");
  }
  await waitFor(
    () => client.evaluate("document.body.innerText.includes('已生成严格脱敏诊断包')"),
    "diagnostics download confirmation"
  );
  if (consoleErrors.length) {
    fail(`browser reported errors: ${consoleErrors.join(" | ")}`);
  }
}

let fixture;
let browser;
let browserControl;
let client;
try {
  fixture = await startFixture();
  browser = await startBrowser();
  browserControl = await openBrowserControl(browser.port);
  await browserControl.call("Browser.setDownloadBehavior", {
    behavior: "allow",
    downloadPath: browser.downloadDirectory,
    eventsEnabled: true
  });
  client = await openDashboard(browser.port);
  await runRegression(client, browser.downloadDirectory);
  console.log("dashboard browser regression: PASS");
} finally {
  client?.close();
  browserControl?.close();
  await stop(browser?.child);
  if (browser?.profile) {
    await rm(browser.profile, { recursive: true, force: true });
  }
  await stop(fixture);
  await Promise.all([
    waitForPortRelease(DASHBOARD_PORT),
    waitForPortRelease(CONTROL_PORT)
  ]);
}
