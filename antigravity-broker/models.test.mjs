import assert from "node:assert/strict";
import test from "node:test";
import { parseAvailableModels } from "./models.mjs";

test("models reflect the authenticated CLI catalog, including non-Gemini models", () => {
  const models = parseAvailableModels("claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\nclaude-opus-4-6-thinking\ngpt-oss-120b-medium\n");
  assert.deepEqual(models.map((item) => item.id), ["claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium"]);
  assert.equal(models[0].is_default, true);
  assert.equal(models[1].is_default, false);
});

test("terminal escapes and duplicates are removed without cataloging account or error text", () => {
  assert.deepEqual(parseAvailableModels("Account: private@example.com\nError: not authenticated\n\u001b[32mgemini-3.5-flash\u001b[0m\ngemini-3.5-flash\nsk-secret-12345\n--model=x\n").map((item) => item.id), ["gemini-3.5-flash"]);
});

test("known metadata is retained and headers alone never imply an available model", () => {
  const metadata = { id: "gemini-3.5-flash", display_name: "Gemini Flash", is_default: true };
  assert.deepEqual(parseAvailableModels("gemini-3.5-flash", [metadata]), [metadata]);
  assert.deepEqual(parseAvailableModels("Available models:\nPlease sign in\n"), []);
});
