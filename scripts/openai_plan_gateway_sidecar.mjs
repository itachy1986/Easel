import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import readline from "node:readline";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";

const EXPECTED_EXPORT = "./plugin-sdk/gateway-runtime";
const ALLOWED_METHODS = new Set([
  "models.authStatus",
  "models.authLogin",
  "wizard.next",
  "wizard.cancel",
  "wizard.status",
]);
const SESSION_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

let client = null;
let sessionId = "";
let admitted = false;
let terminal = false;
let browserAck = null;
let lifetime = new AbortController();

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

class SafeFailure extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

function publicExportTarget(value) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && typeof value.default === "string") return value.default;
  return "";
}

function isLoopbackGatewayUrl(value) {
  try {
    const parsed = new URL(value);
    const hostname = parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase();
    const isLoopback = hostname === "::1" || (net.isIP(hostname) === 4 && hostname.startsWith("127."));
    return parsed.protocol === "ws:" && isLoopback && Boolean(parsed.port)
      && !parsed.username && !parsed.password && !parsed.search && !parsed.hash
      && (parsed.pathname === "" || parsed.pathname === "/");
  } catch {
    return false;
  }
}

async function loadGatewayRuntime(start) {
  try {
    const root = fs.realpathSync(start.packageRoot);
    const entry = fs.realpathSync(start.openclawEntry);
    if (path.dirname(entry) !== root || path.basename(entry) !== "openclaw.mjs") throw new Error();
    const metadata = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
    if (metadata.name !== "openclaw" || metadata.version !== start.expectedVersion || start.expectedVersion !== "2026.9.7") {
      throw new Error();
    }
    const target = publicExportTarget(metadata.exports?.[EXPECTED_EXPORT]);
    if (!target.startsWith("./")) throw new Error();
    const resolved = fs.realpathSync(path.resolve(root, target.slice(2)));
    const relative = path.relative(root, resolved);
    if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) throw new Error();
    const runtime = await import(pathToFileURL(resolved).href);
    if (typeof runtime.GatewayClient !== "function" || typeof runtime.startGatewayClientWhenEventLoopReady !== "function") {
      throw new Error();
    }
    return runtime;
  } catch {
    throw new SafeFailure("gateway_client_unavailable");
  }
}

async function request(method, params, options = {}) {
  if (!ALLOWED_METHODS.has(method) || !client) throw new SafeFailure("gateway_rpc_failed");
  try {
    return await client.request(method, params, { ...options, signal: lifetime.signal });
  } catch (error) {
    if (error instanceof SafeFailure) throw error;
    throw new SafeFailure("gateway_rpc_failed");
  }
}

function resolveOauthChoice(payload) {
  const providers = Array.isArray(payload?.providerCapabilities) ? payload.providerCapabilities : [];
  const openai = providers.filter((row) => row?.provider === "openai");
  if (openai.length !== 1 || !Array.isArray(openai[0].loginOptions)) return "";
  const matches = openai[0].loginOptions.filter((option) => {
    if (option?.kind !== "oauth" || typeof option.id !== "string") return false;
    const parts = option.id.split("/");
    try {
      return decodeURIComponent(parts.at(-1) || "") === "openai";
    } catch {
      return false;
    }
  });
  return matches.length === 1 ? matches[0].id : "";
}

async function stopClient() {
  const current = client;
  client = null;
  if (!current) return;
  try {
    await current.stopAndWait({ timeoutMs: 3000 });
  } catch {
    // The child is already terminal; never expose transport details.
  }
}

async function finish(event, code = "", fallbackEligible = false) {
  if (terminal) return;
  terminal = true;
  lifetime.abort();
  if (browserAck) {
    browserAck.resolve("closed");
    browserAck = null;
  }
  await stopClient();
  const payload = code ? { event, code } : { event };
  if (event === "failure" && fallbackEligible === true) payload.fallbackEligible = true;
  emit(payload);
  rl.close();
  setImmediate(() => process.exit(0));
}

async function cancelWizard() {
  if (!client || !admitted || !sessionId) return;
  try {
    await client.request("wizard.cancel", { sessionId }, { timeoutMs: 5000 });
  } catch {
    // Cancellation remains bounded and connection-local.
  }
}

async function waitForBrowserAck() {
  if (browserAck) throw new SafeFailure("gateway_rpc_failed");
  return await new Promise((resolve) => {
    browserAck = { resolve };
  });
}

async function applyWizardResult(result) {
  if (result?.done === true) {
    if (result.status === "done") {
      await finish("success");
      return true;
    }
    if (result.status === "cancelled") {
      await finish("cancelled");
      return true;
    }
    throw new SafeFailure("auth_failed");
  }
  if (result?.status === "cancelled") {
    await finish("cancelled");
    return true;
  }
  return false;
}

async function runWizard(startResult) {
  if (await applyWizardResult(startResult)) return;
  emit({ event: "waiting" });
  let answer;
  while (!terminal) {
    const result = await request(
      "wizard.next",
      { sessionId, ...(answer ? { answer } : {}) },
      { timeoutMs: null },
    );
    answer = undefined;
    if (await applyWizardResult(result)) return;
    const step = result?.step;
    if (step?.type === "note" && typeof step.id === "string" && typeof step.externalUrl === "string") {
      emit({ event: "open_url", url: step.externalUrl });
      const ack = await waitForBrowserAck();
      if (ack !== "opened" || terminal) return;
      answer = { stepId: step.id };
      emit({ event: "waiting" });
      continue;
    }
    if (step?.type === "progress" || step?.executor === "gateway") {
      emit({ event: "waiting" });
      continue;
    }
    await cancelWizard();
    throw new SafeFailure("unsupported_wizard_step");
  }
}

async function startFlow(start) {
  if (client || terminal) throw new SafeFailure("gateway_rpc_failed");
  if (start.profile !== "easel" || !["siwc", "oauth"].includes(start.method)
      || !SESSION_PATTERN.test(start.sessionId || "") || !isLoopbackGatewayUrl(start.gatewayUrl)) {
    throw new SafeFailure("gateway_client_unavailable");
  }
  sessionId = start.sessionId;
  const runtime = await loadGatewayRuntime(start);
  let helloResolve;
  let helloReject;
  const hello = new Promise((resolve, reject) => { helloResolve = resolve; helloReject = reject; });
  client = new runtime.GatewayClient({
    url: start.gatewayUrl,
    clientName: "cli",
    clientDisplayName: "easel-plan-auth",
    mode: "cli",
    role: "operator",
    scopes: ["operator.admin"],
    caps: [],
    instanceId: randomUUID(),
    requestTimeoutMs: 30000,
    hostDeps: {
      logDebug: () => {},
      logError: () => {},
      redactForLog: () => "[redacted]",
    },
    onHelloOk: () => helloResolve(),
    onConnectError: () => helloReject(new SafeFailure("gateway_auth_unavailable")),
    onReconnectPaused: () => helloReject(new SafeFailure("gateway_auth_unavailable")),
    onClose: () => {
      if (!terminal) helloReject(new SafeFailure("gateway_auth_unavailable"));
    },
  });
  const readiness = await runtime.startGatewayClientWhenEventLoopReady(client, {
    timeoutMs: 10000,
    signal: lifetime.signal,
  });
  if (!readiness?.ready) throw new SafeFailure("gateway_auth_unavailable");
  await Promise.race([
    hello,
    new Promise((_, reject) => setTimeout(() => reject(new SafeFailure("gateway_auth_unavailable")), 15000)),
  ]);
  emit({ event: "ready" });
  let authChoice = "openai-token-sharing";
  if (start.method === "oauth") {
    const status = await request("models.authStatus", { agentId: "main", refresh: false }, { timeoutMs: 30000 });
    authChoice = resolveOauthChoice(status);
    if (!authChoice) throw new SafeFailure("oauth_choice_unavailable");
  }
  // From this point the Gateway may create the session before replying, so
  // every failure/cancel path must attempt exact-session cleanup.
  admitted = true;
  emit({ event: "admitted" });
  const result = await request(
    "models.authLogin",
    { sessionId, agentId: "main", authChoice },
    { timeoutMs: 40000 },
  );
  await runWizard(result);
}

async function handleCommand(command) {
  if (!command || typeof command !== "object" || typeof command.command !== "string") {
    await finish("failure", "gateway_rpc_failed");
    return;
  }
  if (command.command === "start") {
    try {
      await startFlow(command);
    } catch (error) {
      await cancelWizard();
      await finish(
        "failure",
        error instanceof SafeFailure ? error.code : "gateway_rpc_failed",
        !admitted,
      );
    }
    return;
  }
  if (command.command === "browser_opened" && browserAck) {
    browserAck.resolve("opened");
    browserAck = null;
    return;
  }
  if (command.command === "browser_open_failed" && browserAck) {
    browserAck.resolve("failed");
    browserAck = null;
    await cancelWizard();
    await finish("failure", "auth_browser_open_failed");
    return;
  }
  if (command.command === "cancel") {
    await cancelWizard();
    await finish("cancelled");
    return;
  }
  if (command.command === "shutdown") {
    await cancelWizard();
    await finish("cancelled");
    return;
  }
  await cancelWizard();
  await finish("failure", "gateway_rpc_failed");
}

rl.on("line", (line) => {
  if (line.length > 65536) {
    void finish("failure", "gateway_rpc_failed");
    return;
  }
  let command;
  try {
    command = JSON.parse(line);
  } catch {
    void finish("failure", "gateway_rpc_failed");
    return;
  }
  void handleCommand(command);
});

rl.on("close", () => {
  if (!terminal) void handleCommand({ command: "shutdown" });
});
