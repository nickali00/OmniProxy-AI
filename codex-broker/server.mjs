import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import http from "node:http";
import readline from "node:readline";
import {
  parseChatRequest,
  readJsonBody,
  renderChatPrompt,
  runProcess,
} from "./runtime.mjs";

const host = process.env.BROKER_HOST || "0.0.0.0";
const port = Number.parseInt(process.env.BROKER_PORT || "8788", 10);
const codexBinary = process.env.CODEX_BINARY || "/usr/local/bin/codex";
const authTtlMs = Number.parseInt(
  process.env.CODEX_AUTH_TTL_MS || "600000",
  10,
);
const executionTimeoutMs = Number.parseInt(
  process.env.PROVIDER_EXECUTION_TIMEOUT_MS || "300000",
  10,
);

let requestId = 0;
let initialized = false;
let activeAttempt = null;
let activeExecutions = 0;
const pending = new Map();

const appServer = spawn(codexBinary, ["app-server", "--stdio"], {
  env: { ...process.env, RUST_LOG: "error" },
  stdio: ["pipe", "pipe", "pipe"],
});

const lines = readline.createInterface({ input: appServer.stdout });
lines.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return;
  }

  if (message.id !== undefined && pending.has(message.id)) {
    const entry = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(entry.timer);
    if (message.error) {
      entry.reject(new Error("app_server_request_failed"));
    } else {
      entry.resolve(message.result);
    }
    return;
  }

  if (
    message.method === "account/login/completed" &&
    activeAttempt &&
    (!message.params?.loginId ||
      message.params.loginId === activeAttempt.loginId)
  ) {
    activeAttempt.state =
      message.params?.success === true ? "connected" : "failed";
    if (activeAttempt.expiryTimer) clearTimeout(activeAttempt.expiryTimer);
  }
});

// stderr viene consumato ma non registrato: può contenere dettagli di auth.
appServer.stderr.on("data", () => {});
appServer.on("exit", () => process.exit(1));
appServer.on("error", () => process.exit(1));

function sendNotification(method, params = {}) {
  appServer.stdin.write(`${JSON.stringify({ method, params })}\n`);
}

function sendRequest(method, params = {}, timeoutMs = 15_000) {
  const id = ++requestId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("app_server_timeout"));
    }, timeoutMs);
    timer.unref();
    pending.set(id, { resolve, reject, timer });
    appServer.stdin.write(`${JSON.stringify({ method, id, params })}\n`);
  });
}

async function initialize() {
  await sendRequest("initialize", {
    clientInfo: {
      name: "omni_proxy_ai",
      title: "OmniProxy AI",
      version: "0.2.0",
    },
    capabilities: { experimentalApi: true },
  });
  sendNotification("initialized");
  initialized = true;
}

const initialization = initialize().catch(() => {
  process.exit(1);
});

function jsonResponse(response, statusCode, body) {
  const payload = JSON.stringify(body);
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(payload),
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(payload);
}

function publicAttempt(attempt) {
  if (!attempt) return null;
  return {
    id: attempt.loginId,
    state: attempt.state,
    expires_at: new Date(attempt.expiresAt).toISOString(),
    requires_code: false,
    user_code: attempt.userCode,
  };
}

async function accountStatus() {
  await initialization;
  const result = await sendRequest("account/read", { refreshToken: false });
  const account = result?.account;
  return {
    installed: true,
    connected: account !== null && account !== undefined,
    auth_method:
      account?.type === "chatgpt"
        ? "chatgpt"
        : account?.type === "apiKey"
          ? "api_key"
          : "none",
    plan_type:
      account?.type === "chatgpt" && typeof account.planType === "string"
        ? account.planType
        : null,
  };
}

function normalizedPercent(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return Math.min(100, Math.max(0, value));
}

function publicRateLimitWindows(payload) {
  const snapshots =
    payload?.rateLimitsByLimitId &&
    typeof payload.rateLimitsByLimitId === "object"
      ? Object.values(payload.rateLimitsByLimitId)
      : payload?.rateLimits
        ? [payload.rateLimits]
        : [];
  const windows = [];
  for (const snapshot of snapshots) {
    if (!snapshot || typeof snapshot !== "object") continue;
    for (const windowName of ["primary", "secondary"]) {
      const window = snapshot[windowName];
      const usedPercent = normalizedPercent(window?.usedPercent);
      if (usedPercent === null) continue;
      windows.push({
        id: `${snapshot.limitId || "codex"}:${windowName}`,
        limit_name:
          typeof snapshot.limitName === "string"
            ? snapshot.limitName
            : null,
        window: windowName,
        used_percent: usedPercent,
        remaining_percent: Math.max(0, 100 - usedPercent),
        window_minutes:
          Number.isFinite(window?.windowDurationMins)
            ? Math.max(0, window.windowDurationMins)
            : null,
        resets_at:
          Number.isFinite(window?.resetsAt) ? window.resetsAt : null,
      });
    }
  }
  return windows;
}

async function quotaStatus() {
  const status = await accountStatus();
  if (!status.connected) {
    return {
      provider: "codex",
      connected: false,
      available: false,
      unlimited: false,
      source: "codex_app_server",
      reason: "not_connected",
      remaining_percent: null,
      windows: [],
    };
  }
  try {
    const payload = await sendRequest("account/rateLimits/read", {});
    const windows = publicRateLimitWindows(payload);
    const remaining = windows
      .map((window) => window.remaining_percent)
      .filter((value) => Number.isFinite(value));
    return {
      provider: "codex",
      connected: true,
      available: remaining.length > 0,
      unlimited: false,
      source: "codex_app_server",
      reason: remaining.length > 0 ? null : "not_exposed",
      plan_type: status.plan_type,
      remaining_percent:
        remaining.length > 0 ? Math.min(...remaining) : null,
      windows,
      reset_credits:
        Number.isInteger(payload?.rateLimitResetCredits?.availableCount)
          ? Math.max(0, payload.rateLimitResetCredits.availableCount)
          : null,
      checked_at: new Date().toISOString(),
    };
  } catch {
    return {
      provider: "codex",
      connected: true,
      available: false,
      unlimited: false,
      source: "codex_app_server",
      reason: "temporarily_unavailable",
      plan_type: status.plan_type,
      remaining_percent: null,
      windows: [],
      checked_at: new Date().toISOString(),
    };
  }
}

async function cancelAttempt() {
  if (!activeAttempt) return;
  const attempt = activeAttempt;
  activeAttempt = null;
  if (attempt.expiryTimer) clearTimeout(attempt.expiryTimer);
  try {
    await sendRequest(
      "account/login/cancel",
      { loginId: attempt.loginId },
      10_000,
    );
  } catch {
    // La cancellazione resta idempotente anche se app-server è già concluso.
  }
}

async function startAuthentication() {
  const current = await accountStatus();
  if (current.connected) {
    return { provider: "codex", ...current, attempt: null };
  }
  if (
    activeAttempt &&
    ["failed", "expired", "cancelled"].includes(activeAttempt.state)
  ) {
    await cancelAttempt();
  }
  if (activeAttempt) {
    return {
      provider: "codex",
      ...current,
      auth_url: activeAttempt.verificationUrl,
      attempt: publicAttempt(activeAttempt),
    };
  }

  const result = await sendRequest(
    "account/login/start",
    { type: "chatgptDeviceCode" },
    20_000,
  );
  if (
    result?.type !== "chatgptDeviceCode" ||
    typeof result.loginId !== "string" ||
    typeof result.userCode !== "string" ||
    typeof result.verificationUrl !== "string"
  ) {
    throw new Error("invalid_login_response");
  }

  const verificationUrl = new URL(result.verificationUrl);
  const allowedHost = new Set([
    "auth.openai.com",
    "chatgpt.com",
    "device.openai.com",
  ]);
  if (
    verificationUrl.protocol !== "https:" ||
    !allowedHost.has(verificationUrl.hostname) ||
    verificationUrl.username ||
    verificationUrl.password ||
    verificationUrl.hash
  ) {
    await sendRequest("account/login/cancel", { loginId: result.loginId });
    throw new Error("invalid_login_url");
  }

  const attempt = {
    loginId: result.loginId,
    state: "waiting_for_user",
    userCode: result.userCode,
    verificationUrl: verificationUrl.toString(),
    expiresAt: Date.now() + authTtlMs,
    expiryTimer: null,
  };
  attempt.expiryTimer = setTimeout(async () => {
    if (activeAttempt?.loginId !== attempt.loginId) return;
    attempt.state = "expired";
    await cancelAttempt();
  }, authTtlMs);
  attempt.expiryTimer.unref();
  activeAttempt = attempt;

  return {
    provider: "codex",
    ...current,
    auth_url: attempt.verificationUrl,
    attempt: publicAttempt(attempt),
  };
}

async function modelCatalog() {
  await initialization;
  const result = await sendRequest("model/list", {
    limit: 100,
    includeHidden: false,
  });
  const models = Array.isArray(result?.data) ? result.data : [];
  return models
    .filter(
      (model) =>
        model &&
        model.hidden !== true &&
        typeof model.model === "string" &&
        typeof model.displayName === "string",
    )
    .map((model) => {
      const efforts = Array.isArray(model.supportedReasoningEfforts)
        ? model.supportedReasoningEfforts
            .map((option) => option?.reasoningEffort)
            .filter((effort) => typeof effort === "string")
        : [];
      return {
        id: model.model,
        display_name: model.displayName,
        description:
          typeof model.description === "string" ? model.description : "",
        is_default: model.isDefault === true,
        reasoning_efforts: efforts,
        default_reasoning_effort:
          typeof model.defaultReasoningEffort === "string"
            ? model.defaultReasoningEffort
            : efforts[0] || "medium",
      };
    });
}

async function completeChat(body) {
  const status = await accountStatus();
  if (!status.connected) {
    return {
      statusCode: 409,
      body: {
        error: "provider_not_connected",
        message: "Collega l'account ChatGPT prima di usare Codex.",
      },
    };
  }
  if (activeExecutions >= 2) {
    return {
      statusCode: 429,
      body: {
        error: "provider_busy",
        message: "Codex sta già elaborando troppe richieste.",
      },
    };
  }

  const models = await modelCatalog();
  const modelMap = new Map(models.map((model) => [model.id, model]));
  const allEfforts = new Set(
    models.flatMap((model) => model.reasoning_efforts),
  );
  let chat;
  try {
    chat = parseChatRequest(body, new Set(modelMap.keys()), allEfforts);
  } catch {
    return {
      statusCode: 400,
      body: {
        error: "invalid_chat_request",
        message: "Modello, reasoning o messaggi Codex non validi.",
      },
    };
  }
  const selectedModel = modelMap.get(chat.model);
  if (!selectedModel.reasoning_efforts.includes(chat.reasoning)) {
    return {
      statusCode: 400,
      body: {
        error: "unsupported_reasoning_effort",
        message: "Il livello di reasoning non è supportato dal modello.",
      },
    };
  }

  const outputPath = `/tmp/omni-codex-${crypto.randomUUID()}.txt`;
  const reasoningToml = JSON.stringify(chat.reasoning);
  const args = [
    "exec",
    "--skip-git-repo-check",
    "--ephemeral",
    "--ignore-user-config",
    "--ignore-rules",
    "--sandbox",
    "read-only",
    "--model",
    chat.model,
    "--config",
    `model_reasoning_effort=${reasoningToml}`,
    "--cd",
    "/workspace",
    "--output-last-message",
    outputPath,
    "-",
  ];

  activeExecutions += 1;
  try {
    const result = await runProcess(codexBinary, args, {
      input: renderChatPrompt(chat.messages),
      cwd: "/workspace",
      env: { ...process.env, RUST_LOG: "error", NO_COLOR: "1" },
      timeoutMs: executionTimeoutMs,
    });
    if (result.code !== 0) {
      return {
        statusCode: 502,
        body: {
          error: "codex_execution_failed",
          message: "Codex non ha completato la richiesta.",
        },
      };
    }
    const content = (await fs.readFile(outputPath, "utf8")).trim();
    if (!content) throw new Error("empty_completion");
    return {
      statusCode: 200,
      body: { provider: "codex", model: chat.model, content },
    };
  } catch {
    return {
      statusCode: 502,
      body: {
        error: "codex_execution_failed",
        message: "Codex non ha completato la richiesta.",
      },
    };
  } finally {
    activeExecutions -= 1;
    await fs.unlink(outputPath).catch(() => {});
  }
}

async function route(request, response) {
  const url = new URL(request.url || "/", `http://${request.headers.host}`);

  if (request.method === "GET" && url.pathname === "/healthz") {
    await initialization;
    jsonResponse(response, initialized ? 200 : 503, {
      status: initialized ? "ok" : "starting",
    });
    return;
  }

  if (request.method === "GET" && url.pathname === "/v1/status") {
    const status = await accountStatus();
    if (status.connected && activeAttempt) {
      if (activeAttempt.expiryTimer) clearTimeout(activeAttempt.expiryTimer);
      activeAttempt = null;
    }
    jsonResponse(response, 200, {
      provider: "codex",
      ...status,
      attempt: publicAttempt(activeAttempt),
    });
    return;
  }

  if (request.method === "GET" && url.pathname === "/v1/models") {
    const status = await accountStatus();
    jsonResponse(response, 200, {
      provider: "codex",
      connected: status.connected,
      models: await modelCatalog(),
    });
    return;
  }

  if (request.method === "GET" && url.pathname === "/v1/quota") {
    jsonResponse(response, 200, await quotaStatus());
    return;
  }

  if (request.method === "POST" && url.pathname === "/v1/chat") {
    let body;
    try {
      body = await readJsonBody(request);
    } catch {
      jsonResponse(response, 400, {
        error: "invalid_chat_request",
        message: "Richiesta Codex non valida.",
      });
      return;
    }
    const result = await completeChat(body);
    jsonResponse(response, result.statusCode, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/v1/auth/start") {
    const payload = await startAuthentication();
    jsonResponse(response, payload.auth_url ? 201 : 200, payload);
    return;
  }

  const cancelMatch = url.pathname.match(
    /^\/v1\/auth\/([A-Za-z0-9_-]{8,256})$/u,
  );
  if (request.method === "DELETE" && cancelMatch) {
    if (activeAttempt?.loginId === cancelMatch[1]) await cancelAttempt();
    jsonResponse(response, 200, { status: "cancelled" });
    return;
  }

  jsonResponse(response, 404, {
    error: "not_found",
    message: "Endpoint non disponibile.",
  });
}

const server = http.createServer((request, response) => {
  route(request, response).catch(() => {
    jsonResponse(response, 502, {
      error: "codex_app_server_error",
      message: "Codex app-server non ha completato l'operazione.",
    });
  });
});

server.listen(port, host);

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, async () => {
    await cancelAttempt();
    appServer.kill("SIGTERM");
    server.close(() => process.exit(0));
  });
}
