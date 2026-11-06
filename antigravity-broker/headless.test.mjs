import assert from "node:assert/strict";
import test from "node:test";
import { headlessArguments, headlessInput, headlessResult, sessionPolicy, supportedVersion } from "./headless.mjs";

test("unsupported CLI versions do not advertise a usable connection", () => {
  assert.equal(supportedVersion("1.1.7"), false);
  assert.equal(supportedVersion(null), false);
  assert.equal(supportedVersion("1.5.1"), true);
});

test("prompt stays on stdin and session-only settings deny tool side effects", () => {
  const marker = "private test prompt";
  const args = headlessArguments({ model: "claude-sonnet-4-6", reasoning: "low" }, 30);
  assert(!args.join(" ").includes(marker));
  assert(args.includes("stream-json"));
  assert(args.includes("--disable-slash-commands"));
  assert(!args.includes("--dangerously-skip-permissions"));
  for (const action of ["read_file", "write_file", "read_url", "execute_url", "command", "mcp"]) assert(sessionPolicy.permissions.deny.includes(`${action}(*)`));
  assert.equal(JSON.parse(headlessInput(marker)).message.content, marker);
});

test("only the final successful response is returned, never init or diagnostics", () => {
  assert.equal(headlessResult('{"event":"init","private":"not returned"}\n{"event":"result","result":{"status":"SUCCESS","response":"città"}}\n'), "città");
  for (const input of ['{"event":"result","result":{"status":"ERROR","response":"private"}}', '{"event":"init"}', "not JSON"]) assert.throws(() => headlessResult(input));
});
