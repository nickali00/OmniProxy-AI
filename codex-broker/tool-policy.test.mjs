import assert from "node:assert/strict";
import test from "node:test";
import { proxyToolArguments } from "./tool-policy.mjs";

test("proxy policy removes built-in execution, browsing, connectors and subagents", () => {
  const args = proxyToolArguments();
  for (const feature of ["shell_tool", "apps", "browser_use", "computer_use", "image_generation", "view_image", "multi_agent"]) {
    assert.ok(args.some((value, index) => value === "--disable" && args[index + 1] === feature));
  }
  assert.ok(args.includes("mcp_servers={}"));
  assert.ok(args.includes('web_search="disabled"'));
  assert.equal(args.some((arg) => /bypass|danger-full-access|--enable/u.test(arg)), false);
});
