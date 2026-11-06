import assert from "node:assert/strict";
import { mkdtemp, writeFile, chmod, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { completeWithClientTools, hasTextualToolDefinitions, prepareClientTools, renderClientTranscript } from "./client-tools.mjs";

const tool = { type: "function", function: { name: "run_terminal", description: "Run on the client",
  parameters: { type: "object", properties: { command: { type: "string" } } } } };

test("tool history, choice and JSON arguments survive validation", () => {
  const body = { tools: [tool], tool_choice: { type: "function", function: { name: "run_terminal" } },
    messages: [{ role: "assistant", content: null, tool_calls: [{ id: "call_1", type: "function",
      function: { name: "run_terminal", arguments: '{"command":"pwd"}' } }] },
      { role: "tool", tool_call_id: "call_1", name: "run_terminal", content: "/workspace" }] };
  assert.equal(prepareClientTools(body).tools[0].name, "run_terminal");
  const prompt = renderClientTranscript(body.messages, body.tool_choice);
  assert.match(prompt, /call_1/);
  assert.match(prompt, /\/workspace/);
  assert.throws(() => prepareClientTools({ ...body, tool_choice: "invalid" }));
  assert.equal(prepareClientTools({ ...body, tools: [{ type: "function", function: { name: "run_terminal" } }] }).tools[0].description, "");
  assert.equal(hasTextualToolDefinitions([{ role: "developer", content: "tool_definition TOOL_NAME: run_terminal" }]), true);
});

test("app-server client call is returned to Continue without execution", async () => {
  const dir = await mkdtemp(join(tmpdir(), "omni-client-tools-"));
  const binary = join(dir, "fake-codex");
  await writeFile(binary, `#!/usr/bin/env node
const readline = require("node:readline");
readline.createInterface({input:process.stdin}).on("line", line => {
  const message = JSON.parse(line);
  if (message.method === "initialize") process.stdout.write(JSON.stringify({id:message.id,result:{}})+"\\n");
  if (message.method === "thread/start") process.stdout.write(JSON.stringify({id:message.id,result:{thread:{id:"thread-1"}}})+"\\n");
  if (message.method === "turn/start") {
    process.stdout.write(JSON.stringify({id:message.id,result:{turn:{id:"turn-1"}}})+"\\n");
    if (message.params.input[0].text.includes('tool_call_id')) {
      process.stdout.write(JSON.stringify({method:"item/agentMessage/delta",params:{threadId:"thread-1",delta:"Final result"}})+"\\n");
      process.stdout.write(JSON.stringify({method:"turn/completed",params:{threadId:"thread-1",turn:{status:"completed"}}})+"\\n");
    } else {
      process.stdout.write(JSON.stringify({id:77,method:"item/tool/call",params:{threadId:"thread-1",turnId:"turn-1",callId:"original",tool:"run_terminal",arguments:{command:"pwd"}}})+"\\n");
    }
  }
});

`);
  await chmod(binary, 0o700);
  try {
    const result = await completeWithClientTools({ model: "test", reasoning_effort: "low", tools: [tool],
      messages: [{ role: "user", content: "Check pwd" }] }, { codexBinary: process.execPath, codexArgs: [binary], cwd: process.cwd(), timeoutMs: 3000 });
    assert.equal(result.content, null);
    assert.equal(result.tool_calls[0].id, "original");
    assert.equal(result.tool_calls[0].function.name, "run_terminal");
    assert.deepEqual(JSON.parse(result.tool_calls[0].function.arguments), { command: "pwd" });
    const final = await completeWithClientTools({ model: "test", reasoning_effort: "low", tools: [tool],
      messages: [{ role: "user", content: "Check pwd" },
        { role: "assistant", content: null, tool_calls: result.tool_calls },
        { role: "tool", tool_call_id: result.tool_calls[0].id, content: "/workspace" }] },
      { codexBinary: process.execPath, codexArgs: [binary], cwd: process.cwd(), timeoutMs: 3000 });
    assert.equal(final.content, "Final result");
    assert.deepEqual(final.tool_calls, []);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("client disconnect interrupts a pending Codex tool turn", async () => {
  const dir = await mkdtemp(join(tmpdir(), "omni-client-abort-"));
  const binary = join(dir, "fake-codex");
  await writeFile(binary, `#!/usr/bin/env node
const readline = require("node:readline");
readline.createInterface({input:process.stdin}).on("line", line => {
  const message = JSON.parse(line);
  if (message.method === "initialize") process.stdout.write(JSON.stringify({id:message.id,result:{}})+"\\n");
  if (message.method === "thread/start") process.stdout.write(JSON.stringify({id:message.id,result:{thread:{id:"thread-1"}}})+"\\n");
  if (message.method === "turn/start") process.stdout.write(JSON.stringify({id:message.id,result:{turn:{id:"turn-1"}}})+"\\n");
});
`);
  await chmod(binary, 0o700);
  try {
    const controller = new AbortController();
    const started = Date.now();
    const completion = completeWithClientTools({ model: "test", reasoning_effort: "low",
      tools: [tool], messages: [{ role: "user", content: "Wait" }] },
    { codexBinary: process.execPath, codexArgs: [binary], cwd: process.cwd(),
      timeoutMs: 3000, signal: controller.signal });
    setTimeout(() => controller.abort(), 100);
    await assert.rejects(completion, /client_disconnected/);
    assert.ok(Date.now() - started < 1500);
    await assert.rejects(
      completeWithClientTools({ model: "test", reasoning_effort: "low",
        tools: [tool], messages: [{ role: "user", content: "Wait" }] },
      { codexBinary: process.execPath, codexArgs: [binary], signal: controller.signal }),
      /client_disconnected/,
    );
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
