/* Runs inside the actual VS Code extension host; no credentials are logged. */
const fs = require("node:fs");
const path = require("node:path");
const vscode = require("vscode");

exports.run = async function run() {
  const extension = vscode.extensions.getExtension("Continue.continue");
  if (!extension) throw new Error("Continue extension not loaded");
  const api = await extension.activate();
  if (!api.extension?.core) throw new Error("Continue test hook unavailable");
  // The packaged extension stores workers in out/, while its source-test
  // mode expects out/llm/. Use production worker resolution after the hook.
  process.env.NODE_ENV = "production";
  const { config, errors } = await api.extension.configHandler.loadConfig();
  if (!config?.selectedModelByRole?.chat) throw new Error(`No configured chat model (${errors?.length || 0} config errors)`);
  let text = "";
  const stream = api.extension.core.invoke("llm/streamChat", {
    messages: [{ role: "user", content: "Reply with exactly IDE_STREAM_OK." }],
    completionOptions: { maxTokens: 128 },
  });
  for await (const chunk of stream) {
    if (typeof chunk.content === "string") text += chunk.content;
  }
  if (!text.includes("IDE_STREAM_OK")) throw new Error("IDE chat marker not received");
  const readTool = config.tools.find((tool) => tool.function?.name === "read_file");
  if (!readTool) throw new Error("IDE read_file tool unavailable");
  const markerPath = path.join(vscode.workspace.workspaceFolders[0].uri.fsPath, "marker.txt");
  const messages = [{ role: "user", content: `Read the client file ${JSON.stringify(markerPath)} with read_file using exactly that filepath. Then reply with exactly its marker.` }];
  const tools = [{ type: "function", function: readTool.function }];
  const calls = [];
  for await (const chunk of api.extension.core.invoke("llm/streamChat", {
    messages, completionOptions: { maxTokens: 256, tools, toolChoice: "required" },
  })) {
    if (chunk.toolCalls) calls.push(...chunk.toolCalls);
  }
  if (calls.length !== 1 || calls[0].function.name !== "read_file") throw new Error("IDE did not receive one read_file call");
  const result = await api.extension.core.invoke("tools/call", { toolCall: calls[0] });
  if (result.errorMessage) throw new Error(`IDE file tool failed: ${result.errorMessage}`);
  const content = result.contextItems.map((item) => item.content).join("\n");
  if (!content.includes("IDE_FILE_MARKER_928")) throw new Error(`IDE tool did not read the fixture: ${JSON.stringify(result).slice(0, 600)}`);
  messages.push({ role: "assistant", content: "", toolCalls: calls });
  messages.push({ role: "tool", content, toolCallId: calls[0].id });
  let final = "";
  for await (const chunk of api.extension.core.invoke("llm/streamChat", {
    messages, completionOptions: { maxTokens: 128, tools, toolChoice: "none" },
  })) {
    if (typeof chunk.content === "string") final += chunk.content;
  }
  if (!final.includes("IDE_FILE_MARKER_928")) throw new Error("IDE final answer lost the tool result");
  fs.writeFileSync(process.env.OMNIPROXY_IDE_RESULT, JSON.stringify({
    passed: true, extension: extension.packageJSON.version,
    vscode: vscode.version, source: "actual_extension_host", chat_stream: true,
    tool_call: true, client_file_read: true, final_answer: true,
  }));
};
