import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import {
  parseChatRequest,
  readJsonBody,
  renderChatPrompt,
  runProcess,
} from "./runtime.mjs";

const host = process.env.BROKER_HOST || "0.0.0.0";
const port = Number.parseInt(process.env.BROKER_PORT || "8789", 10);
const antigravityBinary =
  process.env.ANTIGRAVITY_BINARY || "/usr/local/bin/antigravity";
const authTtlMs = Number.parseInt(
  process.env.ANTIGRAVITY_AUTH_TTL_MS || "600000",
  10,
);
const executionTimeoutMs = Number.parseInt(
  process.env.PROVIDER_EXECUTION_TIMEOUT_MS || "300000",
  10,
);
const maxAuthBodyBytes = 12 * 1024;
const maxPromptCharacters = 180_000;
const settingsPath = path.join(
  process.env.HOME || "/home/antigravity",
  ".gemini",
  "antigravity-cli",
  "settings.json",
);

let activeAttempt = null;
let activeExecutions = 0;

const reasoningEfforts = ["low", "medium", "high"];
const proprietaryModels = [
  {
    id: "gemini-3.5-flash",
    display_name: "Gemini 3.5 Flash",
    description: "Modello Gemini rapido esposto da Antigravity.",
    is_default: true,
    reasoning_efforts: reasoningEfforts,
    default_reasoning_effort: "medium",
  },
  {
    id: "gemini-3.1-pro",
    display_name: "Gemini 3.1 Pro",
    description: "Modello Gemini avanzato esposto da Antigravity.",
    is_default: false,
    reasoning_efforts: reasoningEfforts,
    default_reasoning_effort: "high",
  },
];

const ansiPattern =
  // CSI, OSC e sequenze terminali semplici prodotte dalla TUI.
  /(?:\u001b\][^\u0007]*(?:\u0007|\u001b\\)|\u001b\[[0-?]*[ -/]*[@-~]|\u001b[()][0-2A-Z0-9]|[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f])/gu;

function stripTerminalOutput(value) {
  return String(value || "")
    .replace(ansiPattern, "")
    .replace(/\r/gu, "");
}

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

async function ensureSafeSettings() {
  const defaults = {
    colorScheme: "terminal",
    altScreenMode: "never",
    toolPermission: "strict",
    artifactReviewPolicy: "asks-for-review",
    enableTerminalSandbox: true,
    enableTelemetry: false,
    permissions: {
      deny: [
        "command(*)",
        "write_file(*)",
        "execute_url(*)",
        "mcp(*)",
      ],
      ask: [],
      allow: [],
    },
  };
  await fs.mkdir(path.dirname(settingsPath), { recursive: true, mode: 0o700 });
  // Il broker ripristina a ogni avvio la policy blindata, evitando che una
  // configurazione più permissiva persista nel volume tra due esecuzioni.
  await fs.writeFile(
    settingsPath,
    `${JSON.stringify(defaults, null, 2)}\n`,
    { encoding: "utf8", mode: 0o600, flag: "w" },
  );
}

async function quotaStatus() {
  const status = await modelStatus();
  return {
    provider: "gemini",
    connected: status.connected,
    available: false,
    unlimited: false,
    source: "antigravity_cli",
    reason: status.connected ? "interactive_only" : "not_connected",
    remaining_percent: null,
    windows: [],
    checked_at: new Date().toISOString(),
  };
}

const settingsReady = ensureSafeSettings();

function cliEnvironment() {
  return {
    ...process.env,
    NO_COLOR: "1",
    BROWSER: "/bin/false",
    TERM: "xterm-256color",
    COLUMNS: "240",
    LINES: "50",
    AGY_CLI_HIDE_ACCOUNT_INFO: "1",
  };
}

function parseAvailableModels(output) {
  const plain = stripTerminalOutput(output).toLowerCase();
  return proprietaryModels.filter((model) => plain.includes(model.id));
}

async function modelStatus() {
  await settingsReady;
  try {
    const result = await runProcess(
      antigravityBinary,
      ["models"],
      {
        input: "",
        timeoutMs: 15_000,
        maxOutputBytes: 512 * 1024,
        env: cliEnvironment(),
        cwd: "/workspace",
      },
    );
    if (result.code !== 0) throw new Error("not_connected");
    const models = parseAvailableModels(
      `${result.stdout}\n${result.stderr}`,
    );
    return {
      installed: true,
      connected: true,
      auth_method: "google_antigravity",
      client_mode: "official_headless_cli",
      models,
    };
  } catch {
    return {
      installed: true,
      connected: false,
      auth_method: "none",
      client_mode: "official_headless_cli",
      models: [],
    };
  }
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
    attempt.process.stdin.write("\u0003\u0003");
    const child = attempt.process;
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

function extractGoogleAuthUrl(output) {
  const plain = stripTerminalOutput(output);
  const start = plain.indexOf("https://accounts.google.com/");
  if (start < 0) return null;
  const marker = plain.indexOf("If you aren't automatically redirected", start);
  if (marker < 0) return null;
  const compact = plain.slice(start, marker).replace(/\s+/gu, "");
  let candidate;
  try {
    candidate = new URL(compact);
  } catch {
    return null;
  }
  const redirect = candidate.searchParams.get("redirect_uri");
  const challenge = candidate.searchParams.get("code_challenge") || "";
  const state = candidate.searchParams.get("state") || "";
  if (
    candidate.protocol !== "https:" ||
    candidate.hostname !== "accounts.google.com" ||
    !["/o/oauth2/auth", "/o/oauth2/v2/auth"].includes(candidate.pathname) ||
    candidate.username ||
    candidate.password ||
    candidate.hash ||
    redirect !== "https://antigravity.google/oauth-callback" ||
    candidate.searchParams.get("response_type") !== "code" ||
    candidate.searchParams.get("code_challenge_method") !== "S256" ||
    !/^[A-Za-z0-9_-]{43,128}$/u.test(challenge) ||
    state.length < 8 ||
    state.length > 1024
  ) {
    return null;
  }
  return candidate.toString();
}

function startAuthentication() {
  cleanupExpiredAttempt();
  if (
    activeAttempt &&
    ["connected", "failed", "cancelled", "expired"].includes(
      activeAttempt.state,
    )
  ) {
    terminateAttempt(activeAttempt.state);
  }
  if (activeAttempt) return activeAttempt;

  const attempt = {
    id: crypto.randomUUID(),
    state: "starting",
    authUrl: null,
    outputBuffer: "",
    selectedLogin: false,
    process: null,
    expiryTimer: null,
    expiresAt: Date.now() + authTtlMs,
  };

  // `script` crea il controlling terminal richiesto dal client ufficiale per
  // ricevere il codice OAuth quando il broker gira in un container headless.
  const command =
    `stty cols 240 rows 50; exec ${antigravityBinary}`;
  const child = spawn(
    "/usr/bin/script",
    ["-qefc", command, "/dev/null"],
    {
      cwd: "/workspace",
      env: cliEnvironment(),
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  attempt.process = child;
  child.stdin.on("error", () => {
    if (attempt.state !== "connected") attempt.state = "failed";
  });
  attempt.expiryTimer = setTimeout(() => {
    if (activeAttempt?.id === attempt.id) terminateAttempt("expired");
  }, authTtlMs);
  attempt.expiryTimer.unref();
  activeAttempt = attempt;

  const consumeOutput = (chunk) => {
    // La TUI può contenere URL OAuth e dati account: nulla viene registrato.
    attempt.outputBuffer = `${attempt.outputBuffer}${chunk.toString("utf8")}`.slice(
      -256 * 1024,
    );
    const plain = stripTerminalOutput(attempt.outputBuffer);
    if (!attempt.selectedLogin && plain.includes("Select login method")) {
      attempt.selectedLogin = true;
      child.stdin.write("\r");
    }
    if (!attempt.authUrl) {
      const authUrl = extractGoogleAuthUrl(attempt.outputBuffer);
      if (authUrl) {
        attempt.authUrl = authUrl;
        attempt.state = "waiting_for_user";
      }
    }
    if (
      plain.includes("Authentication successful") ||
      plain.includes("Accesso riuscito")
    ) {
      attempt.state = "connected";
      if (attempt.expiryTimer) clearTimeout(attempt.expiryTimer);
      setTimeout(() => {
        if (child.exitCode === null) child.stdin.write("\u0003\u0003");
      }, 500).unref();
    }
  };

  child.stdout.on("data", consumeOutput);
  child.stderr.on("data", consumeOutput);
  child.on("error", () => {
    attempt.state = "failed";
  });
  child.on("exit", async () => {
    if (["cancelled", "expired"].includes(attempt.state)) return;
    const status = await modelStatus();
    attempt.state = status.connected ? "connected" : "failed";
  });

  return attempt;
}

async function waitForAuthUrl(attempt) {
  const deadline = Date.now() + 15_000;
  while (
    Date.now() < deadline &&
    attempt.state === "starting" &&
    !attempt.authUrl
  ) {
    await new Promise((resolve) => setTimeout(resolve, 75));
  }
  return attempt;
}

async function readAuthJsonBody(request) {
  return await readJsonBody(request, maxAuthBodyBytes);
}

async function completeChat(body) {
  const status = await modelStatus();
  if (!status.connected) {
    return {
      statusCode: 409,
      body: {
        error: "provider_not_connected",
        message: "Collega l'account Google ad Antigravity prima di usare Gemini.",
      },
    };
  }
  if (activeExecutions >= 2) {
    return {
      statusCode: 429,
      body: {
        error: "provider_busy",
        message: "Antigravity sta già elaborando troppe richieste.",
      },
    };
  }

  const models = status.models;
  let chat;
  try {
    chat = parseChatRequest(
      body,
      new Set(models.map((model) => model.id)),
      new Set(reasoningEfforts),
    );
  } catch {
    return {
      statusCode: 400,
      body: {
        error: "invalid_chat_request",
        message: "Modello, reasoning o messaggi Gemini non validi.",
      },
    };
  }
  const prompt = renderChatPrompt(chat.messages);
  if (prompt.length > maxPromptCharacters) {
    return {
      statusCode: 413,
      body: {
        error: "prompt_too_large",
        message: "Il prompt supera il limite del connettore Antigravity.",
      },
    };
  }

  const timeoutSeconds = Math.max(
    30,
    Math.ceil(executionTimeoutMs / 1000),
  );
  const args = [
    "--model",
    chat.model,
    "--effort",
    chat.reasoning,
    "--mode",
    "plan",
    "--sandbox",
    "--print-timeout",
    `${timeoutSeconds}s`,
  ];

  activeExecutions += 1;
  try {
    const result = await runProcess(antigravityBinary, args, {
      // Il contenuto resta su stdin: non compare nella command line del
      // processo e non viene scritto nei log del broker.
      input: prompt,
      cwd: "/workspace",
      env: cliEnvironment(),
      timeoutMs: executionTimeoutMs + 5_000,
      maxOutputBytes: 16 * 1024 * 1024,
    });
    const content = stripTerminalOutput(result.stdout).trim();
    if (result.code !== 0 || !content) {
      const diagnostic = stripTerminalOutput(result.stderr).toLowerCase();
      if (
        diagnostic.includes("quota") ||
        diagnostic.includes("rate limit") ||
        diagnostic.includes("too many requests")
      ) {
        return {
          statusCode: 429,
          body: {
            error: "provider_rate_limited",
            message: "La quota Antigravity del modello è momentaneamente esaurita.",
          },
        };
      }
      throw new Error("antigravity_failed");
    }
    return {
      statusCode: 200,
      body: { provider: "gemini", model: chat.model, content },
    };
  } catch {
    return {
      statusCode: 502,
      body: {
        error: "antigravity_execution_failed",
        message: "Antigravity CLI non ha completato la richiesta.",
      },
    };
  } finally {
    activeExecutions -= 1;
  }
}

async function disconnectAccount() {
  const status = await modelStatus();
  if (!status.connected) return true;

  return await new Promise((resolve) => {
    const command =
      `stty cols 240 rows 50; exec ${antigravityBinary}`;
    const child = spawn(
      "/usr/bin/script",
      ["-qefc", command, "/dev/null"],
      {
        cwd: "/workspace",
        env: cliEnvironment(),
        stdio: ["pipe", "pipe", "pipe"],
      },
    );
    let sentLogout = false;
    let finished = false;
    let output = "";
    let timer = null;

    const finish = (value) => {
      if (finished) return;
      finished = true;
      if (timer) clearTimeout(timer);
      if (child.exitCode === null) {
        child.stdin.write("\u0003\u0003");
        setTimeout(() => {
          if (child.exitCode === null) child.kill("SIGKILL");
        }, 1_000).unref();
      }
      resolve(value);
    };
    const consume = (chunk) => {
      output = `${output}${chunk.toString("utf8")}`.slice(-128 * 1024);
      const plain = stripTerminalOutput(output);
      if (
        !sentLogout &&
        (plain.includes("Where you are") ||
          plain.includes("Describe your next engineering task") ||
          plain.includes("Type ? for shortcuts"))
      ) {
        sentLogout = true;
        child.stdin.write("/logout\r");
      }
      if (
        sentLogout &&
        (plain.includes("Select login method") ||
          plain.includes("currently not signed in"))
      ) {
        finish(true);
      }
    };
    child.stdout.on("data", consume);
    child.stderr.on("data", consume);
    child.on("error", () => finish(false));
    child.on("exit", async () => {
      const nextStatus = await modelStatus();
      finish(!nextStatus.connected);
    });
    timer = setTimeout(async () => {
      const nextStatus = await modelStatus();
      finish(!nextStatus.connected);
    }, 15_000);
    timer.unref();
  });
}

async function route(request, response) {
  cleanupExpiredAttempt();
  const url = new URL(request.url || "/", `http://${request.headers.host}`);

  if (request.method === "GET" && url.pathname === "/healthz") {
    await settingsReady;
    jsonResponse(response, 200, { status: "ok" });
    return;
  }

  if (request.method === "GET" && url.pathname === "/v1/status") {
    const status = await modelStatus();
    if (status.connected && activeAttempt?.state === "connected") {
      if (activeAttempt.expiryTimer) clearTimeout(activeAttempt.expiryTimer);
      activeAttempt = null;
    }
    jsonResponse(response, 200, {
      provider: "gemini",
      ...status,
      attempt: publicAttempt(activeAttempt),
    });
    return;
  }

  if (request.method === "GET" && url.pathname === "/v1/models") {
    const status = await modelStatus();
    jsonResponse(response, 200, {
      provider: "gemini",
      connected: status.connected,
      models: status.models,
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
        message: "Richiesta Gemini non valida.",
      });
      return;
    }
    const result = await completeChat(body);
    jsonResponse(response, result.statusCode, result.body);
    return;
  }

  if (request.method === "POST" && url.pathname === "/v1/auth/start") {
    const status = await modelStatus();
    if (status.connected) {
      jsonResponse(response, 200, {
        provider: "gemini",
        ...status,
        attempt: null,
      });
      return;
    }
    const attempt = await waitForAuthUrl(startAuthentication());
    if (!attempt.authUrl) {
      terminateAttempt("failed");
      jsonResponse(response, 502, {
        error: "auth_flow_unavailable",
        message: "Antigravity CLI non ha prodotto un URL Google valido.",
      });
      return;
    }
    jsonResponse(response, 201, {
      provider: "gemini",
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
        message: "Tentativo di accesso Google non valido o scaduto.",
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
        message: "Il codice Google monouso non è valido.",
      });
      return;
    }
    activeAttempt.state = "verifying";
    if (
      !activeAttempt.process ||
      activeAttempt.process.exitCode !== null ||
      activeAttempt.process.stdin.destroyed
    ) {
      activeAttempt.state = "failed";
      jsonResponse(response, 409, {
        error: "auth_process_unavailable",
        message: "Il processo di accesso Google non è più disponibile.",
      });
      return;
    }
    activeAttempt.process.stdin.write(`${code}\r`);
    jsonResponse(response, 202, {
      provider: "gemini",
      connected: false,
      attempt: publicAttempt(activeAttempt),
    });
    return;
  }

  const cancelMatch = url.pathname.match(
    /^\/v1\/auth\/([0-9a-f-]{36})$/u,
  );
  if (request.method === "DELETE" && cancelMatch) {
    if (activeAttempt?.id === cancelMatch[1]) {
      terminateAttempt("cancelled");
    }
    jsonResponse(response, 200, { status: "cancelled" });
    return;
  }

  if (request.method === "DELETE" && url.pathname === "/v1/connection") {
    const disconnected = await disconnectAccount();
    jsonResponse(response, disconnected ? 200 : 502, disconnected
      ? { provider: "gemini", connected: false }
      : {
          error: "logout_failed",
          message: "Antigravity non ha completato la disconnessione.",
        });
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
      message: "Errore interno del broker Antigravity.",
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
