import assert from "node:assert/strict";
import { Readable } from "node:stream";
import test from "node:test";
import { runProcess } from "./runtime.mjs";

test("process output preserves UTF-8 across chunks and waits for closed pipes", async () => {
  const result = await runProcess(process.execPath, ["-e", "const text=Buffer.from('città');process.stdout.write(text.subarray(0,text.length-1));setTimeout(()=>process.stdout.write(text.subarray(text.length-1)),20)"], { input: "", timeoutMs: 2000 });
  assert.equal(result.stdout, "città");
});

test("an aborted process is stopped and an already aborted request never starts", async () => {
  const controller = new AbortController();
  const running = runProcess(process.execPath, ["-e", "setInterval(()=>{},1000)"], { input: "", timeoutMs: 5000, signal: controller.signal });
  controller.abort();
  await assert.rejects(running, /provider_process_aborted/u);
  await assert.rejects(runProcess("must-never-spawn", [], { input: "", signal: controller.signal }), /provider_process_aborted/u);
});
import { parseChatRequest, readJsonBody } from "./runtime.mjs";

const models = new Set(["test-model"]);
const efforts = new Set(["medium"]);

test("Codex can accept a longer bounded Continue transcript", () => {
  const body = { model: "test-model", reasoning_effort: "medium",
    messages: Array.from({ length: 201 }, () => ({ role: "user", content: "x".repeat(4000) })) };
  assert.throws(() => parseChatRequest(body, models, efforts));
  assert.equal(parseChatRequest(body, models, efforts,
    { maxMessages: 1000, maxCharacters: 2_000_000 }).messages.length, 201);
  assert.throws(() => parseChatRequest({ ...body, messages: [{ role: "invalid", content: "x" }] },
    models, efforts, { maxMessages: 1000, maxCharacters: 2_000_000 }));
});

test("the Codex body cap is explicit while JSON remains validated", async () => {
  const payload = JSON.stringify({ content: "x".repeat(1024 * 1024 + 100) });
  await assert.rejects(readJsonBody(Readable.from([Buffer.from(payload)])));
  assert.equal((await readJsonBody(Readable.from([Buffer.from(payload)]), 32 * 1024 * 1024)).content.length,
    1024 * 1024 + 100);
  await assert.rejects(readJsonBody(Readable.from([Buffer.from("{broken")]), 32 * 1024 * 1024));
});
