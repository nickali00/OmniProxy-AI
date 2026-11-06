const modelId = /^(?:gemini[-/]|claude[-/]|gpt[-/]|o[1-9][-/.]|deepseek[-/]|qwen[0-9./-]|gemma[0-9./-]|llama[0-9./-]|mistral[-/]|magistral[-/]|kimi[-/]|glm[-/]|minimax[-/]|moonshot[-/]|(?:openai|anthropic|google)\/)[a-z0-9][a-z0-9._:/-]{0,110}$/u;
const ansi = /\u001b\[[0-?]*[ -/]*[@-~]/gu;

export function parseAvailableModels(output, known = []) {
  const metadata = new Map(known.map((item) => [item.id, item]));
  const seen = new Set();
  const result = [];
  for (const line of String(output).replace(ansi, "").split(/\r?\n/u)) {
    // CLI 1.5 emits ID<TAB>display name; older versions emitted only ID.
    const id = line.trim().split("\t", 1)[0].toLowerCase();
    if (!modelId.test(id) || seen.has(id)) continue;
    seen.add(id);
    result.push(metadata.get(id) || {
      id,
      display_name: id,
      description: "Modello disponibile tramite l'account Antigravity.",
      is_default: result.length === 0,
      // Antigravity --effort is a Gemini-only control. Other vendors select
      // their reasoning variant through the model ID itself.
      reasoning_efforts: id.startsWith("gemini-") ? ["low", "medium", "high"] : ["auto"],
      default_reasoning_effort: id.startsWith("gemini-") ? "medium" : "auto",
    });
    if (result.length === 100) break;
  }
  return result;
}
