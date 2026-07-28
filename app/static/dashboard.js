const gatewayCopyButton = document.querySelector("#copy-gateway-base-url");
const BUILD_ENABLED = document.body.dataset.buildEnabled === "true";
let gatewayCopyFeedbackTimer = null;

function tr(key, params = {}) {
  return window.omniI18n?.t(key, params) || key;
}

function currentLocale() {
  return window.omniI18n?.locale() || "en-GB";
}

function renderGatewayCopyButton(label, glyph, ariaLabel) {
  if (!gatewayCopyButton) return;
  const icon = document.createElement("span");
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = glyph;
  gatewayCopyButton.replaceChildren(icon, label);
  gatewayCopyButton.setAttribute("aria-label", ariaLabel);
}

function resetGatewayCopyButton() {
  renderGatewayCopyButton(
    tr("apis.copyUrl"),
    "⧉",
    tr("apis.copyUrl"),
  );
}

gatewayCopyButton?.addEventListener("click", async () => {
  const source = document.querySelector("#gateway-base-url");
  const value = source?.textContent?.trim();
  if (!value) return;
  window.clearTimeout(gatewayCopyFeedbackTimer);
  try {
    await navigator.clipboard.writeText(value);
    renderGatewayCopyButton(
      tr("apis.urlCopied"),
      "✓",
      tr("apis.urlCopied"),
    );
  } catch {
    renderGatewayCopyButton(
      tr("apis.copyFailed"),
      "!",
      tr("apis.copyFailed"),
    );
  }
  gatewayCopyFeedbackTimer = window.setTimeout(
    resetGatewayCopyButton,
    1800,
  );
});

const dialog = document.querySelector("#provider-dialog");
const closeButton = dialog?.querySelector(".dialog-close");
const primaryButton = dialog?.querySelector("#dialog-primary");
const secondaryButton = dialog?.querySelector("#dialog-secondary");
const authFlow = dialog?.querySelector("#auth-flow");
const authProgress = dialog?.querySelector("#auth-progress");
const authProgressText = dialog?.querySelector("#auth-progress-text");
const authLink = dialog?.querySelector("#official-auth-link");
const deviceCodePanel = dialog?.querySelector("#device-code-panel");
const deviceCode = dialog?.querySelector("#device-code");
const copyDeviceCodeButton = dialog?.querySelector("#copy-device-code");
const authCodeForm = dialog?.querySelector("#auth-code-form");
const authCodeLabel = dialog?.querySelector("#auth-code-label");
const authCodeHint = dialog?.querySelector("#auth-code-hint");
const authCodeInput = dialog?.querySelector("#auth-code");
const authFeedback = dialog?.querySelector("#auth-feedback");

const providerConfig = {
  ollama: {
    kind: "local",
    statusPath: "/api/providers/ollama/status",
    noteKey: "provider.note.ollama",
  },
  codex: {
    kind: "device",
    statusPath: "/api/providers/codex/status",
    startPath: "/api/providers/codex/auth/start",
    officialName: "ChatGPT",
    noteKey: "provider.note.codex",
  },
  gemini: {
    kind: "code",
    statusPath: "/api/providers/gemini/status",
    startPath: "/api/providers/gemini/auth/start",
    disconnectPath: "/api/providers/gemini/connection",
    officialName: "Google Antigravity",
    noteKey: "provider.note.gemini",
  },
  claude: {
    kind: "code",
    statusPath: "/api/providers/claude/status",
    startPath: "/api/providers/claude/auth/start",
    officialName: "Claude.ai",
    noteKey: "provider.note.claude",
  },
};

const providerState = Object.fromEntries(
  Object.keys(providerConfig).map((id) => [
    id,
    {
      connected: false,
      installed: null,
      attemptId: null,
      attemptState: null,
      authUrl: null,
      userCode: null,
      pollTimer: null,
      popup: null,
      details: null,
    },
  ]),
);

let activeProvider = null;

function setText(selector, value) {
  const element = document.querySelector(selector);
  if (element) element.textContent = value;
}

function setAuthProgress(message, state = "loading") {
  if (!authProgress || !authProgressText) return;
  authProgress.classList.toggle("complete", state === "complete");
  authProgress.classList.toggle("error", state === "error");
  authProgressText.textContent = message;
}

function setFeedback(message = "", state = "") {
  if (!authFeedback) return;
  authFeedback.textContent = message;
  authFeedback.className = `auth-feedback${state ? ` ${state}` : ""}`;
}

function isAllowedAuthUrl(provider, value) {
  try {
    const url = new URL(value);
    const clean =
      url.protocol === "https:" &&
      url.port === "" &&
      url.hash === "" &&
      url.username === "" &&
      url.password === "";
    if (!clean) return false;
    if (provider === "claude") {
      return (
        url.hostname === "claude.com" &&
        url.pathname === "/cai/oauth/authorize"
      );
    }
    if (provider === "codex") {
      return (
        ["auth.openai.com", "chatgpt.com", "device.openai.com"].includes(
          url.hostname,
        ) && url.pathname !== "/"
      );
    }
    if (provider === "gemini") {
      const redirect = url.searchParams.get("redirect_uri");
      const challenge = url.searchParams.get("code_challenge") || "";
      const state = url.searchParams.get("state") || "";
      return (
        url.hostname === "accounts.google.com" &&
        ["/o/oauth2/auth", "/o/oauth2/v2/auth"].includes(url.pathname) &&
        redirect === "https://antigravity.google/oauth-callback" &&
        url.searchParams.get("response_type") === "code" &&
        url.searchParams.get("code_challenge_method") === "S256" &&
        /^[A-Za-z0-9_-]{43,128}$/.test(challenge) &&
        state.length >= 8 &&
        state.length <= 1024
      );
    }
    return false;
  } catch {
    return false;
  }
}

async function dashboardRequest(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (options.body) headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET") {
    headers.set("X-OmniProxy-Request", "dashboard");
  }

  const response = await fetch(path, {
    ...options,
    headers,
    cache: "no-store",
    credentials: "same-origin",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof payload.message === "string"
        ? payload.message
        : tr("common.operationFailed"),
    );
  }
  return payload;
}

function updateConnectionCount(card, connected) {
  const previous = card.dataset.providerConnected === "true";
  if (previous === connected) return;
  card.dataset.providerConnected = String(connected);

  const summary = document.querySelector("#connection-summary");
  const count = document.querySelector("#connection-count");
  if (!summary || !count) return;
  const total = Number.parseInt(summary.dataset.totalProviders || "0", 10);
  const current = Number.parseInt(summary.dataset.connectedCount || "0", 10);
  const next = Math.max(0, Math.min(total, current + (connected ? 1 : -1)));
  summary.dataset.connectedCount = String(next);
  count.textContent = `${next}/${total}`;
  const navCount = document.querySelector(".nav-count");
  if (navCount) navCount.textContent = String(next);
}

function liveLabel(provider, state) {
  if (provider === "ollama") {
    return state.connected
      ? tr("provider.status.containerConnected")
      : state.installed === false
        ? tr("provider.status.containerMissing")
        : tr("provider.status.searchingContainer");
  }
  return state.connected
    ? tr("provider.status.connected")
    : state.installed === false
      ? tr("provider.status.brokerUnavailable")
      : tr("provider.status.readyToSignIn");
}

function updateProviderCard(provider, payload) {
  const state = providerState[provider];
  const card = document.querySelector(`[data-provider="${provider}"]`);
  if (!state || !card) return;
  state.connected = payload.connected === true;
  state.installed = payload.installed !== false;
  state.details = payload;
  updateConnectionCount(card, state.connected);

  const label = liveLabel(provider, state);
  const stateElement = card.querySelector("[data-provider-state]");
  const action = card.querySelector("[data-provider-action-label]");
  const actionLabel =
    provider === "ollama"
      ? state.connected
        ? tr("provider.action.viewModels")
        : tr("provider.action.searchAgain")
      : state.connected
        ? tr("provider.action.viewConnection")
        : tr("provider.action.connectAccount");

  if (stateElement) {
    stateElement.className = `provider-state ${
      state.connected ? "state-connected" : "state-missing"
    }`;
    stateElement.replaceChildren(document.createElement("i"));
    stateElement.append(label);
  }
  if (action) action.textContent = actionLabel;

  document
    .querySelectorAll(`[data-open-provider="${provider}"]`)
    .forEach((trigger) => {
      trigger.dataset.providerState = label;
      const triggerLabel = trigger.querySelector("[data-provider-action-label]");
      if (triggerLabel) triggerLabel.textContent = actionLabel;
    });

  if (activeProvider === provider) setText("#dialog-status", label);
}

function clearDialogFlow() {
  if (!authFlow) return;
  authFlow.hidden = true;
  if (authLink) {
    authLink.hidden = true;
    authLink.removeAttribute("href");
  }
  if (deviceCodePanel) deviceCodePanel.hidden = true;
  if (deviceCode) deviceCode.textContent = "";
  if (authCodeForm) authCodeForm.hidden = true;
  if (authCodeInput) authCodeInput.value = "";
  if (secondaryButton) secondaryButton.hidden = true;
  if (primaryButton) primaryButton.hidden = false;
  setFeedback();
  setAuthProgress(tr("dialog.preparingSecureSignIn"));
}

function renderOllamaDialog() {
  const state = providerState.ollama;
  if (!primaryButton) return;
  primaryButton.disabled = false;
  primaryButton.textContent = state.connected
    ? tr("provider.action.refreshDetection")
    : tr("provider.action.searchContainer");
  if (state.connected) {
    const models = Array.isArray(state.details?.models)
      ? state.details.models.length
      : 0;
    setText(
      "#dialog-note",
      tr("dialog.detectedModels", {
        note: tr(providerConfig.ollama.noteKey),
        count: models,
        model: state.details?.configured_model || tr("dialog.notDefined"),
      }),
    );
  }
}

function renderProviderDialog(provider) {
  const config = providerConfig[provider];
  const state = providerState[provider];
  if (!config || !state || !primaryButton) return;
  if (config.kind === "local") {
    renderOllamaDialog();
    return;
  }

  authFlow.hidden = false;
  primaryButton.hidden = false;
  if (state.connected) {
    setAuthProgress(
      tr("dialog.accountConnected", { name: config.officialName }),
      "complete",
    );
    setFeedback(
      provider === "gemini"
        ? tr("dialog.googleSessionProtected")
        : tr("dialog.sessionProtected"),
      "success",
    );
    if (authLink) authLink.hidden = true;
    if (deviceCodePanel) deviceCodePanel.hidden = true;
    if (authCodeForm) authCodeForm.hidden = true;
    if (secondaryButton) {
      secondaryButton.hidden = provider !== "gemini";
      secondaryButton.textContent =
        provider === "gemini"
          ? tr("dialog.disconnect")
          : tr("dialog.cancelSignIn");
    }
    primaryButton.disabled = false;
    primaryButton.textContent = tr("dialog.close");
    return;
  }

  if (["failed", "expired"].includes(state.attemptState)) {
    setAuthProgress(tr("dialog.signInIncomplete"), "error");
    setFeedback(
      state.attemptState === "expired"
        ? tr("dialog.requestExpired")
        : tr("dialog.clientFailed"),
      "error",
    );
    if (authCodeForm) authCodeForm.hidden = true;
    if (deviceCodePanel) deviceCodePanel.hidden = true;
    if (secondaryButton) secondaryButton.hidden = true;
    primaryButton.disabled = false;
    primaryButton.textContent = tr("dialog.retry");
    return;
  }

  primaryButton.disabled = false;
  primaryButton.textContent = state.attemptId
    ? tr("dialog.reopen", { name: config.officialName })
    : tr("provider.action.connectAccount");
  setAuthProgress(
    state.attemptId
      ? tr("dialog.waiting", { name: config.officialName })
      : tr("dialog.clientReady"),
  );

  if (!state.attemptId) return;
  if (secondaryButton) {
    secondaryButton.hidden = false;
    secondaryButton.textContent = tr("dialog.cancelSignIn");
  }
  if (authLink && state.authUrl) {
    authLink.hidden = false;
    authLink.href = state.authUrl;
    const authLinkLabel = authLink.querySelector("[data-i18n]");
    if (authLinkLabel) {
      authLinkLabel.textContent = tr("dialog.openOfficialNamed", {
        name: config.officialName,
      });
    }
  }
  if (config.kind === "device" && state.userCode) {
    if (deviceCode) deviceCode.textContent = state.userCode;
    if (deviceCodePanel) deviceCodePanel.hidden = false;
  }
  if (
    config.kind === "code" &&
    state.attemptState === "waiting_for_user"
  ) {
    if (authCodeLabel) {
      authCodeLabel.textContent = tr("dialog.providerNamedCode", {
        name: config.officialName,
      });
    }
    if (authCodeHint) {
      authCodeHint.textContent = tr("dialog.codeHint");
    }
    if (authCodeForm) authCodeForm.hidden = false;
  }
}

function stopPolling(provider) {
  const state = providerState[provider];
  if (state?.pollTimer) window.clearInterval(state.pollTimer);
  if (state) state.pollTimer = null;
}

function startPolling(provider) {
  stopPolling(provider);
  providerState[provider].pollTimer = window.setInterval(
    () => refreshProviderStatus(provider),
    2000,
  );
}

async function refreshProviderStatus(provider) {
  const config = providerConfig[provider];
  const state = providerState[provider];
  try {
    const status = await dashboardRequest(config.statusPath);
    updateProviderCard(provider, status);
    const attempt = status.attempt;
    if (attempt && typeof attempt.id === "string") {
      state.attemptId = attempt.id;
      state.attemptState = attempt.state;
      if (typeof attempt.user_code === "string") {
        state.userCode = attempt.user_code;
      }
      if (["failed", "expired"].includes(attempt.state)) {
        stopPolling(provider);
      }
    }
    if (status.connected) {
      state.attemptId = null;
      state.attemptState = "connected";
      state.authUrl = null;
      state.userCode = null;
      stopPolling(provider);
      if (state.popup && !state.popup.closed) state.popup.close();
    }
    if (activeProvider === provider && dialog?.open) {
      renderProviderDialog(provider);
    }
  } catch {
    updateProviderCard(provider, { connected: false, installed: false });
    if (activeProvider === provider && dialog?.open) {
      setAuthProgress(
        provider === "ollama"
          ? "Container Ollama non trovato"
          : `Broker ${provider} non raggiungibile`,
        "error",
      );
      setFeedback(
        provider === "ollama"
          ? "Avvia un container Ollama raggiungibile e prova di nuovo."
          : "Il container provider non è disponibile. Controlla Docker e riprova.",
        "error",
      );
    }
  }
}

function navigateOAuthPopup(provider, url) {
  if (!isAllowedAuthUrl(provider, url)) {
    throw new Error("La destinazione di accesso restituita non è consentita.");
  }
  const state = providerState[provider];
  const config = providerConfig[provider];
  state.authUrl = url;
  if (authLink) {
    authLink.href = url;
    authLink.hidden = false;
    const authLinkLabel = authLink.querySelector("[data-i18n]");
    if (authLinkLabel) {
      authLinkLabel.textContent = tr("dialog.openOfficialNamed", {
        name: config.officialName,
      });
    }
  }
  if (state.popup && !state.popup.closed) {
    state.popup.location.replace(url);
  } else {
    setFeedback(
      "Il browser ha bloccato la finestra. Usa il collegamento ufficiale qui sopra.",
    );
  }
}

async function startProviderAuth(provider, { reusePopup = false } = {}) {
  const config = providerConfig[provider];
  const state = providerState[provider];
  if (!primaryButton || !config || config.kind === "local") return;
  if (state.connected) {
    dialog?.close();
    return;
  }
  if (state.authUrl) {
    window.open(state.authUrl, "_blank", "noopener,noreferrer");
    return;
  }

  if (!reusePopup || !state.popup || state.popup.closed) {
    state.popup = window.open(
      "about:blank",
      `omni-${provider}-auth`,
      "popup,width=660,height=780",
    );
    if (state.popup) state.popup.opener = null;
  }

  primaryButton.disabled = true;
  state.attemptState = "starting";
  authFlow.hidden = false;
  setAuthProgress("Creo una richiesta di accesso monouso…");
  setFeedback();

  try {
    const result = await dashboardRequest(config.startPath, { method: "POST" });
    updateProviderCard(provider, result);
    if (result.connected) {
      if (state.popup && !state.popup.closed) state.popup.close();
      renderProviderDialog(provider);
      return;
    }
    if (!result.attempt || typeof result.attempt.id !== "string") {
      throw new Error("Il client ufficiale non ha avviato l’accesso.");
    }
    state.attemptId = result.attempt.id;
    state.attemptState = result.attempt.state;
    state.userCode =
      typeof result.attempt.user_code === "string"
        ? result.attempt.user_code
        : null;
    navigateOAuthPopup(provider, result.auth_url);
    setAuthProgress(
      `Completa l’accesso nella pagina ufficiale ${config.officialName}`,
    );
    setFeedback(
      config.kind === "device"
        ? "Inserisci nella pagina ChatGPT il codice mostrato qui sotto."
        : config.kind === "code"
          ? `Se ${config.officialName} mostra un codice monouso, incollalo qui sotto.`
          : `Completa il consenso OAuth su ${config.officialName}.`,
    );
    primaryButton.disabled = false;
    renderProviderDialog(provider);
    startPolling(provider);
  } catch (error) {
    if (state.popup && !state.popup.closed) state.popup.close();
    state.authUrl = null;
    state.attemptId = null;
    setAuthProgress("Accesso non avviato", "error");
    setFeedback(error.message, "error");
    primaryButton.disabled = false;
    primaryButton.textContent = tr("dialog.retry");
  }
}

async function submitProviderCode(event) {
  event.preventDefault();
  const provider = activeProvider;
  const config = providerConfig[provider];
  const state = providerState[provider];
  if (config?.kind !== "code" || !state?.attemptId || !authCodeInput) return;
  const code = authCodeInput.value.trim();
  authCodeInput.value = "";
  if (code.length < 8) {
    setFeedback("Il codice inserito è troppo corto.", "error");
    return;
  }
  setAuthProgress("Verifica del codice monouso…");
  setFeedback();
  state.attemptState = "verifying";
  authCodeForm.hidden = true;
  try {
    await dashboardRequest(
      `/api/providers/${provider}/auth/${encodeURIComponent(
        state.attemptId,
      )}/code`,
      { method: "POST", body: JSON.stringify({ code }) },
    );
    setFeedback(`Codice inoltrato direttamente al client ${provider}.`);
    startPolling(provider);
    window.setTimeout(() => refreshProviderStatus(provider), 650);
  } catch (error) {
    state.attemptState = "waiting_for_user";
    setAuthProgress("Codice non accettato", "error");
    setFeedback(error.message, "error");
    authCodeForm.hidden = false;
  }
}

async function cancelProviderAuth() {
  const provider = activeProvider;
  const config = providerConfig[provider];
  const state = providerState[provider];
  if (!state || config?.kind === "local") return;
  if (provider === "gemini" && state.connected) {
    secondaryButton.disabled = true;
    try {
      await dashboardRequest(config.disconnectPath, { method: "DELETE" });
      state.connected = false;
      state.attemptId = null;
      state.authUrl = null;
      await refreshProviderStatus(provider);
      setFeedback("Sessione Google rimossa dal client Antigravity.", "success");
    } catch (error) {
      setFeedback(error.message, "error");
    } finally {
      secondaryButton.disabled = false;
    }
    return;
  }
  const attemptId = state.attemptId;
  stopPolling(provider);
  state.attemptId = null;
  state.attemptState = null;
  state.authUrl = null;
  state.userCode = null;
  if (state.popup && !state.popup.closed) state.popup.close();
  if (attemptId) {
    try {
      await dashboardRequest(
        `/api/providers/${provider}/auth/${encodeURIComponent(attemptId)}`,
        { method: "DELETE" },
      );
    } catch {
      // La cancellazione è idempotente; il refresh riallinea lo stato.
    }
  }
  clearDialogFlow();
  renderProviderDialog(provider);
}

function openProvider(button) {
  if (!dialog) return;
  activeProvider = button.dataset.openProvider;
  const config = providerConfig[activeProvider];
  setText("#dialog-title", button.dataset.providerName || "Provider");
  setText(
    "#dialog-status",
    liveLabel(activeProvider, providerState[activeProvider]),
  );
  setText(
    "#dialog-auth",
    tr(`provider.${activeProvider}.auth`) ||
      button.dataset.providerAuth ||
      "",
  );
  setText("#dialog-client", button.dataset.providerClient || "");
  setText(
    "#dialog-note",
    config?.noteKey ? tr(config.noteKey) : tr("provider.note.ollama"),
  );
  clearDialogFlow();
  renderProviderDialog(activeProvider);
  dialog.showModal();
  refreshProviderStatus(activeProvider);
}

document.querySelectorAll("[data-open-provider]").forEach((button) => {
  button.addEventListener("click", () => openProvider(button));
});

closeButton?.addEventListener("click", () => dialog.close());
primaryButton?.addEventListener("click", () => {
  if (activeProvider === "ollama") {
    refreshProviderStatus("ollama");
    return;
  }
  startProviderAuth(activeProvider);
});
secondaryButton?.addEventListener("click", cancelProviderAuth);
authCodeForm?.addEventListener("submit", submitProviderCode);
copyDeviceCodeButton?.addEventListener("click", async () => {
  const code = providerState.codex.userCode;
  if (!code) return;
  try {
    await navigator.clipboard.writeText(code);
    copyDeviceCodeButton.textContent = tr("apis.urlCopied");
    window.setTimeout(() => {
      copyDeviceCodeButton.textContent = tr("dialog.copy");
    }, 1500);
  } catch {
    setFeedback("Seleziona e copia manualmente il codice.", "error");
  }
});
dialog?.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});

Promise.allSettled(
  Object.keys(providerConfig).map((provider) =>
    refreshProviderStatus(provider),
  ),
);

const catalogState = {
  providers: [],
  filter: null,
  loaded: false,
};
let managedApis = [];
const MANAGED_API_BASE_URL = "http://gateway:8000/v1";

const apiDialog = document.querySelector("#api-dialog");
const apiForm = document.querySelector("#api-config-form");
const apiSecretResult = document.querySelector("#api-secret-result");
const apiConfigId = document.querySelector("#api-config-id");
const apiName = document.querySelector("#api-name");
const apiProvider = document.querySelector("#api-provider");
const apiModel = document.querySelector("#api-model");
const apiReasoning = document.querySelector("#api-reasoning");
const apiRoutingPreview = document.querySelector("#api-routing-preview");
const apiFormFeedback = document.querySelector("#api-form-feedback");
const apiSave = document.querySelector("#api-save");
const newApiSecret = document.querySelector("#new-api-secret");
const newApiModel = document.querySelector("#new-api-model");
const newApiBaseUrl = document.querySelector("#new-api-base-url");
const copyApiSecret = document.querySelector("#copy-api-secret");
const secretResultClose = document.querySelector("#secret-result-close");

function switchView(view) {
  if (view === "build" && !BUILD_ENABLED) view = "connections";
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.viewPanel !== view;
  });
  document.querySelectorAll("[data-view-target]").forEach((button) => {
    button.classList.toggle("active", button.dataset.viewTarget === view);
  });
  if (view === "models") loadModelCatalog();
  if (view === "apis") {
    loadModelCatalog();
    loadManagedApis();
  }
  if (view === "usage") loadUsage();
  if (view === "build" && BUILD_ENABLED) {
    loadModelCatalog().then(() => loadBuildProjects());
  }
  if (window.location.hash !== `#${view}`) {
    window.history.replaceState(null, "", `#${view}`);
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

document.querySelectorAll("[data-view-target]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.viewTarget));
});

function catalogProvider(providerId) {
  return catalogState.providers.find((provider) => provider.id === providerId);
}

function option(value, label, { disabled = false } = {}) {
  const element = document.createElement("option");
  element.value = value;
  element.textContent = label;
  element.disabled = disabled;
  return element;
}

function renderProviderFilters() {
  const filters = document.querySelector("#provider-filters");
  if (!filters) return;
  filters.replaceChildren();
  const availableProviders = catalogState.providers.filter(
    (provider) => provider.connected && provider.models.length > 0,
  );
  if (
    !availableProviders.some(
      (provider) => provider.id === catalogState.filter,
    )
  ) {
    catalogState.filter = availableProviders[0]?.id || null;
  }
  const glyphs = {
    ollama: "◎",
    codex: "✣",
    gemini: "✦",
    claude: "A",
  };
  availableProviders.forEach((provider) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `provider-filter${
      catalogState.filter === provider.id ? " active" : ""
    }`;
    const glyph = document.createElement("span");
    glyph.className = "provider-filter-glyph";
    glyph.textContent = glyphs[provider.id] || "•";
    const copy = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = provider.name;
    const count = document.createElement("small");
    count.textContent = tr("models.count", {
      count: provider.models.length,
    });
    copy.append(name, count);
    button.append(glyph, copy);
    button.addEventListener("click", () => {
      catalogState.filter = provider.id;
      renderProviderFilters();
      renderModelCatalog();
    });
    filters.append(button);
  });
}

function modelCard(provider, model) {
  const article = document.createElement("article");
  article.className = `catalog-model-card${
    provider.connected ? " available" : ""
  }`;
  article.dataset.provider = provider.id;

  const top = document.createElement("div");
  top.className = "catalog-model-top";
  const providerName = document.createElement("span");
  providerName.className = "catalog-provider";
  providerName.textContent = provider.name;
  const availability = document.createElement("span");
  availability.className = "catalog-availability";
  availability.append(document.createElement("i"));
  availability.append(
    provider.connected
      ? tr("models.available")
      : tr("models.providerDisconnected"),
  );
  top.append(providerName, availability);

  const title = document.createElement("h2");
  title.textContent = model.display_name;
  const id = document.createElement("div");
  id.className = "catalog-model-id";
  id.textContent = model.id;
  const description = document.createElement("p");
  description.className = "catalog-model-description";
  description.textContent =
    model.description || tr("models.defaultDescription");
  const efforts = document.createElement("div");
  efforts.className = "reasoning-tags";
  model.reasoning_efforts.forEach((effort) => {
    const tag = document.createElement("span");
    tag.textContent = effort;
    efforts.append(tag);
  });
  const action = document.createElement("button");
  action.type = "button";
  action.disabled = !provider.connected;
  action.textContent = provider.connected
    ? tr("models.createApi")
    : tr("models.connectFirst");
  if (provider.connected) {
    action.addEventListener("click", () => {
      switchView("apis");
      openApiConfigDialog({
        provider: provider.id,
        model: model.id,
        reasoning: model.default_reasoning_effort,
      });
    });
  }
  article.append(top, title, id, description, efforts, action);
  return article;
}

function renderModelCatalog() {
  const grid = document.querySelector("#model-catalog-grid");
  const summary = document.querySelector("#catalog-summary");
  if (!grid || !summary) return;
  grid.replaceChildren();
  const provider = catalogProvider(catalogState.filter);
  if (!provider || !provider.connected || provider.models.length === 0) {
    const empty = document.createElement("div");
    empty.className = "catalog-empty";
    const title = document.createElement("h2");
    title.textContent = tr("models.noneTitle");
    const copy = document.createElement("p");
    copy.textContent = tr("models.noneCopy");
    empty.append(title, copy);
    grid.append(empty);
    summary.textContent = tr("models.availableCount", { count: 0 });
    return;
  }
  provider.models.forEach((model) => {
    grid.append(modelCard(provider, model));
  });
  summary.textContent = tr("models.providerAvailableCount", {
    provider: provider.name,
    count: provider.models.length,
  });
}

async function loadModelCatalog(force = false) {
  if (catalogState.loaded && !force) return;
  const summary = document.querySelector("#catalog-summary");
  if (summary) summary.textContent = tr("models.updating");
  try {
    const result = await dashboardRequest("/api/models/catalog");
    catalogState.providers = Array.isArray(result.providers)
      ? result.providers
      : [];
    catalogState.loaded = true;
    const availableCount = catalogState.providers.reduce(
      (total, provider) =>
        total + (provider.connected ? provider.models.length : 0),
      0,
    );
    const navCount = document.querySelector(".nav-model-count");
    if (navCount) navCount.textContent = String(availableCount);
    renderProviderFilters();
    renderModelCatalog();
  } catch (error) {
    if (summary) summary.textContent = error.message;
  }
}

document.querySelector("#refresh-models")?.addEventListener("click", () => {
  loadModelCatalog(true);
});

function selectedModel() {
  return catalogProvider(apiProvider?.value)?.models.find(
    (model) => model.id === apiModel?.value,
  );
}

function populateApiProviders(selectedProvider = null) {
  if (!apiProvider) return;
  apiProvider.replaceChildren();
  catalogState.providers.forEach((provider) => {
    if (provider.models.length === 0) return;
    apiProvider.append(
      option(
        provider.id,
        `${provider.name}${
          provider.connected
            ? ""
            : ` · ${tr("models.providerDisconnected")}`
        }`,
        {
          disabled:
            !provider.connected && provider.id !== selectedProvider,
        },
      ),
    );
  });
  if (selectedProvider) apiProvider.value = selectedProvider;
  if (!apiProvider.value) {
    const firstAvailable = catalogState.providers.find(
      (provider) => provider.connected && provider.models.length,
    );
    if (firstAvailable) apiProvider.value = firstAvailable.id;
  }
}

function populateApiModels(selectedModelId = null) {
  if (!apiModel || !apiProvider) return;
  apiModel.replaceChildren();
  const provider = catalogProvider(apiProvider.value);
  provider?.models.forEach((model) => {
    apiModel.append(option(model.id, model.display_name));
  });
  if (selectedModelId) apiModel.value = selectedModelId;
  if (!apiModel.value) {
    const defaultModel = provider?.models.find((model) => model.is_default);
    if (defaultModel) apiModel.value = defaultModel.id;
  }
  populateApiReasoning();
}

function populateApiReasoning(selectedReasoning = null) {
  if (!apiReasoning) return;
  apiReasoning.replaceChildren();
  const model = selectedModel();
  model?.reasoning_efforts.forEach((effort) => {
    apiReasoning.append(option(effort, effort));
  });
  if (selectedReasoning) apiReasoning.value = selectedReasoning;
  if (!apiReasoning.value && model) {
    apiReasoning.value = model.default_reasoning_effort;
  }
  renderApiRoutingPreview();
}

function renderApiRoutingPreview() {
  if (!apiRoutingPreview) return;
  const provider = catalogProvider(apiProvider?.value);
  const model = selectedModel();
  apiRoutingPreview.textContent =
    provider && model
      ? `${provider.name} → ${model.id} → reasoning:${apiReasoning.value}`
      : tr("apiDialog.selectModel");
}

function setApiFeedback(message = "", state = "") {
  if (!apiFormFeedback) return;
  apiFormFeedback.textContent = message;
  apiFormFeedback.className = `auth-feedback${state ? ` ${state}` : ""}`;
}

async function openApiConfigDialog(prefill = {}) {
  await loadModelCatalog();
  if (!apiDialog || !apiForm || !apiSecretResult) return;
  apiForm.hidden = false;
  apiSecretResult.hidden = true;
  newApiSecret.textContent = "";
  copyApiSecret.textContent = tr("apiDialog.copyKey");
  setApiFeedback();

  const existing = prefill.id
    ? managedApis.find((item) => item.id === prefill.id)
    : null;
  apiConfigId.value = existing ? String(existing.id) : "";
  apiName.value = existing?.name || prefill.name || "";
  setText(
    "#api-dialog-title",
    existing ? tr("apiDialog.edit") : tr("apiDialog.new"),
  );
  apiSave.textContent = existing
    ? tr("apiDialog.save")
    : tr("apiDialog.create");

  const providerId = existing?.provider || prefill.provider || null;
  const modelId = existing?.model || prefill.model || null;
  const reasoning =
    existing?.reasoning_effort || prefill.reasoning || null;
  populateApiProviders(providerId);
  populateApiModels(modelId);
  populateApiReasoning(reasoning);
  apiDialog.showModal();
  window.setTimeout(() => apiName.focus(), 50);
}

apiProvider?.addEventListener("change", () => populateApiModels());
apiModel?.addEventListener("change", () => populateApiReasoning());
apiReasoning?.addEventListener("change", renderApiRoutingPreview);

function closeApiDialog() {
  if (newApiSecret) newApiSecret.textContent = "";
  apiDialog?.close();
}

document.querySelectorAll("[data-api-dialog-close]").forEach((button) => {
  button.addEventListener("click", closeApiDialog);
});
document.querySelector("#create-api")?.addEventListener("click", () => {
  openApiConfigDialog();
});
document.querySelector("[data-empty-create-api]")?.addEventListener("click", () => {
  openApiConfigDialog();
});

apiForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = apiConfigId.value;
  const payload = {
    name: apiName.value.trim(),
    provider: apiProvider.value,
    model: apiModel.value,
    reasoning_effort: apiReasoning.value,
  };
  apiSave.disabled = true;
  setApiFeedback(
    id ? tr("apiDialog.saving") : tr("apiDialog.creating"),
  );
  try {
    const result = await dashboardRequest(
      id ? `/api/gateway-apis/${encodeURIComponent(id)}` : "/api/gateway-apis",
      {
        method: id ? "PATCH" : "POST",
        body: JSON.stringify(payload),
      },
    );
    await loadManagedApis();
    if (id) {
      closeApiDialog();
      return;
    }
    apiForm.hidden = true;
    apiSecretResult.hidden = false;
    newApiSecret.textContent = result.api_key;
    newApiBaseUrl.textContent = MANAGED_API_BASE_URL;
    newApiModel.textContent = result.slug;
  } catch (error) {
    setApiFeedback(error.message, "error");
  } finally {
    apiSave.disabled = false;
  }
});

copyApiSecret?.addEventListener("click", async () => {
  const secret = newApiSecret.textContent;
  if (!secret) return;
  try {
    await navigator.clipboard.writeText(secret);
    copyApiSecret.textContent = tr("apiDialog.keyCopied");
  } catch {
    newApiSecret.scrollIntoView({ block: "center" });
  }
});
secretResultClose?.addEventListener("click", closeApiDialog);

function renderManagedApis() {
  const list = document.querySelector("#managed-api-list");
  const empty = document.querySelector("#managed-api-empty");
  const navCount = document.querySelector(".nav-api-count");
  if (!list || !empty) return;
  list.replaceChildren();
  empty.hidden = managedApis.length !== 0;
  if (navCount) navCount.textContent = String(managedApis.length);

  managedApis.forEach((item) => {
    const card = document.createElement("article");
    card.className = `managed-api-card${
      item.status === "paused" ? " paused" : ""
    }`;
    card.dataset.status = item.status || "active";

    const identity = document.createElement("div");
    const titleRow = document.createElement("div");
    titleRow.className = "api-identity-title";
    const title = document.createElement("h2");
    title.textContent = item.name;
    const status = document.createElement("span");
    status.className = `api-status${
      item.status === "paused" ? " paused" : ""
    }`;
    status.textContent =
      item.status === "paused"
        ? tr("apis.status.paused")
        : tr("apis.status.active");
    titleRow.append(title, status);
    const slug = document.createElement("div");
    slug.className = "api-slug";
    slug.textContent = `model: ${item.slug}`;
    identity.append(titleRow, slug);

    const routing = document.createElement("div");
    const route = document.createElement("div");
    route.className = "api-route";
    const provider = document.createElement("strong");
    provider.textContent = item.provider;
    const arrowOne = document.createTextNode("→");
    const model = document.createElement("strong");
    model.textContent = item.model;
    const arrowTwo = document.createTextNode("→");
    const reasoning = document.createElement("span");
    reasoning.textContent = item.reasoning_effort;
    route.append(provider, arrowOne, model, arrowTwo, reasoning);
    const keyMeta = document.createElement("div");
    keyMeta.className = "api-key-meta";
    const hint = document.createElement("code");
    hint.textContent = item.key_hint;
    const used = document.createElement("small");
    used.textContent = item.last_used_at
      ? tr("apis.lastUsed", {
        date: new Date(item.last_used_at).toLocaleString(currentLocale()),
      })
      : tr("apis.neverUsed");
    keyMeta.append(hint, used);
    routing.append(route, keyMeta);

    const actions = document.createElement("div");
    actions.className = "api-card-actions";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.dataset.toggleApi = String(item.id);
    toggle.dataset.status = item.status || "active";
    toggle.textContent =
      item.status === "paused" ? tr("apis.enable") : tr("apis.disable");
    toggle.addEventListener("click", () => {
      toggleManagedApi(item, toggle);
    });
    const edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = tr("apis.edit");
    edit.addEventListener("click", () => openApiConfigDialog({ id: item.id }));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.dataset.deleteApi = String(item.id);
    remove.textContent = tr("apis.delete");
    remove.addEventListener("click", () => deleteManagedApi(item));
    actions.append(toggle, edit, remove);
    card.append(identity, routing, actions);
    list.append(card);
  });
}

async function loadManagedApis() {
  try {
    const result = await dashboardRequest("/api/gateway-apis");
    managedApis = Array.isArray(result.data) ? result.data : [];
    renderManagedApis();
  } catch {
    managedApis = [];
    renderManagedApis();
  }
}

async function toggleManagedApi(item, button) {
  const enabling = item.status === "paused";
  button.disabled = true;
  button.textContent = enabling
    ? tr("apis.enabling")
    : tr("apis.disabling");
  try {
    const result = await dashboardRequest(
      `/api/keys/${encodeURIComponent(item.api_key_id)}/toggle`,
      { method: "PATCH" },
    );
    item.status = result.status === "paused" ? "paused" : "active";
    renderManagedApis();
  } catch (error) {
    button.disabled = false;
    button.textContent = enabling ? tr("apis.enable") : tr("apis.disable");
    window.alert(error.message);
  }
}

async function deleteManagedApi(item) {
  const confirmed = window.confirm(
    tr("apis.deleteConfirm", { name: item.name }),
  );
  if (!confirmed) return;
  try {
    await dashboardRequest(
      `/api/gateway-apis/${encodeURIComponent(item.id)}`,
      { method: "DELETE" },
    );
    await loadManagedApis();
  } catch (error) {
    window.alert(error.message);
  }
}

const usageState = {
  loaded: false,
  loading: false,
  data: null,
};
const usagePeriod = document.querySelector("#usage-period");
const usageApiFilter = document.querySelector("#usage-api-filter");
const usageProviderFilter = document.querySelector(
  "#usage-provider-filter",
);
const usageRefreshButton = document.querySelector("#refresh-usage");
const providerQuotaState = {
  loaded: false,
  loading: false,
  data: null,
};

function formatUsageNumber(value) {
  return new Intl.NumberFormat(currentLocale()).format(Number(value) || 0);
}

function formatUsageLatency(value) {
  const milliseconds = Number(value) || 0;
  if (milliseconds >= 1000) {
    return `${(milliseconds / 1000).toLocaleString(currentLocale(), {
      maximumFractionDigits: 1,
    })} s`;
  }
  return `${formatUsageNumber(milliseconds)} ms`;
}

function usageProviderName(provider) {
  return {
    ollama: "Ollama",
    codex: "Codex",
    gemini: "Gemini",
    claude: "Claude",
    external_mock: tr("common.externalMock"),
    unresolved: tr("common.unresolved"),
  }[provider] || provider;
}

function quotaProviderGlyph(provider) {
  return {
    ollama: "◎",
    codex: "✣",
    gemini: "✦",
    claude: "◈",
  }[provider] || "•";
}

function quotaPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return Math.min(100, Math.max(0, number));
}

function formatQuotaPercent(value) {
  const percent = quotaPercent(value);
  if (percent === null) return "—";
  return percent.toLocaleString(currentLocale(), {
    maximumFractionDigits: Number.isInteger(percent) ? 0 : 1,
  });
}

function formatQuotaWindow(minutes) {
  const duration = Number(minutes);
  if (!Number.isFinite(duration) || duration <= 0) {
    return tr("quota.window");
  }
  if (duration % 1440 === 0) {
    const days = duration / 1440;
    return tr(days === 1 ? "quota.day" : "quota.days", { count: days });
  }
  if (duration % 60 === 0) {
    const hours = duration / 60;
    return tr(hours === 1 ? "quota.hour" : "quota.hours", {
      count: hours,
    });
  }
  return tr("quota.minutes", { count: duration });
}

function formatQuotaReset(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "";
  return tr("quota.reset", {
    date: new Date(timestamp * 1000).toLocaleString(currentLocale(), {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    }),
  });
}

function quotaPresentation(provider) {
  if (provider.unlimited === true && provider.connected === true) {
    return {
      value: "∞",
      unit: tr("quota.local"),
      caption: tr("quota.localCaption"),
      percent: 100,
      status: tr("quota.unlimited"),
    };
  }
  const remaining = quotaPercent(provider.remaining_percent);
  if (provider.available === true && remaining !== null) {
    return {
      value: formatQuotaPercent(remaining),
      unit: "%",
      caption: tr("quota.remainingCaption"),
      percent: remaining,
      status: tr("quota.live"),
    };
  }
  if (provider.reason === "not_connected") {
    return {
      value: "—",
      unit: "",
      caption: provider.provider === "ollama"
        ? tr("quota.ollamaOffline")
        : tr("quota.connectAccount"),
      percent: 0,
      status: tr("quota.offline"),
    };
  }
  if (provider.reason === "interactive_only") {
    return {
      value: "—",
      unit: "",
      caption: tr("quota.interactiveOnly"),
      percent: 0,
      status: tr("quota.clientOnly"),
    };
  }
  return {
    value: "—",
    unit: "",
    caption: tr("quota.temporaryUnavailable"),
    percent: 0,
    status: tr("quota.unavailable"),
  };
}

function renderProviderQuotas(data = {}) {
  const grid = document.querySelector("#provider-quota-grid");
  if (!grid) return;
  grid.replaceChildren();
  const providers = Array.isArray(data.providers) ? data.providers : [];
  providers.forEach((provider) => {
    const presentation = quotaPresentation(provider);
    const card = document.createElement("article");
    card.className = `provider-quota-card${
      provider.available === true ? "" : " quota-unavailable"
    }`;
    card.dataset.provider = provider.provider;

    const head = document.createElement("div");
    head.className = "provider-quota-head";
    const identity = document.createElement("div");
    identity.className = "provider-quota-provider";
    const glyph = document.createElement("span");
    glyph.className = "provider-quota-glyph";
    glyph.setAttribute("aria-hidden", "true");
    glyph.textContent = quotaProviderGlyph(provider.provider);
    const name = document.createElement("strong");
    name.textContent = usageProviderName(provider.provider);
    identity.append(glyph, name);
    const live = document.createElement("span");
    live.className = "provider-quota-live";
    live.textContent = presentation.status;
    head.append(identity, live);

    const value = document.createElement("div");
    value.className = "provider-quota-value";
    value.append(document.createTextNode(presentation.value));
    if (presentation.unit) {
      const unit = document.createElement("small");
      unit.textContent = presentation.unit;
      value.append(unit);
    }
    const caption = document.createElement("p");
    caption.className = "provider-quota-caption";
    caption.textContent = presentation.caption;
    const track = document.createElement("div");
    track.className = "provider-quota-track";
    const fill = document.createElement("i");
    fill.style.width = `${presentation.percent}%`;
    track.append(fill);
    card.append(head, value, caption, track);

    const windows = Array.isArray(provider.windows)
      ? provider.windows.slice(0, 4)
      : [];
    if (windows.length > 0) {
      const windowList = document.createElement("div");
      windowList.className = "provider-quota-windows";
      windows.forEach((window) => {
        const row = document.createElement("div");
        row.className = "provider-quota-window";
        const label = document.createElement("span");
        label.textContent = (
          `${formatQuotaWindow(window.window_minutes)}` +
          formatQuotaReset(window.resets_at)
        );
        const amount = document.createElement("b");
        amount.textContent = tr("quota.remaining", {
          percent: formatQuotaPercent(window.remaining_percent),
        });
        row.append(label, amount);
        windowList.append(row);
      });
      card.append(windowList);
    }
    grid.append(card);
  });
}

async function loadProviderQuotas(force = false) {
  if (
    providerQuotaState.loading ||
    (providerQuotaState.loaded && !force)
  ) return;
  providerQuotaState.loading = true;
  setText("#provider-quota-status", tr("quota.updating"));
  try {
    providerQuotaState.data = await dashboardRequest(
      "/api/providers/quotas",
    );
    providerQuotaState.loaded = true;
    renderProviderQuotas(providerQuotaState.data);
    setText(
      "#provider-quota-status",
      tr("quota.checked", {
        time: new Date().toLocaleTimeString(currentLocale(), {
          hour: "2-digit",
          minute: "2-digit",
        }),
      }),
    );
  } catch (error) {
    setText("#provider-quota-status", tr("quota.failed"));
    const grid = document.querySelector("#provider-quota-grid");
    if (grid) {
      const unavailable = document.createElement("article");
      unavailable.className =
        "provider-quota-card quota-loading quota-unavailable";
      unavailable.textContent = error.message;
      grid.replaceChildren(unavailable);
    }
  } finally {
    providerQuotaState.loading = false;
  }
}

function setUsageStatus(message = "", type = "") {
  const element = document.querySelector("#usage-status");
  if (!element) return;
  element.textContent = message;
  element.className = type;
}

function populateUsageFilters(filters = {}) {
  if (usageApiFilter) {
    const selected = usageApiFilter.value;
    usageApiFilter.replaceChildren(
      option("", tr("usage.allApis")),
      ...(filters.apis || []).map((item) =>
        option(
          String(item.api_key_id),
          item.api_slug
            ? `${item.api_name} · ${item.api_slug}`
            : item.api_name,
        ),
      ),
    );
    if ([...usageApiFilter.options].some((item) => item.value === selected)) {
      usageApiFilter.value = selected;
    }
  }
  if (usageProviderFilter) {
    const selected = usageProviderFilter.value;
    usageProviderFilter.replaceChildren(
      option("", tr("usage.allProviders")),
      ...(filters.providers || []).map((provider) =>
        option(provider, usageProviderName(provider)),
      ),
    );
    if (
      [...usageProviderFilter.options].some(
        (item) => item.value === selected,
      )
    ) {
      usageProviderFilter.value = selected;
    }
  }
}

function renderUsageRanking(selector, rows, labelForRow, detailForRow) {
  const container = document.querySelector(selector);
  if (!container) return;
  container.replaceChildren();
  if (!Array.isArray(rows) || rows.length === 0) {
    const empty = document.createElement("p");
    empty.className = "usage-empty";
    empty.textContent = tr("usage.noRanking");
    container.append(empty);
    return;
  }
  const maximum = Math.max(
    1,
    ...rows.map((row) => Number(row.total_tokens) || 0),
  );
  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "usage-rank-row";
    const copy = document.createElement("div");
    copy.className = "usage-rank-copy";
    const labels = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = labelForRow(row);
    const detail = document.createElement("small");
    detail.textContent = detailForRow(row);
    labels.append(name, detail);
    const total = document.createElement("b");
    total.textContent = tr("usage.tokenCount", {
      count: formatUsageNumber(row.total_tokens),
    });
    copy.append(labels, total);
    const track = document.createElement("div");
    track.className = "usage-rank-track";
    const fill = document.createElement("i");
    fill.style.width = `${Math.max(
      2,
      ((Number(row.total_tokens) || 0) / maximum) * 100,
    )}%`;
    track.append(fill);
    item.append(copy, track);
    container.append(item);
  });
}

function renderUsageTimeline(rows = []) {
  const timeline = document.querySelector("#usage-timeline");
  const empty = document.querySelector("#usage-timeline-empty");
  if (!timeline || !empty) return;
  timeline.replaceChildren();
  empty.hidden = rows.length !== 0;
  timeline.hidden = rows.length === 0;
  if (rows.length === 0) return;
  const maximum = Math.max(
    1,
    ...rows.map((row) => Number(row.total_tokens) || 0),
  );
  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "usage-day";
    item.title = (
      `${row.day}: ${formatUsageNumber(row.prompt_tokens)} input, ` +
      `${formatUsageNumber(row.completion_tokens)} output, ` +
      tr("usage.requestCount", {
        count: formatUsageNumber(row.request_count),
      })
    );
    const bars = document.createElement("div");
    bars.className = "usage-day-bars";
    const input = document.createElement("i");
    input.className = "usage-day-bar";
    input.style.height = `${Math.max(
      2,
      ((Number(row.prompt_tokens) || 0) / maximum) * 100,
    )}%`;
    const output = document.createElement("i");
    output.className = "usage-day-bar output";
    output.style.height = `${Math.max(
      2,
      ((Number(row.completion_tokens) || 0) / maximum) * 100,
    )}%`;
    bars.append(input, output);
    const label = document.createElement("label");
    label.textContent = new Date(`${row.day}T12:00:00Z`).toLocaleDateString(
      currentLocale(),
      { day: "2-digit", month: "short" },
    );
    item.append(bars, label);
    timeline.append(item);
  });
}

function renderUsageRequests(rows = []) {
  const body = document.querySelector("#usage-request-rows");
  const empty = document.querySelector("#usage-requests-empty");
  if (!body || !empty) return;
  body.replaceChildren();
  empty.hidden = rows.length !== 0;
  rows.forEach((row) => {
    const tableRow = document.createElement("tr");
    const date = document.createElement("td");
    date.textContent = new Date(row.created_at).toLocaleString(currentLocale(), {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
    const api = document.createElement("td");
    const apiName = document.createElement("strong");
    apiName.textContent = row.api_name;
    const apiSlug = document.createElement("small");
    apiSlug.textContent = row.api_slug || `key #${row.api_key_id}`;
    api.append(apiName, apiSlug);
    const route = document.createElement("td");
    const provider = document.createElement("strong");
    provider.textContent = usageProviderName(row.routed_provider);
    const model = document.createElement("small");
    model.textContent = row.resolved_model;
    route.append(provider, model);
    const tokens = document.createElement("td");
    tokens.className = "usage-token-cell";
    tokens.textContent = `${formatUsageNumber(row.total_tokens)}`;
    tokens.title = (
      `${formatUsageNumber(row.prompt_tokens)} input · ` +
      `${formatUsageNumber(row.completion_tokens)} output`
    );
    const latency = document.createElement("td");
    latency.textContent = formatUsageLatency(row.latency_ms);
    const status = document.createElement("td");
    const pill = document.createElement("span");
    const succeeded = Number(row.status_code) >= 200 &&
      Number(row.status_code) < 400;
    pill.className = `usage-status-pill${succeeded ? "" : " error"}`;
    pill.textContent = succeeded ? String(row.status_code) : (
      row.error_code || String(row.status_code)
    );
    status.append(pill);
    tableRow.append(date, api, route, tokens, latency, status);
    body.append(tableRow);
  });
}

function renderUsage(data) {
  const summary = data.summary || {};
  const requests = Number(summary.request_count) || 0;
  const successful = Number(summary.successful_requests) || 0;
  const failed = Number(summary.failed_requests) || 0;
  const successRate = requests ? Math.round((successful / requests) * 100) : 0;
  const errorRate = requests ? Math.round((failed / requests) * 100) : 0;
  setText("#usage-request-count", formatUsageNumber(requests));
  setText(
    "#usage-success-rate",
    tr("usage.successful", { percent: successRate }),
  );
  setText("#usage-total-tokens", formatUsageNumber(summary.total_tokens));
  setText(
    "#usage-token-split",
    tr("usage.tokenSplit", {
      input: formatUsageNumber(summary.prompt_tokens),
      output: formatUsageNumber(summary.completion_tokens),
    }),
  );
  setText(
    "#usage-average-latency",
    formatUsageLatency(summary.average_latency_ms),
  );
  setText("#usage-failed-count", formatUsageNumber(failed));
  setText(
    "#usage-error-rate",
    tr("usage.errorRate", { percent: errorRate }),
  );
  setText(
    "#usage-timeline-total",
    tr("usage.tokenCount", {
      count: formatUsageNumber(summary.total_tokens),
    }),
  );
  const navCount = document.querySelector(".nav-usage-count");
  if (navCount) navCount.textContent = formatUsageNumber(requests);
  populateUsageFilters(data.filters);
  renderUsageTimeline(data.daily || []);
  renderUsageRanking(
    "#usage-api-ranking",
    data.by_api || [],
    (row) => row.api_name,
    (row) => (
      tr("usage.requestCount", {
        count: formatUsageNumber(row.request_count),
      }) + (row.api_slug ? ` · ${row.api_slug}` : "")
    ),
  );
  renderUsageRanking(
    "#usage-provider-ranking",
    data.by_provider || [],
    (row) => usageProviderName(row.provider),
    (row) => (
      tr("usage.requestCountWithLatency", {
        count: formatUsageNumber(row.request_count),
        latency: formatUsageLatency(row.average_latency_ms),
      })
    ),
  );
  renderUsageRequests(data.requests || []);
}

async function loadUsage(force = false) {
  loadProviderQuotas(force);
  if (usageState.loading || (usageState.loaded && !force)) return;
  usageState.loading = true;
  if (usageRefreshButton) usageRefreshButton.disabled = true;
  setUsageStatus(tr("usage.refreshing"));
  const query = new URLSearchParams({
    period: usagePeriod?.value || "7d",
    limit: "75",
  });
  if (usageApiFilter?.value) {
    query.set("api_key_id", usageApiFilter.value);
  }
  if (usageProviderFilter?.value) {
    query.set("provider", usageProviderFilter.value);
  }
  try {
    usageState.data = await dashboardRequest(`/api/usage?${query}`);
    usageState.loaded = true;
    renderUsage(usageState.data);
    const count = Number(usageState.data.summary?.request_count) || 0;
    setUsageStatus(
      tr(count === 1 ? "usage.requestFound" : "usage.requestsFound", {
        count: formatUsageNumber(count),
      }),
    );
  } catch (error) {
    setUsageStatus(error.message, "error");
  } finally {
    usageState.loading = false;
    if (usageRefreshButton) usageRefreshButton.disabled = false;
  }
}

[usagePeriod, usageApiFilter, usageProviderFilter].forEach((field) => {
  field?.addEventListener("change", () => {
    usageState.loaded = false;
    loadUsage(true);
  });
});
usageRefreshButton?.addEventListener("click", () => {
  usageState.loaded = false;
  providerQuotaState.loaded = false;
  loadUsage(true);
});

window.addEventListener("omniproxy:languagechange", () => {
  resetGatewayCopyButton();
  Object.entries(providerState).forEach(([provider, state]) => {
    if (state.details) updateProviderCard(provider, state.details);
  });
  if (activeProvider && dialog?.open) {
    setText("#dialog-auth", tr(`provider.${activeProvider}.auth`));
    setText(
      "#dialog-note",
      tr(providerConfig[activeProvider].noteKey),
    );
    renderProviderDialog(activeProvider);
  }
  renderProviderFilters();
  renderModelCatalog();
  renderManagedApis();
  if (usageState.data) renderUsage(usageState.data);
  if (providerQuotaState.data) {
    renderProviderQuotas(providerQuotaState.data);
  }
  if (apiDialog?.open) {
    const editing = Boolean(apiConfigId?.value);
    setText(
      "#api-dialog-title",
      tr(editing ? "apiDialog.edit" : "apiDialog.new"),
    );
    if (apiSave) {
      apiSave.textContent = tr(
        editing ? "apiDialog.save" : "apiDialog.create",
      );
    }
    renderApiRoutingPreview();
  }
});

const buildState = {
  projects: [],
  active: null,
  loaded: false,
  newFolderHandle: null,
  activeFolderHandle: null,
  newFolderName: "",
  newFiles: [],
  modelReviewRequired: false,
  configurationDirty: false,
  handoffBusy: false,
  activityPollTimer: null,
  activityPending: false,
  maintenanceJobId: null,
  maintenanceProjectId: null,
  pendingChanges: new Map(),
  appliedChangeMessages: new Set(),
  autopilotProjects: new Set(),
  autopilotApplying: new Set(),
  autopilotAttemptedMessages: new Set(),
};

const buildDialog = document.querySelector("#build-project-dialog");
const buildForm = document.querySelector("#build-project-form");
const buildDialogFeedback = document.querySelector("#build-dialog-feedback");
const buildIdea = document.querySelector("#build-idea");
const buildAnalystMode = document.querySelector("#build-analyst-mode");
const buildNewAnalystMode = document.querySelector(
  "#build-new-analyst-mode",
);
const buildFeedback = document.querySelector("#build-feedback");
const buildChatLane = document.querySelector("#build-chat-lane");
const buildChatForm = document.querySelector("#build-chat-form");
const buildChatInput = document.querySelector("#build-chat-input");
const buildHandoffButton = document.querySelector("#build-handoff");
const buildRestartChainButton = document.querySelector(
  "#build-restart-chain",
);
const buildDockerUpdateButton = document.querySelector(
  "#build-docker-update",
);
const buildAutopilotButton = document.querySelector("#build-autopilot");
const buildAgentActivity = document.querySelector("#build-agent-activity");
const buildAgentActivityTitle = document.querySelector(
  "#build-agent-activity-title",
);
const buildAgentActivityMessage = document.querySelector(
  "#build-agent-activity-message",
);
const buildAgentActivityTime = document.querySelector(
  "#build-agent-activity-time",
);

function mountBuildSidebarTools() {
  const target = document.querySelector("#build-sidebar-tools");
  const workspace = document.querySelector("#build-workspace");
  if (!target || !workspace) return;

  const sidebarSections = [
    ".build-workspace-header",
    ".build-config-grid",
    ".build-orchestrator-bar",
  ]
    .map((selector) => workspace.querySelector(selector))
    .filter(Boolean);
  if (sidebarSections.length === 0) return;

  const fragment = document.createDocumentFragment();
  sidebarSections.forEach((element) => fragment.append(element));
  target.append(fragment);
  target.dataset.mounted = "true";
}

mountBuildSidebarTools();

const BUILD_MAX_FILES = 2000;
const BUILD_MAX_FILE_BYTES = 1048576;
const BUILD_MAX_TOTAL_BYTES = 50000000;
const BUILD_AUTOPILOT_STORAGE_KEY = "omniproxy.build.autopilot.v1";
const BUILD_EXCLUDED_DIRECTORIES = new Set([
  ".git", ".idea", ".next", ".nuxt", ".pytest_cache", ".venv",
  ".vscode", "__pycache__", "build", "coverage", "dist", "node_modules",
  "target", "vendor",
]);

try {
  const storedAutopilot = JSON.parse(
    window.localStorage.getItem(BUILD_AUTOPILOT_STORAGE_KEY) || "[]",
  );
  if (Array.isArray(storedAutopilot)) {
    buildState.autopilotProjects = new Set(
      storedAutopilot.map((projectId) => String(projectId)),
    );
  }
} catch {
  // Autopilot resta disattivato se lo storage del browser non è disponibile.
}

function buildAutopilotEnabled(projectId = buildState.active?.id) {
  return projectId != null &&
    buildState.autopilotProjects.has(String(projectId));
}

function persistBuildAutopilot() {
  try {
    window.localStorage.setItem(
      BUILD_AUTOPILOT_STORAGE_KEY,
      JSON.stringify([...buildState.autopilotProjects]),
    );
  } catch {
    // Il permesso della cartella resta valido per la sessione corrente.
  }
}

function renderBuildAutopilotControl() {
  if (!buildAutopilotButton) return;
  const enabled = buildAutopilotEnabled();
  buildAutopilotButton.classList.toggle("active", enabled);
  buildAutopilotButton.setAttribute("aria-pressed", String(enabled));
  buildAutopilotButton.innerHTML = enabled
    ? '<span aria-hidden="true">●</span> Autopilot attivo'
    : '<span aria-hidden="true">●</span> Autopilot disattivo';
}

function setBuildFeedback(message = "", state = "") {
  if (!buildFeedback) return;
  buildFeedback.textContent = message;
  buildFeedback.className = `build-feedback${state ? ` ${state}` : ""}`;
}

function setBuildDialogFeedback(message = "", state = "") {
  if (!buildDialogFeedback) return;
  buildDialogFeedback.textContent = message;
  buildDialogFeedback.className =
    `auth-feedback${state ? ` ${state}` : ""}`;
}

function renderBuildAgentActivity(activity) {
  if (!buildAgentActivity || !activity?.active) return;
  buildAgentActivity.hidden = false;
  buildAgentActivity.dataset.role = activity.role || "builder";
  if (buildAgentActivityTitle) {
    buildAgentActivityTitle.textContent =
      activity.title || "Builder sta lavorando";
  }
  if (buildAgentActivityMessage) {
    buildAgentActivityMessage.textContent =
      activity.message || "Sta preparando la risposta.";
  }
  if (buildAgentActivityTime) {
    const startedAt = Number(activity.started_at) * 1000;
    const seconds = Number.isFinite(startedAt)
      ? Math.max(0, Math.floor((Date.now() - startedAt) / 1000))
      : 0;
    buildAgentActivityTime.textContent = `${seconds}s`;
  }
}

function stopBuildActivityPolling() {
  if (buildState.activityPollTimer) {
    window.clearInterval(buildState.activityPollTimer);
  }
  buildState.activityPollTimer = null;
  buildState.activityPending = false;
  if (buildAgentActivity) buildAgentActivity.hidden = true;
}

function startBuildActivityPolling(projectId, role, phase = "chat") {
  stopBuildActivityPolling();
  buildState.activityPending = true;
  const builder = role === "builder";
  renderBuildAgentActivity({
    active: true,
    role,
    phase,
    title: builder
      ? "Builder sta lavorando"
      : "Analista sta lavorando",
    message:
      builder && phase === "handoff"
        ? "Sta ricevendo il brief e preparando la risposta."
        : builder
          ? "Sta elaborando lo storico e il progetto."
          : "Sta analizzando la richiesta e preparando il brief.",
    started_at: Date.now() / 1000,
  });
  buildState.activityPollTimer = window.setInterval(async () => {
    try {
      const activity = await dashboardRequest(
        `/api/build/projects/${encodeURIComponent(projectId)}/activity`,
      );
      if (activity.active) renderBuildAgentActivity(activity);
    } catch {
      // Il feedback locale resta visibile finché la richiesta principale vive.
    }
  }, 650);
}

function updateBuildPlanAvailability() {
  const button = document.querySelector("#run-build-plan");
  if (button) button.disabled = buildState.modelReviewRequired;
}

function buildRoleElements(prefix) {
  return {
    provider: document.querySelector(`#build-${prefix}-provider`),
    model: document.querySelector(`#build-${prefix}-model`),
    reasoning: document.querySelector(`#build-${prefix}-reasoning`),
  };
}

function availableBuildProviders() {
  return catalogState.providers.filter(
    (provider) => provider.connected && provider.models.length > 0,
  );
}

function selectedBuildModel(prefix) {
  const fields = buildRoleElements(prefix);
  return catalogProvider(fields.provider?.value)?.models.find(
    (model) => model.id === fields.model?.value,
  );
}

function buildSelectionAvailable(selection) {
  const provider = catalogProvider(selection?.provider);
  return Boolean(
    provider?.connected &&
    provider.models.some(
      (model) =>
        model.id === selection?.model &&
        model.reasoning_efforts.includes(selection?.reasoning_effort),
    ),
  );
}

function sameBuildSelection(first, second) {
  return (
    first?.provider === second?.provider &&
    first?.model === second?.model &&
    first?.reasoning_effort === second?.reasoning_effort
  );
}

function buildRoleLabel(prefix) {
  return prefix === "analyst" ? "Analista idea" : "Builder";
}

async function refreshLiveBuildSelections() {
  const previous = {
    analyst: readBuildRole("analyst"),
    builder: readBuildRole("builder"),
  };
  await loadModelCatalog(true);
  populateBuildRole("analyst", previous.analyst);
  populateBuildRole("builder", previous.builder);
  const changedRoles = ["analyst", "builder"].filter(
    (role) => !sameBuildSelection(previous[role], readBuildRole(role)),
  );
  if (changedRoles.length > 0) {
    buildState.modelReviewRequired = true;
    updateBuildPlanAvailability();
    setBuildFeedback(
      `${changedRoles.map(buildRoleLabel).join(" e ")} usa un provider o un modello non più disponibile. Verifica la nuova selezione e salvala prima di continuare.`,
      "error",
    );
    return false;
  }
  return true;
}

function populateBuildReasoning(prefix, selectedReasoning = null) {
  const fields = buildRoleElements(prefix);
  if (!fields.reasoning) return;
  fields.reasoning.replaceChildren();
  const model = selectedBuildModel(prefix);
  model?.reasoning_efforts.forEach((effort) => {
    fields.reasoning.append(option(effort, effort));
  });
  if (selectedReasoning) fields.reasoning.value = selectedReasoning;
  if (!fields.reasoning.value && model) {
    fields.reasoning.value = model.default_reasoning_effort;
  }
}

function populateBuildModels(
  prefix,
  selectedModelId = null,
  selectedReasoning = null,
) {
  const fields = buildRoleElements(prefix);
  if (!fields.model || !fields.provider) return;
  fields.model.replaceChildren();
  const provider = catalogProvider(fields.provider.value);
  provider?.models.forEach((model) => {
    fields.model.append(option(model.id, model.display_name));
  });
  if (selectedModelId) fields.model.value = selectedModelId;
  if (!fields.model.value) {
    const preferred =
      provider?.models.find((model) => model.is_default) || provider?.models[0];
    if (preferred) fields.model.value = preferred.id;
  }
  populateBuildReasoning(prefix, selectedReasoning);
}

function populateBuildRole(prefix, selection = null) {
  const fields = buildRoleElements(prefix);
  if (!fields.provider) return;
  fields.provider.replaceChildren();
  availableBuildProviders().forEach((provider) => {
    fields.provider.append(option(provider.id, provider.name));
  });
  if (selection?.provider) fields.provider.value = selection.provider;
  if (!fields.provider.value) {
    const codex = availableBuildProviders().find(
      (provider) => provider.id === "codex",
    );
    fields.provider.value =
      codex?.id || availableBuildProviders()[0]?.id || "";
  }
  populateBuildModels(
    prefix,
    selection?.model || null,
    selection?.reasoning_effort || null,
  );
}

function readBuildRole(prefix) {
  const fields = buildRoleElements(prefix);
  return {
    provider: fields.provider?.value || "",
    model: fields.model?.value || "",
    reasoning_effort: fields.reasoning?.value || "",
  };
}

["analyst", "builder", "new-analyst", "new-builder"].forEach((prefix) => {
  const fields = buildRoleElements(prefix);
  fields.provider?.addEventListener("change", () => {
    if (prefix === "analyst" || prefix === "builder") {
      buildState.configurationDirty = true;
    }
    populateBuildModels(prefix);
  });
  fields.model?.addEventListener("change", () => {
    if (prefix === "analyst" || prefix === "builder") {
      buildState.configurationDirty = true;
    }
    populateBuildReasoning(prefix);
  });
  fields.reasoning?.addEventListener("change", () => {
    if (prefix === "analyst" || prefix === "builder") {
      buildState.configurationDirty = true;
    }
  });
});

buildAnalystMode?.addEventListener("change", () => {
  buildState.configurationDirty = true;
});

function renderBuildProjects() {
  const list = document.querySelector("#build-project-list");
  const total = document.querySelector(".build-project-total");
  const navCount = document.querySelector(".nav-build-count");
  if (!list) return;
  list.replaceChildren();
  if (total) total.textContent = String(buildState.projects.length);
  if (navCount) navCount.textContent = String(buildState.projects.length);

  buildState.projects.forEach((project) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `build-project-item${
      buildState.active?.id === project.id ? " active" : ""
    }`;
    const name = document.createElement("strong");
    name.textContent = project.name;
    const meta = document.createElement("span");
    const folder = document.createElement("b");
    folder.textContent = project.folder_name || "Senza cartella";
    const files = document.createElement("b");
    files.textContent = `${project.file_count} file`;
    meta.append(folder, files);
    button.append(name, meta);
    button.addEventListener("click", () => openBuildProject(project.id));
    list.append(button);
  });
}

function renderBuildArtifacts(artifacts = []) {
  const byType = Object.fromEntries(
    artifacts.map((artifact) => [artifact.artifact_type, artifact]),
  );
  document.querySelectorAll("[data-build-artifact]").forEach((card) => {
    const output = card.querySelector("pre");
    const artifact = byType[card.dataset.buildArtifact];
    if (!output) return;
    output.textContent =
      artifact?.content ||
      {
        analysis: "Nessuna analisi generata.",
        builder_brief: "Nessun brief generato.",
        roadmap: "Nessuna roadmap generata.",
        future_features: "Nessun suggerimento generato.",
      }[card.dataset.buildArtifact];
  });
}

function renderBuildPhases(phases = []) {
  const list = document.querySelector("#build-phase-list");
  const progress = document.querySelector("#build-phase-progress");
  if (!list || !progress) return;
  list.replaceChildren();
  if (!Array.isArray(phases) || phases.length === 0) {
    progress.textContent = "Nessuna fase";
    if (buildRestartChainButton) {
      buildRestartChainButton.hidden = true;
      delete buildRestartChainButton.dataset.phaseId;
    }
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "La checklist verrà creata dall’Analista.";
    list.append(empty);
    return;
  }
  const completed = phases.filter(
    (phase) => phase.status === "completed",
  ).length;
  const blockedPhase = phases.find((phase) => phase.status === "blocked");
  if (buildRestartChainButton) {
    buildRestartChainButton.hidden = !blockedPhase;
    buildRestartChainButton.dataset.phaseId = blockedPhase
      ? String(blockedPhase.id)
      : "";
  }
  progress.textContent = `${completed} / ${phases.length} completate`;
  const statusLabels = {
    pending: "In attesa",
    running: "Builder al lavoro",
    awaiting_apply: "Da applicare",
    completed: "Completata",
    blocked: "In pausa",
  };
  phases.forEach((phase) => {
    const item = document.createElement("li");
    item.className = `status-${phase.status || "pending"}`;
    const marker = document.createElement("span");
    marker.className = "build-phase-marker";
    marker.textContent =
      phase.status === "completed" ? "✓" : String(phase.position).padStart(2, "0");
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = phase.title;
    const instruction = document.createElement("p");
    instruction.textContent = phase.instruction;
    if (phase.error) {
      const error = document.createElement("small");
      error.textContent = phase.error;
      copy.append(title, instruction, error);
    } else {
      copy.append(title, instruction);
    }
    const state = document.createElement("b");
    state.textContent = statusLabels[phase.status] || phase.status;
    item.append(marker, copy, state);
    list.append(item);
  });
}

function builderDisplayContent(content) {
  return String(content || "")
    .replace(
      /<omniproxy-changes>\s*\{[\s\S]*?\}\s*<\/omniproxy-changes>/g,
      "",
    )
    .replace(
      /<omniproxy-plan>\s*\{[\s\S]*?\}\s*<\/omniproxy-plan>/g,
      "",
    )
    .replace(
      /<omniproxy-commands>\s*\{[\s\S]*?\}\s*<\/omniproxy-commands>/g,
      "",
    )
    .trim();
}

function rememberBuilderChanges(result) {
  const message = result?.builder_message || result;
  const changes = Array.isArray(result?.changes) ? result.changes : [];
  if (!message?.id || changes.length === 0) return 0;
  buildState.pendingChanges.set(String(message.id), changes);
  return changes.length;
}

function builderChangeProposal(message) {
  const messageId = String(message.id);
  const changes = buildState.pendingChanges.get(messageId);
  if (!Array.isArray(changes) || changes.length === 0) return null;

  const panel = document.createElement("div");
  panel.className = "build-change-proposal";
  panel.dataset.changeMessageId = messageId;
  const heading = document.createElement("strong");
  heading.textContent = buildState.appliedChangeMessages.has(messageId)
    ? "Modifiche applicate alla cartella"
    : `${changes.length} ${
        changes.length === 1 ? "modifica pronta" : "modifiche pronte"
      }`;
  const paths = document.createElement("ul");
  changes.forEach((change) => {
    const item = document.createElement("li");
    const operation =
      change.operation === "create" ? "Nuovo" : "Modifica";
    item.textContent = `${operation} · ${change.path}`;
    paths.append(item);
  });
  const apply = document.createElement("button");
  apply.type = "button";
  apply.textContent = buildState.appliedChangeMessages.has(messageId)
    ? "Applicate ✓"
    : "Verifica e applica";
  apply.disabled = buildState.appliedChangeMessages.has(messageId);
  apply.addEventListener("click", () => {
    applyBuilderChanges(messageId, changes, apply);
  });
  panel.append(heading, paths, apply);
  return panel;
}

function maybeAutoApplyBuilderChanges(messageId, changes) {
  const id = String(messageId);
  if (
    !buildAutopilotEnabled() ||
    buildState.appliedChangeMessages.has(id) ||
    buildState.autopilotApplying.has(id) ||
    buildState.autopilotAttemptedMessages.has(id)
  ) {
    return;
  }
  buildState.autopilotAttemptedMessages.add(id);
  const button = document.querySelector(
    `.build-change-proposal[data-change-message-id="${CSS.escape(id)}"] button`,
  );
  applyBuilderChanges(id, changes, button, { autopilot: true });
}

function renderBuildMessages() {
  const container = document.querySelector("#build-chat-messages");
  if (!container) return;
  container.replaceChildren();
  const lane = buildChatLane?.value || "analyst";
  updateBuildHandoffControl();
  const messages = (buildState.active?.messages || []).filter(
    (message) => message.lane === lane,
  );
  if (messages.length === 0) {
    const placeholder = document.createElement("p");
    placeholder.className = "build-chat-placeholder";
    placeholder.textContent =
      lane === "analyst"
        ? "Parla con l’Analista per chiarire idea, requisiti e roadmap."
        : "Il Builder riceverà qui le consegne inviate dall’Analista.";
    container.append(placeholder);
    return;
  }
  messages.forEach((message) => {
    const article = document.createElement("article");
    const isHandoff = message.message_type === "handoff";
    article.className =
      `build-chat-message ${message.role}${isHandoff ? " handoff" : ""}`;
    const label = document.createElement("small");
    label.textContent =
      isHandoff
        ? "Consegna Analista → Builder"
        : message.role === "user"
        ? "Tu"
        : lane === "analyst"
          ? "Analista idea"
          : "Builder";
    const copy = document.createElement("p");
    copy.textContent =
      builderDisplayContent(message.content) ||
      "Il Builder ha preparato una proposta di modifica.";
    article.append(label, copy);
    if (
      lane === "builder" &&
      message.role === "assistant"
    ) {
      const proposal = builderChangeProposal(message);
      if (proposal) article.append(proposal);
    }
    container.append(article);
  });
  container.scrollTop = container.scrollHeight;
  window.requestAnimationFrame(() => {
    container.scrollTop = container.scrollHeight;
  });
}

function updateBuildHandoffControl() {
  if (!buildHandoffButton) return;
  const project = buildState.active;
  const analystLane = (buildChatLane?.value || "analyst") === "analyst";
  buildHandoffButton.hidden = !analystLane;
  if (!project || !analystLane) return;
  const phases = Array.isArray(project.phases) ? project.phases : [];
  if (phases.length > 0) {
    const running = phases.find((phase) => phase.status === "running");
    const awaiting = phases.find(
      (phase) => phase.status === "awaiting_apply",
    );
    const blocked = phases.find((phase) => phase.status === "blocked");
    const pending = phases.find((phase) => phase.status === "pending");
    const next = blocked || pending;
    buildHandoffButton.disabled =
      buildState.handoffBusy || Boolean(running || awaiting) || !next;
    buildHandoffButton.firstChild.textContent = buildState.handoffBusy
      ? "Avvio in corso "
      : running
        ? `Fase ${running.position} in esecuzione `
        : awaiting
          ? `Applica la fase ${awaiting.position} `
          : blocked
            ? `Riprova la fase ${blocked.position} `
            : pending
              ? `Avvia la fase ${pending.position} `
              : "Catena completata ";
    return;
  }
  const messages = project.messages || [];
  const latestAnalyst = [...messages].reverse().find(
    (message) =>
      message.lane === "analyst" && message.role === "assistant",
  );
  const hasBrief = (project.artifacts || []).some(
    (artifact) => artifact.artifact_type === "builder_brief",
  );
  const deliveredMessage = latestAnalyst
    ? messages.find(
      (message) =>
        message.lane === "builder" &&
        message.message_type === "handoff" &&
        message.source_message_id === latestAnalyst.id,
    )
    : null;
  const deliveredAnswer = deliveredMessage
    ? [...messages].reverse().find(
      (message) =>
        message.lane === "builder" &&
        message.role === "assistant" &&
        Number(message.id) > Number(deliveredMessage.id),
    )
    : null;
  const legacyReadOnlyAnswer = Boolean(
    deliveredAnswer &&
    /(indicizzata in sola lettura|non dichiaro modifiche applicate)/i.test(
      deliveredAnswer.content,
    ),
  );
  const delivered = Boolean(deliveredMessage && !legacyReadOnlyAnswer);
  buildHandoffButton.disabled =
    buildState.handoffBusy || delivered || (!latestAnalyst && !hasBrief);
  buildHandoffButton.firstChild.textContent = delivered
    ? "Consegnato al Builder "
    : buildState.handoffBusy
      ? "Consegna in corso "
      : legacyReadOnlyAnswer
        ? "Riavvia il Builder "
      : "Passa al Builder ";
}

function renderBuildWorkspace() {
  const empty = document.querySelector("#build-empty");
  const workspace = document.querySelector("#build-workspace");
  const sidebarTools = document.querySelector("#build-sidebar-tools");
  const container = document.querySelector("#workspaceContainer");
  const chatPanel = document.querySelector("#chatPanel");
  const chatResizer = document.querySelector("#resizer2");
  const project = buildState.active;
  if (!empty || !workspace) return;
  empty.hidden = Boolean(project);
  workspace.hidden = !project;
  if (sidebarTools) sidebarTools.hidden = !project;
  if (container) container.classList.toggle("build-project-empty", !project);
  if (chatPanel) chatPanel.hidden = !project;
  if (chatResizer) chatResizer.hidden = !project;
  renderBuildProjects();
  renderBuildAutopilotControl();
  if (!project) return;

  setText("#build-project-title", project.name);
  setText(
    "#build-folder-name",
    project.folder_name || "Nessuna cartella collegata",
  );
  setText("#build-file-count", `${project.file_count} file indicizzati`);
  if (buildDockerUpdateButton) {
    buildDockerUpdateButton.hidden =
      project.folder_name !== "OmniProxy AI";
  }
  if (buildIdea) buildIdea.value = project.idea || "";
  if (buildAnalystMode) {
    buildAnalystMode.value = project.analyst_mode || "detailed";
  }
  const unavailableRoles = ["analyst", "builder"].filter(
    (role) => !buildSelectionAvailable(project[role]),
  );
  populateBuildRole("analyst", project.analyst);
  populateBuildRole("builder", project.builder);
  buildState.configurationDirty = false;
  buildState.modelReviewRequired = unavailableRoles.length > 0;
  updateBuildPlanAvailability();
  if (unavailableRoles.length > 0) {
    setBuildFeedback(
      `${unavailableRoles.map(buildRoleLabel).join(" e ")} usa una configurazione non più disponibile. Scegli il modello mostrato oppure un altro modello e salva la configurazione.`,
      "error",
    );
  } else {
    setBuildFeedback();
  }
  renderBuildPhases(project.phases);
  renderBuildArtifacts(project.artifacts);
  renderBuildMessages();
}

async function openBuildProject(projectId) {
  try {
    await loadModelCatalog();
    buildState.active = await dashboardRequest(
      `/api/build/projects/${encodeURIComponent(projectId)}`,
    );
    buildState.activeFolderHandle = null;
    renderBuildWorkspace();
    const folderHandleReady = readBuildFolderHandle(projectId).then((handle) => {
      if (buildState.active?.id === projectId) {
        buildState.activeFolderHandle = handle;
      }
      return handle;
    });
    dashboardRequest(
      `/api/build/projects/${encodeURIComponent(projectId)}/builder-proposal`,
    ).then(async (proposal) => {
      if (buildState.active?.id !== projectId || !proposal?.message_id) return;
      const proposalId = String(proposal.message_id);
      if (buildState.appliedChangeMessages.has(proposalId)) return;
      const changeCount = rememberBuilderChanges({
        builder_message: { id: proposal.message_id },
        changes: proposal.changes,
      });
      if (changeCount > 0 && buildChatLane) {
        buildChatLane.value = "builder";
      }
      renderBuildMessages();
      if (changeCount > 0) {
        await folderHandleReady;
        window.requestAnimationFrame(() => {
          maybeAutoApplyBuilderChanges(
            proposal.message_id,
            proposal.changes,
          );
        });
      }
      if (proposal.change_error) {
        setBuildFeedback(proposal.change_error, "error");
      }
    }).catch(() => {
      // La chat resta utilizzabile anche se la proposta non è rileggibile.
    });
  } catch (error) {
    setBuildFeedback(error.message, "error");
  }
}

async function loadBuildProjects(force = false) {
  if (buildState.loaded && !force) return;
  try {
    const result = await dashboardRequest("/api/build/projects");
    buildState.projects = Array.isArray(result.data) ? result.data : [];
    buildState.loaded = true;
    renderBuildProjects();
    const activeId = buildState.active?.id;
    const next = buildState.projects.find(
      (project) => project.id === activeId,
    ) || buildState.projects[0];
    if (next) {
      await openBuildProject(next.id);
    } else {
      buildState.active = null;
      renderBuildWorkspace();
    }
  } catch (error) {
    setBuildFeedback(error.message, "error");
  }
}

function isAllowedBuildFile(path) {
  const parts = path.replaceAll("\\", "/").split("/");
  const lowerParts = parts.map((part) => part.toLowerCase());
  if (
    lowerParts.slice(0, -1).some(
      (part) => BUILD_EXCLUDED_DIRECTORIES.has(part),
    )
  ) {
    return false;
  }
  const name = lowerParts.at(-1);
  const extensionIndex = name.lastIndexOf(".");
  const extension =
    extensionIndex > 0 ? name.slice(extensionIndex).toLowerCase() : "";
  if (
    name === ".env" ||
    name.startsWith(".env.") ||
    [".npmrc", ".pypirc", "auth.json", "credentials.json",
      "service-account.json"].includes(name) ||
    [".key", ".p12", ".pem", ".pfx"].includes(extension)
  ) {
    return false;
  }
  return true;
}

async function isBuildTextFile(file) {
  const sample = new Uint8Array(
    await file.slice(0, Math.min(file.size, 16384)).arrayBuffer(),
  );
  if (sample.includes(0)) return false;
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(sample);
    return true;
  } catch {
    return false;
  }
}

async function collectBuildFilesFromHandle(directoryHandle) {
  const files = [];
  let totalBytes = 0;
  let skipped = 0;

  async function walk(handle, prefix = "", depth = 0) {
    if (depth > 12) return;
    const entries = [];
    for await (const entry of handle.values()) entries.push(entry);
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.kind === "directory") {
        if (BUILD_EXCLUDED_DIRECTORIES.has(entry.name.toLowerCase())) {
          skipped += 1;
          continue;
        }
        await walk(entry, relativePath, depth + 1);
        continue;
      }
      if (!isAllowedBuildFile(relativePath)) {
        skipped += 1;
        continue;
      }
      if (files.length >= BUILD_MAX_FILES) {
        skipped += 1;
        continue;
      }
      const file = await entry.getFile();
      if (file.size > BUILD_MAX_FILE_BYTES) {
        skipped += 1;
        continue;
      }
      if (!(await isBuildTextFile(file))) {
        skipped += 1;
        continue;
      }
      const content = await file.text();
      const bytes = new TextEncoder().encode(content).length;
      if (totalBytes + bytes > BUILD_MAX_TOTAL_BYTES) {
        skipped += 1;
        continue;
      }
      totalBytes += bytes;
      files.push({ path: relativePath, content });
    }
  }
  await walk(directoryHandle);
  return {
    folderName: directoryHandle.name,
    files,
    skipped,
    totalBytes,
  };
}

async function collectBuildFilesFromInput(fileList) {
  const files = [];
  let totalBytes = 0;
  let skipped = 0;
  let folderName = "";
  for (const file of [...fileList].sort(
    (a, b) => a.webkitRelativePath.localeCompare(b.webkitRelativePath),
  )) {
    const fullPath = file.webkitRelativePath || file.name;
    const parts = fullPath.split("/");
    if (!folderName && parts.length > 1) folderName = parts[0];
    const relativePath = parts.length > 1 ? parts.slice(1).join("/") : fullPath;
    if (
      !isAllowedBuildFile(relativePath) ||
      file.size > BUILD_MAX_FILE_BYTES ||
      files.length >= BUILD_MAX_FILES
    ) {
      skipped += 1;
      continue;
    }
    if (!(await isBuildTextFile(file))) {
      skipped += 1;
      continue;
    }
    const content = await file.text();
    const bytes = new TextEncoder().encode(content).length;
    if (totalBytes + bytes > BUILD_MAX_TOTAL_BYTES) {
      skipped += 1;
      continue;
    }
    totalBytes += bytes;
    files.push({ path: relativePath, content });
  }
  return {
    folderName: folderName || "cartella-progetto",
    files,
    skipped,
    totalBytes,
  };
}

function openBuildHandleDatabase() {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) {
      reject(new Error("IndexedDB non disponibile."));
      return;
    }
    const request = indexedDB.open("omniproxy-build", 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains("folders")) {
        request.result.createObjectStore("folders");
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function storeBuildFolderHandle(projectId, handle) {
  if (!handle) return;
  try {
    const database = await openBuildHandleDatabase();
    await new Promise((resolve, reject) => {
      const transaction = database.transaction("folders", "readwrite");
      transaction.objectStore("folders").put(handle, String(projectId));
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
    });
    database.close();
  } catch {
    // Lo snapshot resta utilizzabile anche se il browser non persiste handle.
  }
}

async function readBuildFolderHandle(projectId) {
  try {
    const database = await openBuildHandleDatabase();
    const handle = await new Promise((resolve, reject) => {
      const request = database
        .transaction("folders", "readonly")
        .objectStore("folders")
        .get(String(projectId));
      request.onsuccess = () => resolve(request.result || null);
      request.onerror = () => reject(request.error);
    });
    database.close();
    return handle;
  } catch {
    return null;
  }
}

async function sha256BuildText(value) {
  const bytes = new TextEncoder().encode(value);
  const digest = await window.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function buildPathParts(path) {
  const clean = String(path || "").replaceAll("\\", "/");
  const parts = clean.split("/");
  if (
    clean.startsWith("/") ||
    parts.some((part) => !part || part === "." || part === "..") ||
    !isAllowedBuildFile(clean)
  ) {
    throw new Error(`Percorso Builder non consentito: ${path}`);
  }
  return parts;
}

async function readBuildDiskFile(root, path) {
  const parts = buildPathParts(path);
  const name = parts.pop();
  let directory = root;
  try {
    for (const part of parts) {
      directory = await directory.getDirectoryHandle(part);
    }
    const handle = await directory.getFileHandle(name);
    const file = await handle.getFile();
    return {
      directory,
      name,
      content: await file.text(),
    };
  } catch (error) {
    if (error?.name === "NotFoundError") return null;
    throw error;
  }
}

async function writeBuildDiskFile(root, path, content) {
  const parts = buildPathParts(path);
  const name = parts.pop();
  let directory = root;
  for (const part of parts) {
    directory = await directory.getDirectoryHandle(part, { create: true });
  }
  const handle = await directory.getFileHandle(name, { create: true });
  const writable = await handle.createWritable();
  try {
    await writable.write(content);
  } finally {
    await writable.close();
  }
  return { directory, name };
}

async function writableBuildFolder(
  project,
  { interactive = true } = {},
) {
  let handle = buildState.activeFolderHandle;
  if (handle) {
    let permission = await handle.queryPermission({ mode: "readwrite" });
    if (permission !== "granted" && interactive) {
      permission = await handle.requestPermission({ mode: "readwrite" });
    }
    if (permission !== "granted") handle = null;
  }
  if (!handle) {
    if (!interactive) {
      const error = new Error(
        "L’accesso Autopilot alla cartella deve essere riattivato con un clic.",
      );
      error.code = "build_write_permission_required";
      throw error;
    }
    if (!("showDirectoryPicker" in window)) {
      throw new Error(
        "Questo browser non supporta la scrittura sicura della cartella. Usa Chrome o Edge e ricollegala.",
      );
    }
    handle = await window.showDirectoryPicker({ mode: "readwrite" });
  }
  if (project.folder_name && handle.name !== project.folder_name) {
    throw new Error(
      `Hai selezionato “${handle.name}”; la cartella attesa è “${project.folder_name}”.`,
    );
  }
  buildState.activeFolderHandle = handle;
  await storeBuildFolderHandle(project.id, handle);
  return handle;
}

async function applyBuilderChanges(
  messageId,
  changes,
  button,
  { autopilot = false } = {},
) {
  const project = buildState.active;
  if (!project || !Array.isArray(changes) || changes.length === 0) return;
  const id = String(messageId);
  const paths = changes.map((change) => change.path);
  if (!autopilot) {
    const approved = window.confirm(
      `OmniProxy scriverà esclusivamente questi file in “${project.folder_name}”:\n\n${paths.join("\n")}\n\nContinuare?`,
    );
    if (!approved) return;
  }

  buildState.autopilotApplying.add(id);
  if (button) {
    button.disabled = true;
    button.textContent = autopilot
      ? "Autopilot in esecuzione…"
      : "Verifica snapshot…";
  }
  setBuildFeedback(
    autopilot
      ? "Autopilot Builder: verifica e applicazione automatica…"
      : "Verifica sicura dei file prima della scrittura…",
  );
  const backups = [];
  const writtenPaths = new Set();
  let chainResult = null;
  try {
    const root = await writableBuildFolder(project, {
      interactive: !autopilot,
    });
    for (const change of changes) {
      if (
        typeof change.content !== "string" ||
        !change.path ||
        !["update", "create"].includes(change.operation)
      ) {
        throw new Error("La proposta Builder contiene dati non validi.");
      }
      const current = await readBuildDiskFile(root, change.path);
      if (change.operation === "create" && current) {
        throw new Error(
          `${change.path} esiste già: sincronizza la cartella e rigenera la proposta.`,
        );
      }
      if (change.operation === "update") {
        if (!current) {
          throw new Error(
            `${change.path} non esiste più: sincronizza la cartella e riprova.`,
          );
        }
        const currentHash = await sha256BuildText(current.content);
        if (currentHash !== change.base_sha256) {
          throw new Error(
            `${change.path} è cambiato dopo l’analisi. Sincronizza la cartella e chiedi al Builder una nuova patch.`,
          );
        }
      }
      const resultHash = await sha256BuildText(change.content);
      if (resultHash !== change.result_sha256) {
        throw new Error(`Controllo integrità fallito per ${change.path}.`);
      }
      backups.push({
        path: change.path,
        existed: Boolean(current),
        content: current?.content || "",
      });
    }

    if (button) button.textContent = "Applicazione…";
    for (const change of changes) {
      await writeBuildDiskFile(root, change.path, change.content);
      writtenPaths.add(change.path);
    }
    const snapshot = await collectBuildFilesFromHandle(root);
    await dashboardRequest(
      `/api/build/projects/${encodeURIComponent(project.id)}/files`,
      {
        method: "PUT",
        body: JSON.stringify({
          folder_name: snapshot.folderName,
          files: snapshot.files,
        }),
      },
    );
    buildState.appliedChangeMessages.add(id);
    buildState.pendingChanges.delete(id);
    try {
      chainResult = await dashboardRequest(
        `/api/build/projects/${encodeURIComponent(project.id)}/builder-proposals/${encodeURIComponent(id)}/applied`,
        { method: "POST" },
      );
    } catch {
      // I file sono già verificati e sincronizzati: non eseguire rollback
      // soltanto perché non è stato possibile salvare lo stato visuale.
    }
    const nextPhaseResult = chainResult?.next_phase;
    const nextChangeCount = rememberBuilderChanges(nextPhaseResult);
    buildState.loaded = false;
    await openBuildProject(project.id);
    if (chainResult?.command_job?.status === "queued") {
      setBuildFeedback(
        "Catena completata. Aggiornamento dei container avviato dal runner locale.",
        "success",
      );
    } else if (
      ["rejected", "unavailable"].includes(chainResult?.command_job?.status)
    ) {
      setBuildFeedback(chainResult.command_job.message, "error");
    } else if (nextPhaseResult?.status === "blocked") {
      setBuildFeedback(
        nextPhaseResult.change_error ||
          nextPhaseResult.message ||
          "La fase successiva è in pausa.",
        "error",
      );
    } else if (nextChangeCount > 0) {
      setBuildFeedback(
        `Fase completata. La successiva ha preparato ${nextChangeCount} ${
          nextChangeCount === 1 ? "modifica" : "modifiche"
        } ed è ora da applicare.`,
        "success",
      );
    } else if (nextPhaseResult?.status === "chain_completed") {
      setBuildFeedback(
        "Catena completata: tutte le fasi sono state applicate.",
        "success",
      );
    } else {
      setBuildFeedback(
        `${changes.length} ${
          changes.length === 1 ? "file aggiornato" : "file aggiornati"
        } e snapshot sincronizzato.`,
        "success",
      );
    }
  } catch (error) {
    if (writtenPaths.size > 0 && buildState.activeFolderHandle) {
      for (const backup of [...backups].reverse()) {
        if (!writtenPaths.has(backup.path)) continue;
        try {
          if (backup.existed) {
            await writeBuildDiskFile(
              buildState.activeFolderHandle,
              backup.path,
              backup.content,
            );
          } else {
            const created = await readBuildDiskFile(
              buildState.activeFolderHandle,
              backup.path,
            );
            if (created) await created.directory.removeEntry(created.name);
          }
        } catch {
          // Prosegue il rollback degli altri file.
        }
      }
    }
    if (error?.code === "build_write_permission_required") {
      buildState.autopilotProjects.delete(String(project.id));
      persistBuildAutopilot();
      renderBuildAutopilotControl();
    }
    setBuildFeedback(error.message || "Modifiche non applicate.", "error");
    if (button) {
      button.disabled = false;
      button.textContent = "Verifica e applica";
    }
  } finally {
    buildState.autopilotApplying.delete(id);
  }
}

async function removeBuildFolderHandle(projectId) {
  try {
    const database = await openBuildHandleDatabase();
    await new Promise((resolve, reject) => {
      const transaction = database.transaction("folders", "readwrite");
      transaction.objectStore("folders").delete(String(projectId));
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
    });
    database.close();
  } catch {
    // Nessun handle browser da rimuovere.
  }
}

function describeBuildSnapshot(snapshot) {
  const size = new Intl.NumberFormat("it-IT", {
    maximumFractionDigits: 1,
  }).format(snapshot.totalBytes / 1024);
  return `${snapshot.folderName} · ${snapshot.files.length} file · ${size} KB${
    snapshot.skipped ? ` · ${snapshot.skipped} esclusi` : ""
  }`;
}

function applyNewBuildSnapshot(snapshot, handle = null) {
  buildState.newFolderHandle = handle;
  buildState.newFolderName = snapshot.folderName;
  buildState.newFiles = snapshot.files;
  setText("#build-picked-folder", describeBuildSnapshot(snapshot));
}

async function pickNewBuildFolder() {
  setBuildDialogFeedback("Lettura sicura della cartella…");
  try {
    if ("showDirectoryPicker" in window) {
      const handle = await window.showDirectoryPicker({
        mode: "readwrite",
      });
      const snapshot = await collectBuildFilesFromHandle(handle);
      applyNewBuildSnapshot(snapshot, handle);
    } else {
      document.querySelector("#build-folder-fallback")?.click();
    }
    setBuildDialogFeedback();
  } catch (error) {
    if (error?.name !== "AbortError") {
      setBuildDialogFeedback(
        error.message || "Impossibile leggere la cartella.",
        "error",
      );
    } else {
      setBuildDialogFeedback();
    }
  }
}

async function openBuildProjectDialog() {
  await loadModelCatalog();
  buildState.newFolderHandle = null;
  buildState.newFolderName = "";
  buildState.newFiles = [];
  buildForm?.reset();
  setText("#build-picked-folder", "Puoi anche iniziare senza cartella.");
  setBuildDialogFeedback();
  populateBuildRole("new-analyst");
  populateBuildRole("new-builder");
  buildDialog?.showModal();
  window.setTimeout(
    () => document.querySelector("#build-new-name")?.focus(),
    40,
  );
}

document.querySelector("#pick-build-folder")?.addEventListener(
  "click",
  pickNewBuildFolder,
);
document.querySelector("#build-folder-fallback")?.addEventListener(
  "change",
  async (event) => {
    try {
      const snapshot = await collectBuildFilesFromInput(event.target.files);
      applyNewBuildSnapshot(snapshot);
      setBuildDialogFeedback();
    } catch (error) {
      setBuildDialogFeedback(error.message, "error");
    }
  },
);
document.querySelector("#new-build-project")?.addEventListener(
  "click",
  openBuildProjectDialog,
);
document.querySelectorAll("[data-new-build-project]").forEach((button) => {
  button.addEventListener("click", openBuildProjectDialog);
});
document.querySelectorAll("[data-build-dialog-close]").forEach((button) => {
  button.addEventListener("click", () => buildDialog?.close());
});
buildDialog?.addEventListener("click", (event) => {
  if (event.target === buildDialog) buildDialog.close();
});

buildForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = document.querySelector("#create-build-project");
  submit.disabled = true;
  setBuildDialogFeedback("Creazione workspace sicuro…");
  try {
    const project = await dashboardRequest("/api/build/projects", {
      method: "POST",
      body: JSON.stringify({
        name: document.querySelector("#build-new-name").value.trim(),
        folder_name: buildState.newFolderName,
        idea: document.querySelector("#build-new-idea").value.trim(),
        analyst_mode: buildNewAnalystMode?.value || "schematic",
        analyst: readBuildRole("new-analyst"),
        builder: readBuildRole("new-builder"),
        files: buildState.newFiles,
      }),
    });
    await storeBuildFolderHandle(project.id, buildState.newFolderHandle);
    if (buildState.newFolderHandle) {
      buildState.autopilotProjects.add(String(project.id));
      persistBuildAutopilot();
    }
    buildDialog.close();
    buildState.loaded = false;
    await loadBuildProjects(true);
    await openBuildProject(project.id);
  } catch (error) {
    setBuildDialogFeedback(error.message, "error");
  } finally {
    submit.disabled = false;
  }
});

async function saveActiveBuildProject({
  quiet = false,
  skipCatalogRefresh = false,
} = {}) {
  const project = buildState.active;
  if (!project) return null;
  if (!quiet) setBuildFeedback("Salvataggio configurazione…");
  try {
    if (
      !skipCatalogRefresh &&
      !(await refreshLiveBuildSelections())
    ) {
      return null;
    }
    const updated = await dashboardRequest(
      `/api/build/projects/${encodeURIComponent(project.id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          name: project.name,
          folder_name: project.folder_name,
          idea: buildIdea?.value.trim() || "",
          analyst_mode: buildAnalystMode?.value || "detailed",
          analyst: readBuildRole("analyst"),
          builder: readBuildRole("builder"),
          files: [],
        }),
      },
    );
    buildState.active = {
      ...buildState.active,
      ...updated,
      files: buildState.active.files,
      artifacts: buildState.active.artifacts,
      messages: buildState.active.messages,
      phases: buildState.active.phases,
    };
    buildState.modelReviewRequired = false;
    buildState.configurationDirty = false;
    updateBuildPlanAvailability();
    buildState.loaded = false;
    if (!quiet) setBuildFeedback("Configurazione salvata.", "success");
    return updated;
  } catch (error) {
    setBuildFeedback(error.message, "error");
    throw error;
  }
}

document.querySelector("#save-build-project")?.addEventListener(
  "click",
  async () => {
    try {
      await saveActiveBuildProject();
    } catch {
      // Il messaggio contestuale è già mostrato nella barra Build.
    }
  },
);

buildAutopilotButton?.addEventListener("click", async () => {
  const project = buildState.active;
  if (!project) return;
  const projectId = String(project.id);
  if (buildAutopilotEnabled(project.id)) {
    buildState.autopilotProjects.delete(projectId);
    persistBuildAutopilot();
    renderBuildAutopilotControl();
    setBuildFeedback(
      "Autopilot disattivato: le prossime modifiche richiederanno conferma.",
      "success",
    );
    return;
  }

  buildAutopilotButton.disabled = true;
  setBuildFeedback(
    "Autorizza una volta la cartella: poi il Builder scriverà automaticamente.",
  );
  try {
    const handle = await writableBuildFolder(project, { interactive: true });
    const snapshot = await collectBuildFilesFromHandle(handle);
    await dashboardRequest(
      `/api/build/projects/${encodeURIComponent(project.id)}/files`,
      {
        method: "PUT",
        body: JSON.stringify({
          folder_name: snapshot.folderName,
          files: snapshot.files,
        }),
      },
    );
    buildState.autopilotProjects.add(projectId);
    persistBuildAutopilot();
    buildState.pendingChanges.clear();
    buildState.autopilotAttemptedMessages.clear();
    buildState.loaded = false;
    await loadBuildProjects(true);
    await openBuildProject(project.id);
    renderBuildAutopilotControl();
    setBuildFeedback(
      `Autopilot attivo e ${snapshot.files.length} file sincronizzati. Le prossime patch valide saranno applicate senza conferme.`,
      "success",
    );
  } catch (error) {
    if (error?.name !== "AbortError") {
      setBuildFeedback(error.message, "error");
    }
  } finally {
    buildAutopilotButton.disabled = false;
  }
});

document.querySelector("#sync-build-folder")?.addEventListener(
  "click",
  async () => {
    const project = buildState.active;
    if (!project) return;
    setBuildFeedback("Richiesta accesso alla cartella…");
    try {
      let handle = await readBuildFolderHandle(project.id);
      if (
        handle &&
        (await handle.queryPermission({ mode: "readwrite" })) !== "granted"
      ) {
        const permission = await handle.requestPermission({
          mode: "readwrite",
        });
        if (permission !== "granted") handle = null;
      }
      if (!handle) {
        if (!("showDirectoryPicker" in window)) {
          throw new Error(
            "Il browser richiede di ricollegare la cartella dal nuovo progetto.",
          );
        }
        handle = await window.showDirectoryPicker({ mode: "readwrite" });
      }
      const snapshot = await collectBuildFilesFromHandle(handle);
      await dashboardRequest(
        `/api/build/projects/${encodeURIComponent(project.id)}/files`,
        {
          method: "PUT",
          body: JSON.stringify({
            folder_name: snapshot.folderName,
            files: snapshot.files,
          }),
        },
      );
      await storeBuildFolderHandle(project.id, handle);
      buildState.activeFolderHandle = handle;
      setBuildFeedback(
        `Sincronizzati ${snapshot.files.length} file; ${snapshot.skipped} esclusi.`,
        "success",
      );
      buildState.loaded = false;
      await loadBuildProjects(true);
      await openBuildProject(project.id);
    } catch (error) {
      if (error?.name !== "AbortError") {
        setBuildFeedback(error.message, "error");
      } else {
        setBuildFeedback();
      }
    }
  },
);

function renderDockerUpdateState(status = "idle") {
  if (!buildDockerUpdateButton) return;
  buildDockerUpdateButton.classList.toggle(
    "is-running",
    ["queued", "running", "reconnecting"].includes(status),
  );
  buildDockerUpdateButton.classList.toggle(
    "is-complete",
    status === "completed",
  );
  buildDockerUpdateButton.disabled =
    ["queued", "running", "reconnecting"].includes(status);
  buildDockerUpdateButton.textContent = {
    queued: "Docker in coda…",
    running: "Aggiornamento Docker…",
    reconnecting: "Riavvio gateway…",
    completed: "Docker aggiornato ✓",
    failed: "Riprova Docker",
  }[status] || "Aggiorna Docker";
}

async function pollDockerUpdate(projectId, jobId) {
  const deadline = Date.now() + 20 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
    if (
      buildState.maintenanceJobId !== jobId ||
      buildState.maintenanceProjectId !== projectId
    ) {
      return;
    }
    let job;
    try {
      job = await dashboardRequest(
        `/api/build/projects/${encodeURIComponent(projectId)}/maintenance/jobs/${encodeURIComponent(jobId)}`,
      );
    } catch {
      renderDockerUpdateState("reconnecting");
      continue;
    }
    if (job.status === "queued" || job.status === "running") {
      renderDockerUpdateState(job.status);
      continue;
    }
    buildState.maintenanceJobId = null;
    buildState.maintenanceProjectId = null;
    if (job.status === "completed" && Number(job.return_code) === 0) {
      renderDockerUpdateState("completed");
      setBuildFeedback(
        "Docker aggiornato e container verificati. Ricarico l’interfaccia…",
        "success",
      );
      window.setTimeout(() => window.location.reload(), 1400);
      return;
    }
    renderDockerUpdateState("failed");
    setBuildFeedback(
      "L’aggiornamento Docker non è riuscito. Puoi riprovare dal pulsante.",
      "error",
    );
    return;
  }
  buildState.maintenanceJobId = null;
  buildState.maintenanceProjectId = null;
  renderDockerUpdateState("failed");
  setBuildFeedback(
    "Timeout durante l’aggiornamento Docker. Controlla lo stato e riprova.",
    "error",
  );
}

buildDockerUpdateButton?.addEventListener("click", async () => {
  const project = buildState.active;
  if (!project || buildState.maintenanceJobId) return;
  renderDockerUpdateState("queued");
  setBuildFeedback(
    "Avvio rebuild sicuro dei container OmniProxy…",
  );
  try {
    const job = await dashboardRequest(
      `/api/build/projects/${encodeURIComponent(project.id)}/maintenance/rebuild`,
      { method: "POST" },
    );
    if (!job?.id || job.status !== "queued") {
      throw new Error(
        job?.message || "Il runner Docker non ha accettato l’operazione.",
      );
    }
    buildState.maintenanceJobId = String(job.id);
    buildState.maintenanceProjectId = project.id;
    pollDockerUpdate(project.id, String(job.id));
  } catch (error) {
    buildState.maintenanceJobId = null;
    buildState.maintenanceProjectId = null;
    renderDockerUpdateState("failed");
    setBuildFeedback(error.message, "error");
  }
});

document.querySelector("#delete-build-project")?.addEventListener(
  "click",
  async () => {
    const project = buildState.active;
    if (!project) return;
    if (!window.confirm(
      `Eliminare “${project.name}”? Piano e chat verranno rimossi.`,
    )) {
      return;
    }
    try {
      await dashboardRequest(
        `/api/build/projects/${encodeURIComponent(project.id)}`,
        { method: "DELETE" },
      );
      await removeBuildFolderHandle(project.id);
      buildState.activeFolderHandle = null;
      buildState.active = null;
      buildState.loaded = false;
      await loadBuildProjects(true);
    } catch (error) {
      setBuildFeedback(error.message, "error");
    }
  },
);

document.querySelector("#run-build-plan")?.addEventListener(
  "click",
  async () => {
    const project = buildState.active;
    const idea = buildIdea?.value.trim() || "";
    if (!project) return;
    if (idea.length < 10) {
      setBuildFeedback(
        "Descrivi l’idea con almeno 10 caratteri prima di avviare la pipeline.",
        "error",
      );
      buildIdea?.focus();
      return;
    }
    if (buildState.modelReviewRequired) {
      setBuildFeedback(
        "La configurazione salvata usa un modello non più disponibile. Verifica le selezioni e premi “Salva configurazione” prima di avviare la pipeline.",
        "error",
      );
      return;
    }
    const orchestrator = document.querySelector(".build-orchestrator-bar");
    orchestrator?.classList.add("is-busy");
    try {
      if (!(await refreshLiveBuildSelections())) return;
      setBuildFeedback(
        "Pipeline attiva: comprensione dell’idea… può richiedere alcuni minuti.",
      );
      const saved = await saveActiveBuildProject({
        quiet: true,
        skipCatalogRefresh: true,
      });
      if (!saved) return;
      const result = await dashboardRequest(
        `/api/build/projects/${encodeURIComponent(project.id)}/plan`,
        {
          method: "POST",
          body: JSON.stringify({ idea }),
        },
      );
      buildState.active = {
        ...buildState.active,
        ...result.project,
        artifacts: result.artifacts,
        phases: result.phases,
      };
      renderBuildWorkspace();
      setBuildFeedback(
        "Piano completato: brief, fasi e migliorie sono stati salvati.",
        "success",
      );
      buildState.loaded = false;
    } catch (error) {
      let selectionsStillAvailable = true;
      try {
        selectionsStillAvailable = await refreshLiveBuildSelections();
      } catch {
        // Conserva l'errore originale se il catalogo non è raggiungibile.
      }
      if (selectionsStillAvailable) {
        setBuildFeedback(error.message, "error");
      }
    } finally {
      orchestrator?.classList.remove("is-busy");
    }
  },
);

buildChatLane?.addEventListener("change", () => {
  renderBuildMessages();
  if (buildChatInput) {
    buildChatInput.placeholder =
      buildChatLane.value === "analyst"
        ? "Chiarisci l’idea oppure scrivi “passa al Builder” per consegnare automaticamente…"
        : "Chiedi al Builder di affrontare una fase specifica…";
  }
});

buildRestartChainButton?.addEventListener("click", async () => {
  const project = buildState.active;
  const blocked = (project?.phases || []).find(
    (phase) => phase.status === "blocked",
  );
  if (!project || !blocked || buildState.handoffBusy) return;
  if (buildState.modelReviewRequired || buildState.configurationDirty) {
    setBuildFeedback(
      "Salva la configurazione dei modelli prima di riavviare la catena.",
      "error",
    );
    return;
  }
  buildState.handoffBusy = true;
  buildRestartChainButton.disabled = true;
  setBuildFeedback(
    `L’Analista sta rivalutando la fase ${blocked.position}…`,
  );
  startBuildActivityPolling(project.id, "analyst", "restart");
  try {
    const result = await dashboardRequest(
      `/api/build/projects/${encodeURIComponent(project.id)}/resume`,
      {
        method: "POST",
      },
    );
    const handoff = result.handoff;
    const changeCount = rememberBuilderChanges(handoff);
    if (
      handoff?.status === "completed" ||
      handoff?.status === "already_completed"
    ) {
      buildChatLane.value = "builder";
    }
    await openBuildProject(project.id);
    if (handoff?.status === "failed" || handoff?.status === "blocked") {
      setBuildFeedback(
        handoff.message ||
          handoff.change_error ||
          "Il riavvio della catena non è riuscito.",
        "error",
      );
    } else if (changeCount > 0) {
      setBuildFeedback(
        `Fase ${blocked.position} ripresa: il Builder ha preparato la nuova patch.`,
        "success",
      );
    } else {
      setBuildFeedback(
        `La fase ${blocked.position} è stata riconsegnata al Builder.`,
        "success",
      );
    }
  } catch (error) {
    await openBuildProject(project.id);
    setBuildFeedback(error.message, "error");
  } finally {
    stopBuildActivityPolling();
    buildState.handoffBusy = false;
    buildRestartChainButton.disabled = false;
    updateBuildHandoffControl();
  }
});

buildHandoffButton?.addEventListener("click", async () => {
  const project = buildState.active;
  if (!project || buildState.handoffBusy) return;
  if (buildState.modelReviewRequired || buildState.configurationDirty) {
    setBuildFeedback(
      "Salva la configurazione dei modelli prima della consegna.",
      "error",
    );
    return;
  }
  try {
    if (!(await refreshLiveBuildSelections())) return;
  } catch (error) {
    setBuildFeedback(error.message, "error");
    return;
  }

  buildState.handoffBusy = true;
  updateBuildHandoffControl();
  if (buildChatInput) buildChatInput.disabled = true;
  setBuildFeedback("Consegna strutturata al Builder in corso…");
  startBuildActivityPolling(project.id, "builder", "handoff");
  try {
    const result = await dashboardRequest(
      `/api/build/projects/${encodeURIComponent(project.id)}/handoff`,
      {
        method: "POST",
        body: JSON.stringify({
          instruction: buildChatInput?.value.trim() || "",
        }),
      },
    );
    if (buildChatInput) buildChatInput.value = "";
    if (buildChatLane) buildChatLane.value = "builder";
    const changeCount = rememberBuilderChanges(result);
    await openBuildProject(project.id);
    if (result.change_error) {
      setBuildFeedback(result.change_error, "error");
    } else if (changeCount > 0) {
      setBuildFeedback(
        buildAutopilotEnabled(project.id)
          ? `Il Builder ha preparato ${changeCount} ${
              changeCount === 1 ? "modifica" : "modifiche"
            }: Autopilot le sta verificando e applicando.`
          : `Il Builder ha preparato ${changeCount} ${
              changeCount === 1 ? "modifica" : "modifiche"
            }. Verifica i file nella chat e premi “Verifica e applica”.`,
        "success",
      );
    } else {
      setBuildFeedback(
        result.status === "already_completed"
          ? "Questa consegna era già stata ricevuta dal Builder."
          : "Consegna ricevuta: il Builder ha risposto nella sua chat.",
        "success",
      );
    }
  } catch (error) {
    await openBuildProject(project.id);
    setBuildFeedback(error.message, "error");
  } finally {
    stopBuildActivityPolling();
    buildState.handoffBusy = false;
    if (buildChatInput) buildChatInput.disabled = false;
    updateBuildHandoffControl();
  }
});

buildChatForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const project = buildState.active;
  const message = buildChatInput?.value.trim() || "";
  if (!project || !message) return;
  if (buildState.modelReviewRequired || buildState.configurationDirty) {
    setBuildFeedback(
      "Salva la configurazione dei modelli prima di continuare la chat.",
      "error",
    );
    return;
  }
  try {
    if (!(await refreshLiveBuildSelections())) return;
  } catch (error) {
    setBuildFeedback(error.message, "error");
    return;
  }
  const submit = buildChatForm.querySelector("button[type='submit']");
  submit.disabled = true;
  buildChatInput.disabled = true;
  setBuildFeedback(
    buildChatLane.value === "analyst"
      ? "L’Analista sta ragionando…"
      : "Il Builder sta preparando la risposta…",
  );
  startBuildActivityPolling(
    project.id,
    buildChatLane.value,
    "chat",
  );
  const requestedLane = buildChatLane.value;
  try {
    const result = await dashboardRequest(
      `/api/build/projects/${encodeURIComponent(project.id)}/chat`,
      {
        method: "POST",
        body: JSON.stringify({
          lane: buildChatLane.value,
          message,
        }),
      },
    );
    buildChatInput.value = "";
    const handoff = result.handoff;
    const changeResult = handoff || (
      requestedLane === "builder" ? result : null
    );
    const changeCount = rememberBuilderChanges(changeResult);
    if (
      handoff?.status === "completed" ||
      handoff?.status === "already_completed"
    ) {
      buildChatLane.value = "builder";
    }
    await openBuildProject(project.id);
    if (handoff?.status === "failed") {
      setBuildFeedback(
        `L’Analista ha risposto, ma la consegna è fallita: ${handoff.message}`,
        "error",
      );
    } else if (changeResult?.change_error) {
      setBuildFeedback(changeResult.change_error, "error");
    } else if (changeCount > 0) {
      setBuildFeedback(
        buildAutopilotEnabled(project.id)
          ? `Il Builder ha preparato ${changeCount} ${
              changeCount === 1 ? "modifica" : "modifiche"
            }: Autopilot le sta verificando e applicando.`
          : `Il Builder ha preparato ${changeCount} ${
              changeCount === 1 ? "modifica" : "modifiche"
            }. Verifica i file nella chat e premi “Verifica e applica”.`,
        "success",
      );
    } else if (handoff) {
      setBuildFeedback(
        "L’Analista ha consegnato il brief e il Builder ha risposto.",
        "success",
      );
    } else {
      setBuildFeedback();
    }
  } catch (error) {
    await openBuildProject(project.id);
    let selectionsStillAvailable = !buildState.modelReviewRequired;
    if (selectionsStillAvailable) {
      try {
        selectionsStillAvailable = await refreshLiveBuildSelections();
      } catch {
        // Conserva l'errore originale se il catalogo non è raggiungibile.
      }
    }
    if (selectionsStillAvailable) {
      setBuildFeedback(error.message, "error");
    }
  } finally {
    stopBuildActivityPolling();
    submit.disabled = false;
    buildChatInput.disabled = false;
    buildChatInput.focus();
  }
});

const baseUrl = document.querySelector("#gateway-base-url");
if (baseUrl) baseUrl.textContent = MANAGED_API_BASE_URL;
loadModelCatalog();
loadManagedApis();
loadUsage();

const initialView = window.location.hash.slice(1);
const publicViews = ["connections", "models", "apis", "usage"];
if (BUILD_ENABLED) publicViews.push("build");
if (publicViews.includes(initialView)) {
  switchView(initialView);
} else if (initialView === "new-api") {
  switchView("apis");
  openApiConfigDialog();
}
window.addEventListener("hashchange", () => {
  const view = window.location.hash.slice(1);
  if (publicViews.includes(view)) {
    switchView(view);
  }
});

(function initBuildWorkspaceLayout() {
  const container = document.querySelector("#workspaceContainer");
  const resizerOne = document.querySelector("#resizer1");
  const resizerTwo = document.querySelector("#resizer2");
  const toggle = document.querySelector("#toggleSidebarBtn");
  const show = document.querySelector("#showSidebarBtn");
  if (!container || !resizerOne || !resizerTwo || !toggle || !show) return;

  const layoutKey = "workspace-layout-cols";
  const collapsedKey = "workspace-sidebar-collapsed";
  const defaults = { sidebarWidth: 280, chatWidth: 400 };
  const clamp = (value, minimum, maximum) =>
    Math.min(maximum, Math.max(minimum, value));
  const safeWidth = (value, minimum, maximum, fallback) => {
    const number = Number(value);
    return Number.isFinite(number)
      ? clamp(number, minimum, maximum)
      : fallback;
  };

  function readLayout() {
    try {
      const saved = JSON.parse(window.localStorage.getItem(layoutKey) || "null");
      return {
        sidebarWidth: safeWidth(
          saved?.sidebarWidth,
          150,
          400,
          defaults.sidebarWidth,
        ),
        chatWidth: safeWidth(
          saved?.chatWidth,
          250,
          600,
          defaults.chatWidth,
        ),
      };
    } catch {
      return { ...defaults };
    }
  }

  function applyLayout(layout) {
    const next = {
      sidebarWidth: safeWidth(
        layout.sidebarWidth,
        150,
        400,
        defaults.sidebarWidth,
      ),
      chatWidth: safeWidth(
        layout.chatWidth,
        250,
        600,
        defaults.chatWidth,
      ),
    };
    container.style.setProperty(
      "--build-sidebar-width",
      `${next.sidebarWidth}px`,
    );
    container.style.setProperty(
      "--build-chat-width",
      `${next.chatWidth}px`,
    );
    resizerOne.setAttribute(
      "aria-valuenow",
      String(Math.round(next.sidebarWidth)),
    );
    resizerTwo.setAttribute(
      "aria-valuenow",
      String(Math.round(next.chatWidth)),
    );
    return next;
  }

  function currentLayout() {
    const style = window.getComputedStyle(container);
    return {
      sidebarWidth: safeWidth(
        Number.parseFloat(style.getPropertyValue("--build-sidebar-width")),
        150,
        400,
        defaults.sidebarWidth,
      ),
      chatWidth: safeWidth(
        Number.parseFloat(style.getPropertyValue("--build-chat-width")),
        250,
        600,
        defaults.chatWidth,
      ),
    };
  }

  function saveLayout(layout) {
    try {
      window.localStorage.setItem(layoutKey, JSON.stringify(layout));
    } catch {
      // Il layout resta valido per la sessione corrente.
    }
  }

  function setCollapsed(collapsed, persist = true) {
    container.classList.toggle("sidebar-collapsed", collapsed);
    show.classList.toggle("hidden", !collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    show.setAttribute("aria-expanded", String(!collapsed));
    resizerOne.tabIndex = collapsed ? -1 : 0;
    resizerOne.setAttribute("aria-hidden", String(collapsed));
    if (!persist) return;
    try {
      window.localStorage.setItem(collapsedKey, String(collapsed));
    } catch {
      // Il collasso resta valido per la sessione corrente.
    }
  }

  function startResize(side, event) {
    if (event.button !== 0 || event.isPrimary === false) return;
    if (
      side === "sidebar" &&
      container.classList.contains("sidebar-collapsed")
    ) {
      return;
    }
    event.preventDefault();
    const startX = event.clientX;
    const start = currentLayout();
    let current = start;
    const resizer = side === "sidebar" ? resizerOne : resizerTwo;
    const pointerId = event.pointerId;
    let finished = false;
    resizer.setPointerCapture(pointerId);
    document.body.classList.add("resizing-active");
    resizer.classList.add("resizing");

    function move(moveEvent) {
      if (moveEvent.pointerId !== pointerId) return;
      const delta = moveEvent.clientX - startX;
      current = { ...start };
      if (side === "sidebar") {
        current.sidebarWidth = clamp(
          start.sidebarWidth + delta,
          150,
          400,
        );
      } else {
        current.chatWidth = clamp(start.chatWidth - delta, 250, 600);
      }
      current = applyLayout(current);
    }

    function stop(stopEvent) {
      if (
        finished ||
        (stopEvent?.pointerId != null &&
          stopEvent.pointerId !== pointerId)
      ) {
        return;
      }
      finished = true;
      document.body.classList.remove("resizing-active");
      resizer.classList.remove("resizing");
      resizer.removeEventListener("pointermove", move);
      resizer.removeEventListener("pointerup", stop);
      resizer.removeEventListener("pointercancel", stop);
      resizer.removeEventListener("lostpointercapture", stop);
      if (resizer.hasPointerCapture(pointerId)) {
        resizer.releasePointerCapture(pointerId);
      }
      saveLayout(current);
    }

    resizer.addEventListener("pointermove", move);
    resizer.addEventListener("pointerup", stop);
    resizer.addEventListener("pointercancel", stop);
    resizer.addEventListener("lostpointercapture", stop);
  }

  function resizeWithKeyboard(side, event) {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    if (
      side === "sidebar" &&
      container.classList.contains("sidebar-collapsed")
    ) {
      return;
    }
    event.preventDefault();
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const next = currentLayout();
    if (side === "sidebar") {
      next.sidebarWidth = clamp(
        next.sidebarWidth + direction * 10,
        150,
        400,
      );
    } else {
      next.chatWidth = clamp(next.chatWidth - direction * 10, 250, 600);
    }
    saveLayout(applyLayout(next));
  }

  resizerOne.addEventListener(
    "pointerdown",
    (event) => startResize("sidebar", event),
  );
  resizerTwo.addEventListener(
    "pointerdown",
    (event) => startResize("chat", event),
  );
  resizerOne.addEventListener(
    "keydown",
    (event) => resizeWithKeyboard("sidebar", event),
  );
  resizerTwo.addEventListener(
    "keydown",
    (event) => resizeWithKeyboard("chat", event),
  );
  toggle.addEventListener("click", () => setCollapsed(true));
  show.addEventListener("click", () => setCollapsed(false));

  applyLayout(readLayout());
  let collapsed = false;
  try {
    collapsed = window.localStorage.getItem(collapsedKey) === "true";
  } catch {
    collapsed = false;
  }
  setCollapsed(collapsed, false);
})();
