"use strict";
const el = (id) => document.getElementById(id);
const state = {me: null, project: null, keys: [], editing: null};
async function api(path, method = "GET", body) {
  const response = await fetch(`/admin/v1${path}`, {method, headers: {"Content-Type": "application/json", "X-OmniProxy-Request": "dashboard"}, body: body === undefined ? undefined : JSON.stringify(body)});
  if (response.status === 401) { location.assign("/admin/login"); throw new Error("Sessione scaduta."); }
  if (!response.ok) { const value = await response.json(); throw new Error(value.error?.message || "Operazione non disponibile."); }
  return response.status === 204 ? null : response.json();
}
function report(error) { el("team-status").textContent = error.message; }
function options(target, values, label) {
  target.replaceChildren();
  for (const value of values) { const option = document.createElement("option"); option.value = value.id; option.textContent = label(value); target.append(option); }
}
function manager() { return ["owner", "admin"].includes(state.me.tenants.find(t => t.tenant_id === el("tenant").value)?.role); }
function reset() { state.editing = null; el("key-form").reset(); el("key-title").textContent = "Nuova chiave applicativa"; el("key-name").disabled = false; el("key-profile").disabled = false; el("key-budget").disabled = true; }
async function loadProject() {
  state.project = el("project").value;
  reset();
  el("key-form").hidden = !manager() || !state.project;
  el("new-secret").textContent = "";
  el("profiles").replaceChildren(); el("keys").replaceChildren();
  if (!state.project) return;
  const [profiles, keys] = await Promise.all([api(`/projects/${state.project}/profiles`), api(`/projects/${state.project}/keys`)]);
  options(el("key-profile"), profiles.data.filter(p => p.active), p => p.name || p.slug);
  for (const profile of profiles.data) { const row = document.createElement("li"); row.textContent = `${profile.name || profile.slug}: ${profile.provider} / ${profile.model}`; el("profiles").append(row); }
  state.keys = keys.data;
  for (const key of state.keys) {
    const row = document.createElement("li");
    const text = document.createElement("p"); text.textContent = `${key.name} · ${key.key_hint} · ${key.is_active ? key.status : "revocata"} · ${JSON.stringify(key.limits)}`; row.append(text);
    if (manager() && key.is_active) {
      const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "Modifica limiti";
      edit.addEventListener("click", () => {
        reset(); state.editing = key.id; el("key-title").textContent = "Modifica limiti"; el("key-name").value = key.name; el("key-name").disabled = true; el("key-profile").value = key.profile_id; el("key-profile").disabled = true;
        for (const [field, name] of [["input", "input_tokens"], ["output", "output_tokens"], ["context", "context_tokens"], ["budget", "token_budget"]]) el(`key-${field}`).value = key.limits[name] ?? "";
        el("key-mode").value = key.limits.context_mode || "reject"; el("key-period").value = key.limits.budget_period || "unlimited"; el("key-budget").disabled = el("key-period").value === "unlimited";
      });
      const revoke = document.createElement("button"); revoke.type = "button"; revoke.textContent = "Revoca";
      revoke.addEventListener("click", () => api(`/projects/${state.project}/keys/${key.id}`, "DELETE").then(loadProject).catch(report));
      row.append(edit, revoke);
    }
    el("keys").append(row);
  }
}
async function loadTenant() {
  const projects = await api(`/tenants/${el("tenant").value}/projects`);
  options(el("project"), projects.data.filter(p => p.active), p => p.name);
  el("project-form").hidden = !manager();
  await loadProject();
}
el("tenant").addEventListener("change", () => loadTenant().catch(report));
el("project").addEventListener("change", () => loadProject().catch(report));
el("key-period").addEventListener("change", () => { el("key-budget").disabled = el("key-period").value === "unlimited"; el("key-budget").required = !el("key-budget").disabled; });
el("key-reset").addEventListener("click", reset);
el("clear-secret").addEventListener("click", () => { el("new-secret").textContent = ""; });
el("project-form").addEventListener("submit", (event) => { event.preventDefault(); api(`/tenants/${el("tenant").value}/projects`, "POST", {name: el("project-name").value}).then(loadTenant).catch(report); });
el("key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const number = (id) => el(id).value === "" ? null : Number(el(id).value);
  const limits = {input_tokens: number("key-input"), output_tokens: number("key-output"), context_tokens: number("key-context"), context_mode: el("key-mode").value, budget_period: el("key-period").value, token_budget: el("key-period").value === "unlimited" ? null : number("key-budget")};
  try {
    if (state.editing) { await api(`/projects/${state.project}/keys/${state.editing}/limits`, "PATCH", limits); await loadProject(); }
    else { const value = await api(`/projects/${state.project}/keys`, "POST", {name: el("key-name").value, profile_id: Number(el("key-profile").value), limits}); await loadProject(); el("new-secret").textContent = value.api_key; }
    el("team-status").textContent = "Salvato.";
  } catch (error) { report(error); }
});
el("logout").addEventListener("click", () => api(`/sessions/${state.me.session_id}`, "DELETE").then(() => location.assign("/admin/login")).catch(report));
(async () => { state.me = await api("/me"); el("identity").textContent = state.me.name; options(el("tenant"), state.me.tenants.map(t => ({...t, id: t.tenant_id})), t => `${t.name} (${t.role})`); if (state.me.tenants.length) await loadTenant(); })().catch(report);
