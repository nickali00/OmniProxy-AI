import { spawn } from "node:child_process";

const allowedRoles = new Set([
  "system",
  "developer",
  "user",
  "assistant",
  "tool",
]);

export async function readJsonBody(request, maxBytes = 1024 * 1024) {
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBytes) throw new Error("body_too_large");
    chunks.push(chunk);
  }
  if (chunks.length === 0) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

export function parseChatRequest(body, allowedModels, allowedEfforts) {
  const model = typeof body.model === "string" ? body.model.trim() : "";
  const reasoning =
    typeof body.reasoning_effort === "string"
      ? body.reasoning_effort.trim().toLowerCase()
      : "auto";
  if (
    !/^[A-Za-z0-9._:-]{1,128}$/u.test(model) ||
    !allowedModels.has(model) ||
    !/^[a-z0-9_-]{1,32}$/u.test(reasoning) ||
    !allowedEfforts.has(reasoning) ||
    !Array.isArray(body.messages) ||
    body.messages.length < 1 ||
    body.messages.length > 200
  ) {
    throw new Error("invalid_chat_request");
  }

  let totalCharacters = 0;
  const messages = body.messages.map((message) => {
    const role = typeof message?.role === "string" ? message.role : "";
    const content =
      typeof message?.content === "string" ? message.content : "";
    totalCharacters += content.length;
    if (!allowedRoles.has(role) || totalCharacters > 750_000) {
      throw new Error("invalid_chat_request");
    }
    return { role, content };
  });

  const maxOutputTokens =
    Number.isInteger(body.max_output_tokens) &&
    body.max_output_tokens > 0 &&
    body.max_output_tokens <= 131_072
      ? body.max_output_tokens
      : null;

  return { model, reasoning, messages, maxOutputTokens };
}

export function renderChatPrompt(messages) {
  const transcript = messages
    .map(
      ({ role, content }) =>
        `<message role="${role}">\n${content}\n</message>`,
    )
    .join("\n\n");
  return [
    "Respond to the following chat conversation.",
    "Return only the final assistant answer for the user.",
    "Do not edit files, execute commands, or inspect the environment.",
    "",
    transcript,
    "",
    "<assistant>",
  ].join("\n");
}

export function runProcess(
  binary,
  args,
  {
    input,
    cwd,
    env,
    timeoutMs = 300_000,
    maxOutputBytes = 12 * 1024 * 1024,
  },
) {
  return new Promise((resolve, reject) => {
    const child = spawn(binary, args, {
      cwd,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let outputBytes = 0;
    let settled = false;
    let timer;

    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (error) reject(error);
      else resolve(result);
    };
    const consume = (target) => (chunk) => {
      outputBytes += chunk.length;
      if (outputBytes > maxOutputBytes) {
        child.kill("SIGKILL");
        finish(new Error("provider_output_too_large"));
        return;
      }
      if (target === "stdout") stdout += chunk.toString("utf8");
      else stderr += chunk.toString("utf8");
    };

    child.stdout.on("data", consume("stdout"));
    child.stderr.on("data", consume("stderr"));
    child.on("error", () => finish(new Error("provider_process_failed")));
    child.on("exit", (code, signal) => {
      if (signal) {
        finish(new Error("provider_process_terminated"));
        return;
      }
      finish(null, { code, stdout, stderr });
    });
    child.stdin.on("error", () => {});
    child.stdin.end(input);

    timer = setTimeout(() => {
      child.kill("SIGTERM");
      const forceKill = setTimeout(() => child.kill("SIGKILL"), 2_000);
      forceKill.unref();
      finish(new Error("provider_process_timeout"));
    }, timeoutMs);
    timer.unref();
  });
}
