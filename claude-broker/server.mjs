import { execFile, spawn } from "node:child_process";
import crypto from "node:crypto";
import http from "node:http";
import { promisify } from "node:util";
import {
  parseChatRequest,
  readJsonBody,
  renderChatPrompt,
  runProcess,
} from "./runtime.mjs";

const execFileAsync = promisify(execFile);
const host = process.env.BROKER_HOST || "0.0.0.0";
const port = Number.parseInt(process.env.BROKER_PORT || "8787", 10);
const claudeBinary = process.env.CLAUDE_BINARY || "/usr/local/bin/claude";
const authTtlMs = Number.parseInt(
  process.env.CLAUDE_AUTH_TTL_MS || "300000",
  10,
);
const maxBodyBytes = 12 * 1024;
const executionTimeoutMs = Number.parseInt(
  process.env.PROVIDER_EXECUTION_TIMEOUT_MS || "300000",
  10,
);
const authUrlPattern =
  /https:\/\/claude\.com\/cai\/oauth\/authorize\?[^\s\u001b\u0007]+/u;

let activeAttempt = null;
let activeExecutions = 0;

const claudeEfforts = ["low", "medium", "high", "xhigh", "max"];
const claudeModels = [
  {
    id: "sonnet",
    display_name: "Claude Sonnet",
    description: "Alias ufficiale aggiornato automaticamente da Claude Code.",
    is_default: true,
    reasoning_efforts: claudeEfforts,
    default_reasoning_effort: "high",
  },
  {
    id: "opus",
    display_name: "Claude Opus",
    description: "Alias ufficiale Claude Code per il profilo Opus.",
    is_default: false,
    reasoning_efforts: claudeEfforts,
    default_reasoning_effort: "high",
  },
  {
    id: "haiku",
    display_name: "Claude Haiku",
    description: "Alias ufficiale Claude Code per il profilo più rapido.",
    is_default: false,
    reasoning_efforts: claudeEfforts,
    default_reasoning_effort: "medium",
  },
  {
    id: "fable",
    display_name: "Claude Fable",
    description: "Alias ufficiale disponibile nel client Claude Code installato.",
    is_default: false,
    reasoning_efforts: claudeEfforts,
    default_reasoning_effort: "high",
  },
];

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
    id: attempt.id,
    state: attempt.state,
    expires_at: new Date(attempt.expiresAt).toISOString(),
    requires_code: attempt.state === "waiting_for_user",
  };
}

function terminateAttempt(nextState = "cancelled") {
  if (!activeAttempt) return;
  const attempt = activeAttempt;
  attempt.state = nextState;
  if (attempt.expiryTimer) clearTimeout(attempt.expiryTimer);
  if (attempt.process && attempt.process.exitCode === null) {
    const child = attempt.process;
    child.kill("SIGTERM");
    const forceKill = setTimeout(() => {
      if (child.exitCode === null) child.kill("SIGKILL");
    }, 2_000);
    forceKill.unref();
  }
  activeAttempt = null;
}

function cleanupExpiredAttempt() {
  if (activeAttempt && Date.now() >= activeAttempt.expiresAt) {
    terminateAttempt("expired");
  }
}

async function authenticationStatus() {
  try {
    const { stdout } = await execFileAsync(
      claudeBinary,
      ["auth", "status", "--json"],
      {
        timeout: 10_000,
        maxBuffer: 64 * 1024,
        env: process.env,
      },
    );
    const status = JSON.parse(stdout);
    return {
      installed: true,
      connected: status.loggedIn === true,
      auth_method:
        typeof status.authMethod === "string" ? status.authMethod : "none",
    };
  } catch (error) {
    // `claude auth status` usa exit code 1 quando il client non è autenticato.
    if (error && typeof error.stdout === "string") {
      try {
        const status = JSON.parse(error.stdout);
        return {
          installed: true,
          connected: status.loggedIn === true,
          auth_method:
            typeof status.authMethod === "string" ? status.authMethod : "none",
        };
      } catch {
        // La risposta viene intenzionalmente ridotta a un errore generico.
      }
    }
    return { installed: true, connected: false, auth_method: "none" };
  }
}

async function quotaStatus() {
  const status = await authenticationStatus();
  return {
    provider: "claude",
    connected: status.connected,
    available: false,
    unlimited: false,
    source: "claude_code",
    reason: status.connected ? "interactive_only" : "not_connected",
    remaining_percent: null,
    windows: [],
    checked_at: new Date().toISOString(),
  };
}

async function readAuthJsonBody(request) {
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBodyBytes) {
      throw new Error("body_too_large");
    }
    chunks.push(chunk);
  }
  if (chunks.length === 0) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new Error("invalid_json");
  }
}

async function completeChat(body) {
  const status = await authenticationStatus();
  if (!status.connected) {
    return {
      statusCode: 409,
      body: {
        error: "provider_not_connected",
        message: "Collega l'account Claude.ai prima di usare Claude.",
      },
    };
  }
  if (activeExecutions >= 2) {
    return {
      statusCode: 429,
      body: {
        error: "provider_busy",
        message: "Claude sta già elaborando troppe richieste.",
      },
    };
  }

  let chat;
  try {
    chat = parseChatRequest(
      body,
      new Set(claudeModels.map((model) => model.id)),
      new Set(claudeEfforts),
    );
  } catch {
    return {
      statusCode: 400,
      body: {
        error: "invalid_chat_request",
        message: "Modello, reasoning o messaggi Claude non validi.",
      },
    };
  }

  const args = [
    "--print",
    "--output-format",
    "json",
    "--input-format",
    "text",
    "--model",
    chat.model,
    "--effort",
    chat.reasoning,
    "--permission-mode",
    "plan",
    "--tools",
    "",
    "--safe-mode",
    "--no-session-persistence",
  ];

  activeExecutions += 1;
  try {
    const result = await runProcess(claudeBinary, args, {
      input: renderChatPrompt(chat.messages),
      cwd: "/app",
      env: { ...process.env, NO_COLOR: "1" },
      timeoutMs: executionTimeoutMs,
    });
    if (result.code !== 0) throw new Error("claude_failed");
    const payload = JSON.parse(result.stdout);
    const content =
      typeof payload.result === "string" ? payload.result.trim() : "";
    if (!content || payload.is_error === true) {
      throw new Error("empty_completion");
    }
    return {
      statusCode: 200,
      body: { provider: "claude", model: chat.model, content },
    };
  } catch {
    return {
      statusCode: 502,
      body: {
        error: "claude_execution_failed",
        message: "Claude Code non ha completato la richiesta.",
      },
    };
  } finally {
    activeExecutions -= 1;
  }
}

function startAuthentication() {
  cleanupExpiredAttempt();
  if (
    activeAttempt &&
    ["connected", "completed", "failed", "cancelled", "expired"].includes(
      activeAttempt.state,
    )
  ) {
    activeAttempt = null;
  }
  if (activeAttempt) return activeAttempt;

  const attempt = {
    id: crypto.randomUUID(),
    state: "starting",
    authUrl: null,
    outputBuffer: "",
    process: null,
    expiryTimer: null,
    expiresAt: Date.now() + authTtlMs,
  };

  const child = spawn(
    claudeBinary,
    ["auth", "login", "--claudeai"],
    {
      env: {
        ...process.env,
        BROWSER: "/bin/true",
        NO_COLOR: "1",
      },
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  attempt.process = child;
  attempt.expiryTimer = setTimeout(() => {
    if (activeAttempt?.id === attempt.id) terminateAttempt("expired");
  }, authTtlMs);
  attempt.expiryTimer.unref();
  activeAttempt = attempt;

  const consumeOutput = (chunk) => {
    // L'output non viene mai registrato: può contenere l'URL OAuth monouso.
    attempt.outputBuffer = `${attempt.outputBuffer}${chunk.toString("utf8")}`.slice(
      -64 * 1024,
    );
    const match = attempt.outputBuffer.match(authUrlPattern);
    if (!match || attempt.authUrl) return;
    try {
      const candidate = new URL(match[0]);
      if (
        candidate.protocol === "https:" &&
        candidate.hostname === "claude.com" &&
        candidate.pathname === "/cai/oauth/authorize" &&
        candidate.hash === "" &&
        candidate.username === "" &&
        candidate.password === ""
      ) {
        attempt.authUrl = candidate.toString();
        attempt.state = "waiting_for_user";
      }
    } catch {
      attempt.state = "failed";
    }
  };

  child.stdout.on("data", consumeOutput);
  child.stderr.on("data", consumeOutput);
  child.on("error", () => {
    attempt.state = "failed";
  });
  child.on("exit", async (code, signal) => {
    if (signal && ["cancelled", "expired"].includes(attempt.state)) return;
    const status = await authenticationStatus();
    attempt.state = status.connected
      ? "connected"
      : code === 0
        ? "completed"
        : "failed";
  });

  return attempt;
}

async function waitForAuthUrl(attempt) {
  const deadline = Date.now() + 12_000;
  while (
    Date.now() < deadline &&
    attempt.state === "starting" &&
    !attempt.authUrl
  ) {
    await new Promise((resolve) => setTimeout(resolve, 75));
  }
  return attempt;
}

async function route(request, response) {
  cleanupExpiredAttempt();
  const url = new URL(request.url || "/", `http://${request.headers.host}`);

  if (request.method === "GET" && url.pathname === "/healthz") {
    jsonResponse(response, 200, { status: "ok" });
    return;
  }

  if (request.method === "GET" && url.pathname === "/v1/status") {
    const status = await authenticationStatus();
    jsonResponse(response, 200, {
      provider: "claude",
      ...status,
      attempt: publicAttempt(activeAttempt),
    });
    return;
  }

  if (request.method === "GET" && url.pathname === "/v1/models") {
    const status = await authenticationStatus();
    jsonResponse(response, 200, {
      provider: "claude",
      connected: status.connected,
      models: claudeModels,
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
        message: "Richiesta Claude non valida.",
      });
      return;
    }
    const result = await completeChat(body);
    jsonResponse(response, result.statusCode, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/v1/auth/start") {
    const status = await authenticationStatus();
    if (status.connected) {
      jsonResponse(response, 200, {
        provider: "claude",
        ...status,
        attempt: null,
      });
      return;
    }

    const attempt = await waitForAuthUrl(startAuthentication());
    if (!attempt.authUrl) {
      jsonResponse(response, 502, {
        error: "auth_flow_unavailable",
        message: "Claude Code non ha prodotto un URL OAuth valido.",
      });
      return;
    }
    jsonResponse(response, 201, {
      provider: "claude",
      installed: true,
      connected: false,
      auth_url: attempt.authUrl,
      attempt: publicAttempt(attempt),
    });
    return;
  }

  const codeMatch = url.pathname.match(
    /^\/v1\/auth\/([0-9a-f-]{36})\/code$/u,
  );
  if (request.method === "POST" && codeMatch) {
    if (
      !activeAttempt ||
      activeAttempt.id !== codeMatch[1] ||
      activeAttempt.state !== "waiting_for_user"
    ) {
      jsonResponse(response, 404, {
        error: "auth_attempt_not_found",
        message: "Tentativo di accesso non valido o scaduto.",
      });
      return;
    }

    let body;
    try {
      body = await readAuthJsonBody(request);
    } catch {
      jsonResponse(response, 400, {
        error: "invalid_request",
        message: "Richiesta non valida.",
      });
      return;
    }

    const code = typeof body.code === "string" ? body.code.trim() : "";
    if (
      code.length < 8 ||
      code.length > 8192 ||
      /[\r\n\u0000]/u.test(code)
    ) {
      jsonResponse(response, 400, {
        error: "invalid_auth_code",
        message: "Il codice monouso non è valido.",
      });
      return;
    }

    activeAttempt.state = "verifying";
    activeAttempt.process.stdin.write(`${code}\n`);
    // La variabile viene lasciata uscire immediatamente dallo scope.
    jsonResponse(response, 202, {
      provider: "claude",
      connected: false,
      attempt: publicAttempt(activeAttempt),
    });
    return;
  }

  const cancelMatch = url.pathname.match(
    /^\/v1\/auth\/([0-9a-f-]{36})$/u,
  );
  if (request.method === "DELETE" && cancelMatch) {
    if (activeAttempt && activeAttempt.id === cancelMatch[1]) {
      terminateAttempt("cancelled");
    }
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
    jsonResponse(response, 500, {
      error: "internal_error",
      message: "Errore interno del broker.",
    });
  });
});

server.listen(port, host);

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    terminateAttempt("cancelled");
    server.close(() => process.exit(0));
  });
}
