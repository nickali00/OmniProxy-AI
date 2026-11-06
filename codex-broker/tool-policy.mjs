// Both proxy entry points use the same CLI policy. Tool execution belongs
// to the external client, gateway MCP, or job executor, never this CLI.
const disabled = ["shell_tool", "apps", "browser_use", "browser_use_external",
  "browser_use_full_cdp_access", "in_app_browser", "browser_annotation_api",
  "computer_use", "image_generation", "view_image", "multi_agent",
  "multi_agent_v2", "multi_agent_v2_dynamic_tools", "sleep_tool", "tool_suggest"];

export function proxyToolArguments() {
  return [...disabled.flatMap((name) => ["--disable", name]),
    "-c", "mcp_servers={}", "-c", 'web_search="disabled"'];
}
