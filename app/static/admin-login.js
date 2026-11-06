"use strict";
document.getElementById("admin-login").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("admin-token");
  const token = input.value.trim();
  input.value = "";
  const status = document.getElementById("admin-login-status");
  try {
    const response = await fetch("/admin/v1/session", {method: "POST", headers: {Authorization: `Bearer ${token}`}});
    if (!response.ok) throw new Error();
    const me = await fetch("/admin/v1/me");
    if (!me.ok) throw new Error();
    location.assign((await me.json()).operator ? "/" : "/admin");
  } catch {
    status.textContent = "Accesso non riuscito: verifica token e scadenza.";
  }
});
