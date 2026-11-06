export const sessionPolicy = Object.freeze({
  toolPermission: "strict",
  artifactReviewPolicy: "asks-for-review",
  enableTerminalSandbox: true,
  // Telemetry is account-owned in CLI 1.5; --settings rejects that key.
  useG1Credits: false,
  allowNonWorkspaceAccess: false,
  historySize: 0,
  statusLine: { enabled: false },
  title: { enabled: false },
  permissions: { deny: ["read_file(*)", "write_file(*)", "read_url(*)", "execute_url(*)", "command(*)", "mcp(*)"], ask: [], allow: [] },
});

export function supportedVersion(version) {
  const parts = String(version).match(/^(\d+)\.(\d+)\.(\d+)$/u);
  if (!parts) return false;
  const [major, minor, patch] = parts.slice(1).map(Number);
  return major > 1 || (major === 1 && (minor > 5 || (minor === 5 && patch >= 1)));
}

export function headlessArguments(chat, timeoutSeconds) {
  return ["--model", chat.model, ...(chat.reasoning === "auto" ? [] : ["--effort", chat.reasoning]), "--mode", "plan", "--sandbox",
    "--input-format", "stream-json", "--output-format", "stream-json",
    "--disable-slash-commands", "--settings", JSON.stringify(sessionPolicy),
    "--print-timeout", `${timeoutSeconds}s`];
}

export function headlessInput(prompt) {
  return JSON.stringify({ event: "user", message: { content: prompt } }) + "\n";
}

export function headlessResult(output) {
  const results = String(output).split(/\r?\n/u).filter(Boolean).map((line) => JSON.parse(line)).filter((item) => item.event === "result");
  if (results.length !== 1 || results[0].result?.status !== "SUCCESS" || typeof results[0].result?.response !== "string" || !results[0].result.response.trim()) {
    throw new Error("invalid_antigravity_result");
  }
  return results[0].result.response;
}
