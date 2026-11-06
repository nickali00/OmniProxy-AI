import { spawn } from "node:child_process";
import crypto from "node:crypto";
import readline from "node:readline";
import { proxyToolArguments } from "./tool-policy.mjs";

const toolNamePattern = /^[A-Za-z_][A-Za-z0-9_-]{0,63}$/u;

export function prepareClientTools(body) {
  if (!Array.isArray(body.messages) || !Array.isArray(body.tools)) {
    throw new Error("invalid_chat_request");
  }
  const names = new Set();
  const tools = body.tools.map((entry) => {
    const fn = entry?.function;
    if (entry?.type !== "function" || !toolNamePattern.test(fn?.name || "") ||
        names.has(fn.name) || (fn.description !== undefined && typeof fn.description !== "string") ||
        (fn.parameters !== undefined &&
          (typeof fn.parameters !== "object" || fn.parameters === null || Array.isArray(fn.parameters)))) {
      throw new Error("invalid_chat_request");
    }
    names.add(fn.name);
    return { type: "function", name: fn.name, description: fn.description || "",
      inputSchema: fn.parameters || { type: "object", properties: {} } };
  });
  const choice = body.tool_choice ?? "auto";
  const forced = typeof choice === "object" && choice?.type === "function"
    ? choice.function?.name : null;
  if ((typeof choice === "string" && !["auto", "none", "required"].includes(choice)) ||
      (typeof choice === "object" && !names.has(forced))) {
    throw new Error("invalid_chat_request");
  }
  for (const message of body.messages) {
    if (!message || !["system", "developer", "user", "assistant", "tool"].includes(message.role)) {
      throw new Error("invalid_chat_request");
    }
    if (message.role === "tool" && typeof message.tool_call_id !== "string") {
      throw new Error("invalid_chat_request");
    }
    if (message.tool_calls) {
      if (message.role !== "assistant" || !Array.isArray(message.tool_calls)) throw new Error("invalid_chat_request");
      for (const call of message.tool_calls) {
        if (call?.type !== "function" || typeof call.id !== "string" ||
            !toolNamePattern.test(call.function?.name || "") ||
            typeof call.function?.arguments !== "string") throw new Error("invalid_chat_request");
        try { JSON.parse(call.function.arguments); } catch { throw new Error("invalid_chat_request"); }
      }
    }
  }
  return { tools, choice, forced };
}

export function renderClientTranscript(messages, choice) {
  const parts = [
    "Continue client conversation. The transcript below contains all previous tool calls and client results.",
    "Only request a client tool when needed. The client executes it and sends the result in a later request.",
    "Never claim a tool ran before its result appears in the transcript.",
  ];
  if (choice === "none") parts.push("Do not call a tool in this turn.");
  if (choice === "required") parts.push("Call one available client tool before answering.");
  if (typeof choice === "object") parts.push(`Call the client tool ${choice.function.name} before answering.`);
  for (const message of messages) {
    const content = typeof message.content === "string" ? message.content :
      Array.isArray(message.content) ? message.content.filter((part) => part?.type === "text").map((part) => part.text).join("\n") : "";
    parts.push(JSON.stringify({ role: message.role, content, name: message.name,
      tool_call_id: message.tool_call_id, tool_calls: message.tool_calls }));
  }
  parts.push("Respond to the latest user message, or request exactly one client tool.");
  return parts.join("\n");
}

export function hasTextualToolDefinitions(messages) {
  return messages.some((message) =>
    ["system", "developer"].includes(message?.role) &&
    typeof message.content === "string" &&
    message.content.includes("tool_definition") &&
    message.content.includes("TOOL_NAME:"));
}

export async function completeWithClientTools(body, { codexBinary, codexArgs = [], cwd = "/workspace", timeoutMs = 300_000, signal } = {}) {
  const { tools, choice, forced } = prepareClientTools(body);
  if (signal?.aborted) throw new Error("client_disconnected");
  const child = spawn(codexBinary, [...codexArgs, "app-server", "--stdio", ...proxyToolArguments()], {
    cwd, env: { ...process.env, RUST_LOG: "error", NO_COLOR: "1" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  child.stderr.on("data", () => {});
  let nextId = 0;
  const pending = new Map();
  const lines = readline.createInterface({ input: child.stdout });
  let threadId = null;
  let answer = "";
  let settled = false;
  let resolveResult;
  let rejectResult;
  const completion = new Promise((resolve, reject) => { resolveResult = resolve; rejectResult = reject; });
  completion.catch(() => {});
  const finish = (error, result) => {
    if (settled) return;
    settled = true;
    for (const entry of pending.values()) {
      if (error) entry.reject(error);
      else entry.resolve(null);
    }
    pending.clear();
    if (error) rejectResult(error); else resolveResult(result);
    child.kill("SIGTERM");
  };
  const send = (method, params) => new Promise((resolve, reject) => {
    const id = ++nextId;
    pending.set(id, { resolve, reject });
    child.stdin.write(`${JSON.stringify({ id, method, params })}\n`);
  });
  const timer = setTimeout(() => finish(new Error("provider_process_timeout")), timeoutMs);
  const abort = () => finish(new Error("client_disconnected"));
  signal?.addEventListener("abort", abort, { once: true });
  child.on("error", () => finish(new Error("provider_process_failed")));
  child.on("exit", () => finish(new Error("provider_process_terminated")));
  lines.on("line", (line) => {
    let message;
    try { message = JSON.parse(line); } catch { return; }
    if (message.id !== undefined && pending.has(message.id)) {
      const entry = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) entry.reject(new Error("app_server_request_failed"));
      else entry.resolve(message.result);
      return;
    }
    if (message.method === "item/tool/call" && message.id !== undefined) {
      const call = message.params;
      if (call.threadId !== threadId || !tools.some((tool) => tool.name === call.tool) ||
          choice === "none" || (forced && call.tool !== forced)) {
        finish(new Error("invalid_client_tool_call"));
        return;
      }
      const args = call.arguments;
      if (!args || typeof args !== "object" || Array.isArray(args)) {
        finish(new Error("invalid_client_tool_arguments"));
        return;
      }
      const id = typeof call.callId === "string" && /^[A-Za-z0-9_-]{1,128}$/u.test(call.callId)
        ? call.callId : `call_${crypto.randomUUID().replaceAll("-", "")}`;
      finish(null, { content: null, tool_calls: [{ id,
        type: "function", function: { name: call.tool, arguments: JSON.stringify(args) } }] });
      return;
    }
    if (message.method === "item/agentMessage/delta" && message.params?.threadId === threadId) {
      answer += message.params.delta || "";
    }
    if (message.method === "turn/completed" && message.params?.threadId === threadId) {
      if (message.params.turn?.status === "completed" &&
          (choice === "required" || forced) && tools.length) {
        finish(new Error("required_tool_not_called"));
      } else if (message.params.turn?.status === "completed") finish(null, { content: answer.trim(), tool_calls: [] });
      else finish(new Error("codex_execution_failed"));
    }
  });
  try {
    await send("initialize", { clientInfo: { name: "omni_proxy_ai", title: "OmniProxy AI", version: "0.4.6" },
      capabilities: { experimentalApi: true } });
    child.stdin.write(`${JSON.stringify({ method: "initialized", params: {} })}\n`);
    if (settled) return await completion;
    const clientInstructions = body.messages
      .filter((message) => ["system", "developer"].includes(message.role))
      .map((message) => message.content)
      .filter((content) => typeof content === "string").join("\n\n");
    const started = await send("thread/start", { model: body.model, cwd, sandbox: "read-only",
      approvalPolicy: "never", ephemeral: true, dynamicTools: choice === "none" ? [] : tools,
      baseInstructions: "Use only the supplied client dynamic tools. Do not execute commands or use server tools. Return a normal answer when no tool is needed.\n\n" + clientInstructions });
    threadId = started.thread.id;
    await send("turn/start", { threadId, effort: body.reasoning_effort,
      input: [{ type: "text", text: renderClientTranscript(body.messages, choice) }] });
    return await completion;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
    child.kill("SIGKILL");
  }
}
