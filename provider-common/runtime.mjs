import { spawn } from "node:child_process";
import { StringDecoder } from "node:string_decoder";

const allowedRoles = new Set([
  "system",
  "developer",
  "user",
  "assistant",
  "tool",
]);
const maxImageBytes = 4 * 1024 * 1024;
const maxImages = 8;
const maxTotalImageBytes = 8 * 1024 * 1024;
const allowedImageTypes = new Set(["image/jpeg", "image/png", "image/webp"]);

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

export function parseChatRequest(body, allowedModels, allowedEfforts, limits = {}) {
  const maxMessages = limits.maxMessages ?? 200;
  const maxCharacters = limits.maxCharacters ?? 750_000;
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
    body.messages.length > maxMessages
  ) {
    throw new Error("invalid_chat_request");
  }

  let totalCharacters = 0;
  let totalImageBytes = 0;
  let imageCount = 0;
  const messages = body.messages.map((message) => {
    const role = typeof message?.role === "string" ? message.role : "";
    const { content, images } = parseMessageContent(message?.content);
    totalCharacters += content.length;
    for (const image of images) {
      imageCount += 1;
      totalImageBytes += image.byteLength;
    }
    if (
      !allowedRoles.has(role) ||
      (images.length > 0 && role !== "user") ||
      totalCharacters > maxCharacters ||
      imageCount > maxImages ||
      totalImageBytes > maxTotalImageBytes
    ) {
      throw new Error("invalid_chat_request");
    }
    return { role, content, images };
  });

  const maxOutputTokens =
    Number.isInteger(body.max_output_tokens) &&
    body.max_output_tokens > 0 &&
    body.max_output_tokens <= 131_072
      ? body.max_output_tokens
      : null;

  return { model, reasoning, messages, maxOutputTokens };
}

function parseMessageContent(value) {
  if (typeof value === "string") return { content: value, images: [] };
  if (!Array.isArray(value)) return { content: "", images: [] };

  const textParts = [];
  const images = [];
  for (const part of value) {
    if (
      (part?.type === "text" || part?.type === "input_text") &&
      typeof part.text === "string"
    ) {
      textParts.push(part.text);
      continue;
    }
    if (part?.type !== "image_url") continue;
    const url = part?.image_url?.url;
    images.push(parseImageDataUrl(url));
  }
  return { content: textParts.join("\n"), images };
}

function parseImageDataUrl(value) {
  if (typeof value !== "string") throw new Error("invalid_chat_request");
  const match = value.match(
    /^data:(image\/(?:jpeg|png|webp));base64,([A-Za-z0-9+/]+={0,2})$/u,
  );
  if (!match || !allowedImageTypes.has(match[1]) || match[2].length % 4 !== 0) {
    throw new Error("invalid_chat_request");
  }
  const data = Buffer.from(match[2], "base64");
  if (
    data.length < 1 ||
    data.length > maxImageBytes ||
    data.toString("base64").replace(/=+$/u, "") !==
      match[2].replace(/=+$/u, "") ||
    !matchesImageType(match[1], data)
  ) {
    throw new Error("invalid_chat_request");
  }
  return {
    mimeType: match[1],
    data: match[2],
    byteLength: data.length,
  };
}

function matchesImageType(mimeType, data) {
  if (mimeType === "image/jpeg") {
    return (
      data.length >= 4 &&
      data[0] === 0xff &&
      data[1] === 0xd8 &&
      data[2] === 0xff &&
      data[data.length - 2] === 0xff &&
      data[data.length - 1] === 0xd9
    );
  }
  if (mimeType === "image/png") {
    return data.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"));
  }
  return (
    data.length >= 12 &&
    data.subarray(0, 4).toString("ascii") === "RIFF" &&
    data.subarray(8, 12).toString("ascii") === "WEBP"
  );
}

export function renderChatPrompt(messages) {
  let imageIndex = 0;
  const transcript = messages
    .map(({ role, content, images = [] }) => {
      const markers = images.map(() => {
        imageIndex += 1;
        return `[Attached image ${imageIndex}]`;
      });
      return `<message role="${role}">\n${[...markers, content]
        .filter(Boolean)
        .join("\n")}\n</message>`;
    })
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
    signal,
  },
) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new Error("provider_process_aborted")); return; }
    const child = spawn(binary, args, {
      cwd,
      env,
      stdio: ["pipe", "pipe", "pipe"],
      detached: process.platform !== "win32",
    });
    let stdout = "";
    let stderr = "";
    let outputBytes = 0;
    let settled = false;
    let timer;
    const decoders = { stdout: new StringDecoder("utf8"), stderr: new StringDecoder("utf8") };
    const kill = (kind) => {
      try {
        if (process.platform !== "win32") process.kill(-child.pid, kind);
        else child.kill(kind);
      } catch { /* process already stopped */ }
    };
    const terminate = () => {
      kill("SIGTERM");
      const forceKill = setTimeout(() => kill("SIGKILL"), 2_000);
      forceKill.unref();
    };
    const aborted = () => { terminate(); finish(new Error("provider_process_aborted")); };

    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      signal?.removeEventListener("abort", aborted);
      if (error) reject(error);
      else resolve(result);
    };
    const consume = (target) => (chunk) => {
      outputBytes += chunk.length;
      if (outputBytes > maxOutputBytes) {
        kill("SIGKILL");
        finish(new Error("provider_output_too_large"));
        return;
      }
      if (target === "stdout") stdout += decoders.stdout.write(chunk);
      else stderr += decoders.stderr.write(chunk);
    };

    child.stdout.on("data", consume("stdout"));
    child.stderr.on("data", consume("stderr"));
    child.on("error", () => finish(new Error("provider_process_failed")));
    child.on("close", (code, exitSignal) => {
      stdout += decoders.stdout.end();
      stderr += decoders.stderr.end();
      if (exitSignal) {
        finish(new Error("provider_process_terminated"));
        return;
      }
      finish(null, { code, stdout, stderr });
    });
    child.stdin.on("error", () => {});
    signal?.addEventListener("abort", aborted, { once: true });
    child.stdin.end(input);

    timer = setTimeout(() => {
      terminate();
      finish(new Error("provider_process_timeout"));
    }, timeoutMs);
    timer.unref();
  });
}
