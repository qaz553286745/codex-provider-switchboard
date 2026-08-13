const state = {
  payload: null,
  config: null,
  kiroModels: [],
  cursorModels: [],
  cursorVariants: [],
  selectedCursorVariant: null,
  customModels: [],
  directModels: [],
  directPlatforms: [],
  directLoginId: null,
  directLoginTimer: null,
  busy: new Set(),
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const providerNames = {
  kiro: "Kiro",
  cursor: "Cursor",
  custom: "第三方",
  direct: "原生平台",
};
const stabilityNames = { stable: "稳定", beta: "Beta", experimental: "实验性" };

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("visible"), 3800);
}

function proxyBaseUrl() {
  return `${window.location.origin}/v1`;
}

async function requestJSON(url, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(url, { ...options, headers, credentials: "same-origin" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = payload?.error?.message || payload?.message || `请求失败 (${response.status})`;
    throw new Error(message);
  }
  return payload;
}

function paramsKey(params = []) {
  return JSON.stringify(
    [...params]
      .map((item) => ({ id: String(item.id), value: item.value }))
      .sort((a, b) => a.id.localeCompare(b.id)),
  );
}

function isMaximumVariant(variant) {
  const text = `${variant.label} ${paramsKey(variant.model_params)}`.toLowerCase();
  return /(^|[^a-z])(max|ultra|最高)([^a-z]|$)/.test(text);
}

function defaultCursorVariant() {
  return {
    key: "cursor-default",
    model_id: "",
    model_params: [],
    model_display_name: "Cursor 默认模型",
    label: "使用 Cursor 默认模型与参数",
    is_default: true,
  };
}

function buildCursorVariants(modelId) {
  if (!modelId) return [defaultCursorVariant()];
  const model = state.cursorModels.find((item) => item.id === modelId);
  if (!model) return [];
  const variants = Array.isArray(model.variants) && model.variants.length
    ? model.variants
    : [{ params: [], displayName: model.displayName, isDefault: true }];
  return variants.map((variant, index) => {
    const params = Array.isArray(variant.params) ? variant.params : [];
    const details = params.length
      ? params.map((item) => `${item.id}=${String(item.value)}`).join(" · ")
      : "默认参数";
    const displayName = model.displayName || model.id;
    return {
      key: `${model.id}:${index}:${paramsKey(params)}`,
      model_id: model.id,
      model_params: params,
      model_display_name: `${displayName} · ${details}`,
      label: details,
      is_default: variant.isDefault === true,
    };
  });
}

function renderParamList(container, params, emptyText) {
  container.replaceChildren();
  if (!params?.length) {
    const empty = document.createElement("span");
    empty.className = "empty-chip";
    empty.textContent = emptyText;
    container.append(empty);
    return;
  }
  params.forEach((param) => {
    const chip = document.createElement("span");
    chip.className = "param-chip";
    chip.textContent = `${param.id}=${String(param.value)}`;
    container.append(chip);
  });
}

function renderCursorVariantSelect(cursor, modelId) {
  const select = $("#cursor-variant");
  select.replaceChildren();
  state.cursorVariants = buildCursorVariants(modelId);
  if (!state.cursorVariants.length) {
    state.cursorVariants = [{
      key: `${modelId}:saved`,
      model_id: modelId,
      model_params: cursor.model_id === modelId ? cursor.model_params || [] : [],
      model_display_name: cursor.model_display_name || modelId,
      label: "已保存参数（模型目录暂不可用）",
      is_default: true,
    }];
  }
  state.cursorVariants.forEach((variant) => {
    const option = document.createElement("option");
    option.value = variant.key;
    option.textContent = `${variant.label}${variant.is_default ? " · 默认" : ""}${
      isMaximumVariant(variant) ? " · 最高 / MAX" : ""
    }`;
    select.append(option);
  });
  const configured = state.cursorVariants.find(
    (variant) => variant.model_id === (cursor.model_id || "")
      && paramsKey(variant.model_params) === paramsKey(cursor.model_params || []),
  );
  const selected = configured
    || state.cursorVariants.find((variant) => variant.is_default)
    || state.cursorVariants[0];
  state.selectedCursorVariant = selected;
  select.value = selected.key;
  select.disabled = false;
  renderParamList($("#cursor-params"), selected.model_params, "使用 Cursor 默认参数");
}

function renderCursorModels(cursor) {
  const select = $("#cursor-model");
  select.replaceChildren();
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = "Cursor 默认模型（不发送 model 字段）";
  select.append(defaultOption);
  state.cursorModels.forEach((model) => {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = model.displayName || model.id;
    select.append(option);
  });
  if (cursor.model_id && !state.cursorModels.some((model) => model.id === cursor.model_id)) {
    const option = document.createElement("option");
    option.value = cursor.model_id;
    option.textContent = cursor.model_display_name || cursor.model_id;
    select.append(option);
  }
  select.value = cursor.model_id || "";
  select.disabled = false;
  renderCursorVariantSelect(cursor, select.value);
}

function renderKiroModels(kiro) {
  const select = $("#kiro-model-select");
  select.replaceChildren();
  state.kiroModels.forEach((model) => {
    const option = document.createElement("option");
    option.value = model.id;
    const rate = model.rateMultiplier !== undefined ? ` · ${model.rateMultiplier}x` : "";
    option.textContent = `${model.displayName || model.id}${rate}`;
    option.title = model.description || "";
    select.append(option);
  });
  if (!state.kiroModels.some((model) => model.id === kiro.model_id)) {
    const option = document.createElement("option");
    option.value = kiro.model_id;
    option.textContent = `${kiro.model_id} · 当前保存`;
    select.append(option);
  }
  select.value = kiro.model_id;
  select.disabled = false;
}

function renderCustomModelList() {
  const datalist = $("#custom-model-list");
  datalist.replaceChildren();
  state.customModels.forEach((model) => {
    const option = document.createElement("option");
    option.value = model.id;
    option.label = model.displayName || model.id;
    datalist.append(option);
  });
}

function directPlatform(platformId = null) {
  const selected = platformId || $("#direct-platform")?.value || state.config?.direct?.platform_id;
  return state.directPlatforms.find((item) => item.id === selected) || null;
}

function directCredential(platformId) {
  return state.payload?.credentials?.providers?.[platformId] || {
    configured: false,
    source: "none",
    type: null,
  };
}

function renderDirectModels(direct, platformId = null) {
  const platform = directPlatform(platformId);
  if (!platform) return;
  const select = $("#direct-model");
  select.replaceChildren();
  const configuredModel = platform.id === direct.platform_id ? direct.model_id : "";
  const preferred = configuredModel || platform.default_model;
  const models = [...state.directModels];
  if (preferred && !models.some((model) => model.id === preferred)) {
    models.unshift({ id: preferred, displayName: `${preferred} · 默认` });
  }
  models.forEach((model) => {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = model.displayName || model.id;
    option.title = model.description || "";
    select.append(option);
  });
  select.value = preferred;
  select.disabled = models.length === 0;
}

function renderDirectControls(platformId) {
  const platform = directPlatform(platformId);
  if (!platform) return;
  const credential = directCredential(platform.id);
  const authModes = Array.isArray(platform.auth_modes) ? platform.auth_modes : [];
  const supportsKey = authModes.includes("api_key");
  const supportsOAuth = authModes.includes("oauth");
  const stability = stabilityNames[platform.stability] || platform.stability;
  const badge = $("#direct-platform-stability");
  badge.textContent = stability;
  badge.className = `status-badge ${platform.stability === "stable" ? "ok" : "warning"}`;
  $("#direct-platform-note").textContent = platform.note || "—";
  $("#direct-model-source").textContent = platform.models_path
    ? "来自平台官方模型目录"
    : "来自 Switchboard 受控兼容目录";
  $("#direct-key").disabled = !supportsKey;
  $("#save-direct-key").disabled = !supportsKey;
  $("#login-direct").disabled = !supportsOAuth;
  $("#logout-direct").disabled = !credential.configured;
  const importPiKiro = $("#import-pi-kiro");
  importPiKiro.hidden = platform.id !== "kiro_direct";
  importPiKiro.disabled = platform.id !== "kiro_direct" || state.busy.has("direct-import");
  $("#direct-key").placeholder = supportsKey
    ? (credential.configured ? "已配置（输入新值可替换）" : "输入所选平台的 API Key")
    : "该平台使用账号 OAuth";
  $("#direct-key-note").textContent = credential.configured
    ? `已配置 · ${credential.source.startsWith("env:") ? "环境变量" : "独立凭据文件"} · 明文永不回显`
    : (supportsKey ? "可保存到独立 0600 凭据文件；环境变量优先" : "请使用账号登录");
  $("#direct-kiro-options").hidden = platform.id !== "kiro_direct";
}

function renderDirectPlatformSelect(direct) {
  const select = $("#direct-platform");
  select.replaceChildren();
  state.directPlatforms.forEach((platform) => {
    const option = document.createElement("option");
    option.value = platform.id;
    option.textContent = `${platform.name} · ${stabilityNames[platform.stability] || platform.stability}`;
    select.append(option);
  });
  select.value = direct.platform_id;
  renderDirectControls(direct.platform_id);
  renderDirectModels(direct, direct.platform_id);
}

function renderDirectLogin(login) {
  const panel = $("#direct-login-progress");
  panel.hidden = false;
  const terminal = ["complete", "failed", "cancelled"].includes(login.status);
  const title = {
    starting: "正在启动登录…",
    waiting: "等待你完成认证",
    complete: "认证完成",
    failed: "认证失败",
    cancelled: "认证已取消",
  }[login.status] || "认证进行中";
  $("#direct-login-title").textContent = title;
  const event = login.event || {};
  const prompt = login.prompt || null;
  $("#direct-login-message").textContent = login.error
    || prompt?.message
    || event.instructions
    || (terminal ? "认证流程已结束。" : "请稍候。");
  const link = $("#direct-login-link");
  const url = event.verification_uri || event.url || "";
  link.hidden = !url;
  if (url) link.href = url;
  const code = $("#direct-login-code");
  code.hidden = !event.user_code;
  code.textContent = event.user_code || "";
  const input = $("#direct-login-input");
  const respond = $("#direct-login-respond");
  input.hidden = !prompt;
  respond.hidden = !prompt;
  if (prompt) input.placeholder = prompt.placeholder || "粘贴授权码或回调 URL";
  $("#direct-login-cancel").hidden = terminal;
}

function renderProvider(activeProvider) {
  $$('[data-provider-card]').forEach((card) => {
    card.classList.toggle("active", card.dataset.providerCard === activeProvider);
  });
  const input = $(`input[name="provider"][value="${activeProvider}"]`);
  if (input) input.checked = true;
}

function renderLastRequest(last) {
  const grid = $("#last-request");
  const values = last
    ? [
        ["厂家", providerNames[last.provider] || last.provider || "—"],
        ["动作", last.action || "—"],
        ["模型", last.model?.id || last.model || "默认模型"],
        ["会话复用", last.session_reused ? "是" : "否"],
      ]
    : [["厂家", "尚无请求"], ["动作", "—"], ["模型", "—"], ["会话复用", "—"]];
  grid.replaceChildren();
  values.forEach(([label, value]) => {
    const item = document.createElement("div");
    const caption = document.createElement("span");
    const strong = document.createElement("strong");
    caption.textContent = label;
    strong.textContent = value;
    strong.title = value;
    item.append(caption, strong);
    grid.append(item);
  });
  const params = last?.model?.params
    || (last?.effort ? [{ id: "--effort", value: last.effort }] : []);
  renderParamList($("#last-params"), params, last ? "未显式发送模型参数" : "等待首个请求");
}

function renderCodexConfig(config) {
  const badge = $("#codex-config-status");
  if (config.active && config.current_matches_managed) {
    badge.textContent = "代理接管中";
    badge.className = "status-badge ok";
  } else if (config.active) {
    badge.textContent = "检测到外部修改";
    badge.className = "status-badge warning";
  } else {
    badge.textContent = "未接管";
    badge.className = "status-badge";
  }
  $("#codex-config-path").textContent = config.config_path || "—";
  $("#codex-backup-path").textContent = config.backup_path || "尚无备份";
  if (!config.active) {
    $("#codex-config-integrity").textContent = config.restore_method === "external_detach"
      ? "已检测到代理路由在外部移除，状态已自动同步"
      : "当前未修改 Codex 配置";
  } else if (config.restore_strategy === "field_level") {
    $("#codex-config-integrity").textContent = config.current_matches_managed
      ? "备份存在；关闭时校验后仅恢复 Switchboard 管理的字段"
      : "受管字段有变化；关闭时仍按备份恢复，其他字段保持不变";
  } else {
    $("#codex-config-integrity").textContent = "备份不可用；仍可关闭，届时仅清理 Switchboard 路由值";
  }
  $("#enable-codex-config").disabled = config.active;
  $("#disable-codex-config").disabled = !config.active;
}

function setInputValue(selector, value) {
  $(selector).value = value ?? "";
}

function renderState(payload, { preserveForm = false } = {}) {
  state.payload = payload;
  state.config = payload.settings;
  const { cursor, custom, kiro, direct } = payload.settings;
  state.directPlatforms = Array.isArray(payload.direct_platforms)
    ? payload.direct_platforms
    : [];
  $("#version").textContent = `v${payload.version}`;
  $("#kiro-model").textContent = payload.providers.kiro.model;
  $("#kiro-concurrency").textContent = `${payload.providers.kiro.max_concurrency || 1} 个 CLI 进程`;
  $("#config-path").textContent = `Switchboard 配置：${payload.settings.config_path}`;
  $("#switchboard-log-path").textContent = payload.log_history?.path || "—";

  const kiroStatus = $("#kiro-status");
  kiroStatus.textContent = payload.providers.kiro.available ? "本机可用" : "未找到 CLI";
  kiroStatus.className = `status-badge ${payload.providers.kiro.available ? "ok" : "error"}`;

  const cursorProvider = payload.providers.cursor;
  const cursorStatus = $("#cursor-status");
  if (cursor.backend === "cli" && !cursorProvider.cli_available) {
    cursorStatus.textContent = "未找到 CLI";
  } else {
    cursorStatus.textContent = cursor.api_key_configured ? "可用" : "未配置";
  }
  cursorStatus.className = `status-badge ${cursorProvider.configured ? "ok" : "error"}`;
  $("#cursor-backend-summary").textContent = cursor.backend === "cloud_api"
    ? "Cloud Agents API"
    : "本地 Cursor CLI";
  $("#cursor-auth").textContent = cursor.api_key_configured
    ? `已配置 · ${cursor.api_key_source === "environment" ? "环境变量" : "本机文件"}`
    : "等待 API Key";
  $("#cursor-model-summary").textContent = cursor.model_display_name || cursor.model_id || "Cursor 默认模型";
  $("#cursor-concurrency").textContent = `${cursorProvider.max_concurrency || 1} 个 CLI 进程`;
  $("#key-note").textContent = cursor.api_key_configured
    ? "密钥已保存；留空不会覆盖，网页不会读回明文"
    : "只保存在本机配置文件，网页不会读回明文";
  $("#cursor-key").placeholder = cursor.api_key_configured ? "已保存（输入新值可替换）" : "粘贴 crsr_…";

  const customReady = custom.api_key_configured && custom.base_url;
  const customStatus = $("#custom-status");
  customStatus.textContent = customReady ? "接口已配置" : "未配置";
  customStatus.className = `status-badge ${customReady ? "ok" : "error"}`;
  $("#custom-base-summary").textContent = custom.base_url || "等待配置";
  $("#custom-model-summary").textContent = custom.model_display_name || custom.model_id || "第三方默认模型";
  $("#custom-auth").textContent = custom.api_key_configured
    ? `已配置 · ${custom.api_key_source === "environment" ? "环境变量" : "本机文件"}`
    : "等待 API Key";
  $("#custom-key-note").textContent = custom.api_key_configured
    ? "密钥已保存；留空不会覆盖，网页不会读回明文"
    : "也可通过 THIRD_PARTY_API_KEY 注入";
  $("#custom-key").placeholder = custom.api_key_configured ? "已保存（输入新值可替换）" : "粘贴第三方 API Key";

  const directProvider = payload.providers.direct;
  const selectedDirectPlatform = state.directPlatforms.find(
    (platform) => platform.id === direct.platform_id,
  );
  const selectedDirectCredential = directCredential(direct.platform_id);
  const directStatus = $("#direct-status");
  directStatus.textContent = directProvider.configured ? "已认证" : "未认证";
  directStatus.className = `status-badge ${directProvider.configured ? "ok" : "error"}`;
  $("#direct-platform-summary").textContent = selectedDirectPlatform?.name || direct.platform_id;
  $("#direct-auth-summary").textContent = selectedDirectCredential.configured
    ? `${selectedDirectCredential.type === "oauth" ? "账号 OAuth" : "API Key"} · ${selectedDirectCredential.source.startsWith("env:") ? "环境变量" : "本机文件"}`
    : "等待认证";
  $("#direct-model-summary").textContent = direct.model_id;
  $("#direct-stability-summary").textContent = stabilityNames[directProvider.stability]
    || directProvider.stability;

  if (!preserveForm) {
    $("#cursor-backend").value = cursor.backend || "cli";
    $("#cursor-model-source").textContent = cursor.backend === "cloud_api"
      ? "来自官方 GET /v1/models"
      : "来自 cursor-agent --list-models";
    $("#follow-effort").checked = cursor.follow_codex_effort !== false;
    renderCursorModels(cursor);
    renderKiroModels(kiro);
    setInputValue("#custom-base-url", custom.base_url);
    setInputValue("#custom-model", custom.model_id);
    setInputValue("#custom-models-path", custom.models_path || "/models");
    setInputValue("#custom-quota-path", custom.quota_path);
    setInputValue("#quota-total-field", custom.quota_total_field);
    setInputValue("#quota-used-field", custom.quota_used_field);
    setInputValue("#quota-remaining-field", custom.quota_remaining_field);
    setInputValue("#quota-reset-field", custom.quota_reset_field);
    setInputValue("#quota-unit", custom.quota_unit || "credits");
    $("#direct-follow-effort").checked = direct.follow_codex_effort !== false;
    renderDirectPlatformSelect(direct);
  }

  renderProvider(payload.settings.active_provider);
  renderLastRequest(payload.last_upstream_request);
  renderCodexConfig(payload.codex_config);
  const service = $("#service-pill");
  service.className = "service-pill ok";
  $("#service-label").textContent = `代理在线 · 当前 ${providerNames[payload.settings.active_provider]}`;
}

async function loadState(options) {
  const payload = await requestJSON("/api/control/state");
  renderState(payload, options);
  return payload;
}

function formatQuotaNumber(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function quotaMetric(label, value, unit) {
  const item = document.createElement("div");
  const caption = document.createElement("span");
  const strong = document.createElement("strong");
  caption.textContent = label;
  strong.textContent = value === "—" ? value : `${value} ${unit || ""}`.trim();
  item.append(caption, strong);
  return item;
}

function quotaParagraph(text) {
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  return paragraph;
}

function renderQuota(provider, payload = null, error = null) {
  const container = $(`#${provider}-quota`);
  const badge = $(`#${provider}-quota-badge`);
  container.replaceChildren();
  if (error) {
    badge.textContent = "查询失败";
    badge.className = "status-badge error";
    container.append(quotaParagraph(error));
    return;
  }
  if (payload.status === "available" || payload.status === "ok") {
    badge.textContent = "已更新";
    badge.className = "status-badge ok";
    const metrics = document.createElement("div");
    metrics.className = "quota-metrics";
    metrics.append(
      quotaMetric("剩余", formatQuotaNumber(payload.remaining), payload.unit),
      quotaMetric("已用", formatQuotaNumber(payload.used), payload.unit),
      quotaMetric("总额", formatQuotaNumber(payload.total), payload.unit),
    );
    container.append(metrics);
  } else if (payload.status === "unmapped") {
    badge.textContent = "字段未映射";
    badge.className = "status-badge warning";
    container.append(quotaParagraph("端点可以访问，但没有匹配到额度字段；请填写 JSON 字段映射。"));
  } else {
    badge.textContent = payload.account_verified ? "Key 已验证" : "官方未提供";
    badge.className = `status-badge ${payload.account_verified ? "ok" : "warning"}`;
    container.append(quotaParagraph(payload.note || "当前接口不提供剩余额度。"));
  }

  if (payload.last_run_usage) {
    const usage = document.createElement("div");
    usage.className = "quota-metrics";
    usage.append(
      quotaMetric("最近输入", formatQuotaNumber(payload.last_run_usage.input_tokens), "tokens"),
      quotaMetric("最近输出", formatQuotaNumber(payload.last_run_usage.output_tokens), "tokens"),
      quotaMetric("最近合计", formatQuotaNumber(payload.last_run_usage.total_tokens), "tokens"),
    );
    container.append(usage);
    const details = payload.last_run_usage.cursor_cli_details;
    if (details) {
      const cacheUsage = document.createElement("div");
      cacheUsage.className = "quota-metrics";
      cacheUsage.append(
        quotaMetric("未缓存输入", formatQuotaNumber(details.uncached_input_tokens), "tokens"),
        quotaMetric("缓存读取", formatQuotaNumber(details.cache_read_tokens), "tokens"),
        quotaMetric("缓存写入", formatQuotaNumber(details.cache_write_tokens), "tokens"),
      );
      container.append(cacheUsage);
    }
  }

  const meta = document.createElement("div");
  meta.className = "quota-meta";
  if (payload.plan) {
    const row = document.createElement("span");
    row.textContent = `套餐：${payload.plan}`;
    meta.append(row);
  }
  if (payload.reset_at) {
    const row = document.createElement("span");
    row.textContent = `重置：${payload.reset_at}`;
    meta.append(row);
  }
  if (payload.source) {
    const row = document.createElement("span");
    row.textContent = `来源：${payload.source}`;
    meta.append(row);
  }
  if (payload.dashboard_url) {
    const link = document.createElement("a");
    link.href = payload.dashboard_url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = "打开官方用量页 ↗";
    meta.append(link);
  }
  container.append(meta);
}

async function refreshQuota(provider) {
  const button = $(`[data-refresh-quota="${provider}"]`);
  if (state.busy.has(`quota-${provider}`)) return;
  if (provider === "cursor" && !state.config.cursor.api_key_configured) {
    renderQuota(provider, null, "请先配置 Cursor API Key。" );
    return;
  }
  if (provider === "custom" && !(state.config.custom.api_key_configured && state.config.custom.base_url)) {
    renderQuota(provider, null, "请先配置第三方 Base URL 与 API Key。" );
    return;
  }
  if (provider === "direct" && !state.payload.providers.direct.configured) {
    renderQuota(provider, null, "请先认证当前原生平台。" );
    return;
  }
  state.busy.add(`quota-${provider}`);
  button.disabled = true;
  try {
    const payload = await requestJSON(`/api/control/${provider}/quota?refresh=1`);
    renderQuota(provider, payload);
  } catch (error) {
    renderQuota(provider, null, error.message);
  } finally {
    state.busy.delete(`quota-${provider}`);
    button.disabled = false;
  }
}

async function loadModels(provider, force = false) {
  let query = force ? "?refresh=1" : "";
  if (provider === "direct") {
    const platformId = $("#direct-platform").value;
    const params = new URLSearchParams({ platform_id: platformId });
    if (force) params.set("refresh", "1");
    query = `?${params.toString()}`;
  }
  const payload = await requestJSON(`/api/control/${provider}/models${query}`);
  if (provider === "kiro") {
    state.kiroModels = payload.models;
    renderKiroModels(state.config.kiro);
  } else if (provider === "cursor") {
    state.cursorModels = payload.models;
    renderCursorModels(state.config.cursor);
  } else if (provider === "custom") {
    state.customModels = payload.models;
    renderCustomModelList();
  } else {
    state.directModels = payload.models;
    renderDirectModels(state.config.direct, $("#direct-platform").value);
  }
  return payload.models;
}

async function saveKiro() {
  const modelId = $("#kiro-model-select").value;
  const payload = await requestJSON("/api/control/settings", {
    method: "PUT",
    body: JSON.stringify({ kiro: { model_id: modelId } }),
  });
  renderState(payload);
  showToast(`Kiro 模型已切换为 ${modelId}`);
}

async function saveCursor({ includeKey = true, notify = true } = {}) {
  const cursor = state.config.cursor;
  const keyValue = $("#cursor-key").value.trim();
  const selected = state.selectedCursorVariant || {
    model_id: cursor.model_id || "",
    model_params: cursor.model_params || [],
    model_display_name: cursor.model_display_name || "Cursor 默认模型",
  };
  const cursorPayload = {
    backend: $("#cursor-backend").value,
    model_id: selected.model_id,
    model_params: selected.model_params,
    model_display_name: selected.model_display_name,
    follow_codex_effort: $("#follow-effort").checked,
  };
  if (includeKey && keyValue) cursorPayload.api_key = keyValue;
  const payload = await requestJSON("/api/control/settings", {
    method: "PUT",
    body: JSON.stringify({ cursor: cursorPayload }),
  });
  $("#cursor-key").value = "";
  renderState(payload);
  if (notify) showToast("Cursor 配置已应用");
  return payload;
}

function customPayload(includeKey = true) {
  const payload = {
    base_url: $("#custom-base-url").value.trim(),
    model_id: $("#custom-model").value.trim(),
    model_display_name: $("#custom-model").value.trim() || "Third-party default",
    models_path: $("#custom-models-path").value.trim() || "/models",
    quota_path: $("#custom-quota-path").value.trim(),
    quota_total_field: $("#quota-total-field").value.trim(),
    quota_used_field: $("#quota-used-field").value.trim(),
    quota_remaining_field: $("#quota-remaining-field").value.trim(),
    quota_reset_field: $("#quota-reset-field").value.trim(),
    quota_unit: $("#quota-unit").value.trim() || "credits",
  };
  const key = $("#custom-key").value.trim();
  if (includeKey && key) payload.api_key = key;
  return payload;
}

async function saveCustom({ includeKey = true, notify = true } = {}) {
  const payload = await requestJSON("/api/control/settings", {
    method: "PUT",
    body: JSON.stringify({ custom: customPayload(includeKey) }),
  });
  $("#custom-key").value = "";
  renderState(payload);
  if (notify) showToast("第三方代理配置已应用");
  return payload;
}

async function saveDirect({ notify = true } = {}) {
  const platform = directPlatform();
  if (!platform) throw new Error("请选择原生平台");
  const modelId = $("#direct-model").value || platform.default_model;
  const payload = await requestJSON("/api/control/settings", {
    method: "PUT",
    body: JSON.stringify({
      direct: {
        platform_id: platform.id,
        model_id: modelId,
        follow_codex_effort: $("#direct-follow-effort").checked,
      },
    }),
  });
  renderState(payload);
  if (notify) showToast(`${platform.name} 配置已应用`);
  return payload;
}

async function saveDirectKey() {
  const button = $("#save-direct-key");
  if (state.busy.has("direct-key")) return;
  const platform = directPlatform();
  const apiKey = $("#direct-key").value.trim();
  if (!platform || !apiKey) {
    showToast("请输入所选平台的 API Key", true);
    return;
  }
  state.busy.add("direct-key");
  button.disabled = true;
  button.textContent = "正在校验…";
  try {
    await saveDirect({ notify: false });
    const payload = await requestJSON("/api/control/direct/api-key", {
      method: "PUT",
      body: JSON.stringify({ platform_id: platform.id, api_key: apiKey }),
    });
    $("#direct-key").value = "";
    renderState(payload);
    const result = await requestJSON("/api/control/direct/test", {
      method: "POST",
      body: JSON.stringify({ platform_id: platform.id }),
    });
    state.directModels = result.models;
    renderDirectModels(state.config.direct, platform.id);
    showToast(`${platform.name} 认证成功，读取到 ${result.models.length} 个模型`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.busy.delete("direct-key");
    renderDirectControls($("#direct-platform").value);
    button.textContent = "保存并校验";
  }
}

function stopDirectLoginPolling() {
  if (state.directLoginTimer) clearTimeout(state.directLoginTimer);
  state.directLoginTimer = null;
}

async function pollDirectLogin() {
  if (!state.directLoginId) return;
  try {
    const login = await requestJSON(
      `/api/control/direct/auth/login/${encodeURIComponent(state.directLoginId)}`,
    );
    renderDirectLogin(login);
    if (["complete", "failed", "cancelled"].includes(login.status)) {
      stopDirectLoginPolling();
      if (login.status === "complete") {
        const payload = await loadState();
        await loadModels("direct", true).catch(() => {});
        showToast(`${directPlatform(payload.settings.direct.platform_id)?.name || "平台"} 登录成功`);
      }
      return;
    }
  } catch (error) {
    stopDirectLoginPolling();
    showToast(error.message, true);
    return;
  }
  state.directLoginTimer = setTimeout(pollDirectLogin, 1000);
}

async function startDirectLogin() {
  if (state.busy.has("direct-login")) return;
  const platform = directPlatform();
  if (!platform) return;
  state.busy.add("direct-login");
  $("#login-direct").disabled = true;
  try {
    await saveDirect({ notify: false });
    const options = {};
    if (platform.id === "kiro_direct") {
      const startUrl = $("#direct-kiro-start-url").value.trim();
      const region = $("#direct-kiro-region").value;
      if (startUrl) options.start_url = startUrl;
      if (region) options.region = region;
    }
    const login = await requestJSON(
      `/api/control/direct/auth/${encodeURIComponent(platform.id)}/login`,
      { method: "POST", body: JSON.stringify(options) },
    );
    state.directLoginId = login.id;
    renderDirectLogin(login);
    stopDirectLoginPolling();
    state.directLoginTimer = setTimeout(pollDirectLogin, 300);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.busy.delete("direct-login");
    renderDirectControls($("#direct-platform").value);
  }
}

async function importPiKiroCredentials() {
  if (state.busy.has("direct-import")) return;
  const platform = directPlatform();
  if (!platform || platform.id !== "kiro_direct") return;
  const button = $("#import-pi-kiro");
  state.busy.add("direct-import");
  button.disabled = true;
  try {
    await saveDirect({ notify: false });
    const payload = await requestJSON(
      "/api/control/direct/auth/kiro_direct/import",
      { method: "POST", body: JSON.stringify({ source: "pi" }) },
    );
    stopDirectLoginPolling();
    state.directLoginId = null;
    $("#direct-login-progress").hidden = true;
    state.directModels = [];
    renderState(payload);
    await loadModels("direct", true).catch(() => {});
    showToast("已从 ~/.pi/agent/auth.json 导入 Kiro 认证");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.busy.delete("direct-import");
    renderDirectControls($("#direct-platform").value);
  }
}

async function respondDirectLogin() {
  if (!state.directLoginId) return;
  const value = $("#direct-login-input").value.trim();
  if (!value) {
    showToast("请输入授权码或回调 URL", true);
    return;
  }
  try {
    const login = await requestJSON(
      `/api/control/direct/auth/login/${encodeURIComponent(state.directLoginId)}/respond`,
      { method: "POST", body: JSON.stringify({ value }) },
    );
    $("#direct-login-input").value = "";
    renderDirectLogin(login);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function cancelDirectLogin() {
  if (!state.directLoginId) return;
  try {
    const login = await requestJSON(
      `/api/control/direct/auth/login/${encodeURIComponent(state.directLoginId)}/cancel`,
      { method: "POST", body: "{}" },
    );
    stopDirectLoginPolling();
    renderDirectLogin(login);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function logoutDirect() {
  const platform = directPlatform();
  if (!platform) return;
  try {
    const payload = await requestJSON(
      `/api/control/direct/auth/${encodeURIComponent(platform.id)}`,
      { method: "DELETE" },
    );
    state.directModels = [];
    renderState(payload);
    showToast(`${platform.name} 的本机认证已清除`);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function testCursor() {
  const button = $("#test-cursor");
  if (state.busy.has("cursor-test")) return;
  state.busy.add("cursor-test");
  button.disabled = true;
  button.textContent = "正在校验…";
  try {
    await saveCursor({ notify: false });
    const payload = await requestJSON("/api/control/cursor/test", { method: "POST", body: "{}" });
    state.cursorModels = payload.models;
    renderCursorModels(state.config.cursor);
    await refreshQuota("cursor");
    showToast(`Cursor 连接成功，读取到 ${state.cursorModels.length} 个模型`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.busy.delete("cursor-test");
    button.disabled = false;
    button.textContent = "保存并校验";
  }
}

async function testCustom() {
  const button = $("#test-custom");
  if (state.busy.has("custom-test")) return;
  state.busy.add("custom-test");
  button.disabled = true;
  button.textContent = "正在校验…";
  try {
    await saveCustom({ notify: false });
    const payload = await requestJSON("/api/control/custom/test", { method: "POST", body: "{}" });
    state.customModels = payload.models;
    renderCustomModelList();
    await refreshQuota("custom");
    showToast(`第三方接口连接成功，读取到 ${state.customModels.length} 个模型`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.busy.delete("custom-test");
    button.disabled = false;
    button.textContent = "保存并校验";
  }
}

async function switchProvider(provider) {
  const current = state.config.active_provider;
  if (provider === "kiro" && !state.payload.providers.kiro.available) {
    renderProvider(current);
    showToast("本机没有找到 Kiro CLI", true);
    return;
  }
  if (provider === "cursor" && !state.payload.providers.cursor.configured) {
    renderProvider(current);
    const message = state.config.cursor.backend === "cli"
      && !state.payload.providers.cursor.cli_available
      ? "本机没有找到 Cursor Agent CLI"
      : "请先保存并校验 Cursor API Key";
    showToast(message, true);
    if (!state.config.cursor.api_key_configured) $("#cursor-key").focus();
    return;
  }
  if (provider === "custom" && !(state.config.custom.api_key_configured && state.config.custom.base_url)) {
    renderProvider(current);
    showToast("请先配置第三方 Base URL 与 API Key", true);
    $("#custom-base-url").focus();
    return;
  }
  if (provider === "direct" && !state.payload.providers.direct.configured) {
    renderProvider(current);
    showToast("请先认证当前原生平台", true);
    $("#direct-configuration").scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  try {
    const payload = await requestJSON("/api/control/settings", {
      method: "PUT",
      body: JSON.stringify({ active_provider: provider }),
    });
    renderState(payload, { preserveForm: true });
    showToast(`已切换到 ${providerNames[provider]}`);
  } catch (error) {
    renderProvider(current);
    showToast(error.message, true);
  }
}

async function clearKey(provider) {
  const section = state.config[provider];
  if (!section.api_key_configured) {
    showToast(`当前没有已保存的 ${providerNames[provider]} API Key`);
    return;
  }
  const button = provider === "cursor" ? $("#clear-cursor-key") : $("#clear-custom-key");
  button.disabled = true;
  try {
    const body = { [provider]: { clear_api_key: true } };
    if (state.config.active_provider === provider) body.active_provider = "kiro";
    const payload = await requestJSON("/api/control/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    });
    if (provider === "cursor") state.cursorModels = [];
    else state.customModels = [];
    renderState(payload);
    showToast(
      payload.settings[provider].api_key_configured
        ? "文件密钥已清除；环境变量仍然生效"
        : `${providerNames[provider]} API Key 已从本机配置中清除`,
    );
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function activeProxyModel() {
  const active = state.config.active_provider;
  if (active === "kiro") return state.config.kiro.model_id;
  if (active === "cursor") return state.config.cursor.model_id || "cursor-default";
  if (active === "direct") return state.config.direct.model_id;
  return state.config.custom.model_id || "custom-default";
}

async function codexConfigAction(action) {
  const confirmation = $("#codex-confirmation").value.trim();
  const button = action === "enable" ? $("#enable-codex-config") : $("#disable-codex-config");
  button.disabled = true;
  try {
    const body = action === "enable"
      ? { confirmation, model: activeProxyModel() }
      : { confirmation };
    const payload = await requestJSON(`/api/control/codex-config/${action}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    state.payload.codex_config = payload;
    renderCodexConfig(payload);
    $("#codex-confirmation").value = "";
    if (action === "enable") {
      showToast("Codex 原配置已备份，本地代理已启用");
    } else if (payload.restore_method === "managed_cleanup") {
      showToast("本地代理已关闭；备份不可用，仅清理了 Switchboard 路由值", true);
    } else if (payload.restore_method === "external_detach") {
      showToast("代理路由此前已被移除，Switchboard 状态已同步");
    } else {
      showToast("本地代理已关闭，受管字段已从备份恢复");
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    const config = state.payload.codex_config;
    $("#enable-codex-config").disabled = config.active;
    $("#disable-codex-config").disabled = !config.active;
  }
}

function bindEvents() {
  $$('[data-provider-card]').forEach((card) => {
    card.addEventListener("click", (event) => {
      if (event.target.closest("label, a, button")) return;
      const provider = card.dataset.providerCard;
      const radio = card.querySelector('input[type="radio"]');
      if (!radio.checked) switchProvider(provider);
    });
  });
  $$('input[name="provider"]').forEach((input) => {
    input.addEventListener("change", () => switchProvider(input.value));
  });
  $$('[data-refresh-quota]').forEach((button) => {
    button.addEventListener("click", () => refreshQuota(button.dataset.refreshQuota));
  });
  $("#refresh-kiro-models").addEventListener("click", async () => {
    const button = $("#refresh-kiro-models");
    button.disabled = true;
    try {
      const models = await loadModels("kiro", true);
      showToast(`读取到 ${models.length} 个 Kiro 模型`);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("#save-kiro").addEventListener("click", () => saveKiro().catch((error) => showToast(error.message, true)));
  $("#direct-platform").addEventListener("change", async (event) => {
    state.directModels = [];
    renderDirectControls(event.target.value);
    renderDirectModels(state.config.direct, event.target.value);
    try {
      await loadModels("direct");
    } catch (error) {
      if (directCredential(event.target.value).configured) showToast(error.message, true);
    }
  });
  $("#refresh-direct-models").addEventListener("click", async () => {
    const button = $("#refresh-direct-models");
    button.disabled = true;
    try {
      const models = await loadModels("direct", true);
      showToast(`读取到 ${models.length} 个原生平台模型`);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("#save-direct").addEventListener("click", async () => {
    const button = $("#save-direct");
    button.disabled = true;
    try { await saveDirect(); } catch (error) { showToast(error.message, true); }
    finally { button.disabled = false; }
  });
  $("#save-direct-key").addEventListener("click", saveDirectKey);
  $("#import-pi-kiro").addEventListener("click", importPiKiroCredentials);
  $("#login-direct").addEventListener("click", startDirectLogin);
  $("#logout-direct").addEventListener("click", logoutDirect);
  $("#direct-login-respond").addEventListener("click", respondDirectLogin);
  $("#direct-login-cancel").addEventListener("click", cancelDirectLogin);
  $("#test-cursor").addEventListener("click", testCursor);
  $("#clear-cursor-key").addEventListener("click", () => clearKey("cursor"));
  $("#save-cursor").addEventListener("click", async () => {
    const button = $("#save-cursor");
    button.disabled = true;
    try {
      const payload = await saveCursor();
      if (payload.settings.cursor.api_key_configured) await loadModels("cursor", true);
    } catch (error) { showToast(error.message, true); }
    finally { button.disabled = false; }
  });
  $("#cursor-backend").addEventListener("change", (event) => {
    $("#cursor-model-source").textContent = event.target.value === "cloud_api"
      ? "来自官方 GET /v1/models"
      : "来自 cursor-agent --list-models";
  });
  $("#cursor-model").addEventListener("change", (event) => {
    renderCursorVariantSelect(state.config.cursor, event.target.value);
  });
  $("#cursor-variant").addEventListener("change", (event) => {
    state.selectedCursorVariant = state.cursorVariants.find(
      (variant) => variant.key === event.target.value,
    );
    renderParamList(
      $("#cursor-params"),
      state.selectedCursorVariant?.model_params || [],
      "使用 Cursor 默认参数",
    );
  });
  $("#test-custom").addEventListener("click", testCustom);
  $("#clear-custom-key").addEventListener("click", () => clearKey("custom"));
  $("#save-custom").addEventListener("click", async () => {
    const button = $("#save-custom");
    button.disabled = true;
    try { await saveCustom(); } catch (error) { showToast(error.message, true); }
    finally { button.disabled = false; }
  });
  $("#enable-codex-config").addEventListener("click", () => codexConfigAction("enable"));
  $("#disable-codex-config").addEventListener("click", () => codexConfigAction("disable"));
  $("#copy-url").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(proxyBaseUrl());
      showToast("代理地址已复制");
    } catch {
      showToast("浏览器未允许访问剪贴板", true);
    }
  });
}

async function start() {
  const baseUrl = proxyBaseUrl();
  $("#endpoint-base").textContent = baseUrl.replace(/^https?:\/\//, "");
  $("#copy-url-value").textContent = baseUrl.replace(/^https?:\/\//, "");
  bindEvents();
  try {
    const payload = await loadState();
    const jobs = [];
    if (payload.providers.kiro.available) {
      jobs.push(loadModels("kiro").catch((error) => showToast(error.message, true)));
      jobs.push(refreshQuota("kiro"));
    }
    if (payload.settings.cursor.api_key_configured) {
      jobs.push(loadModels("cursor").catch(() => {}));
      jobs.push(refreshQuota("cursor"));
    }
    if (payload.providers.custom.configured) {
      jobs.push(loadModels("custom").catch(() => {}));
      jobs.push(refreshQuota("custom"));
    }
    if (payload.providers.direct.configured) {
      jobs.push(loadModels("direct").catch(() => {}));
      jobs.push(refreshQuota("direct"));
    }
    await Promise.allSettled(jobs);
  } catch (error) {
    const service = $("#service-pill");
    service.className = "service-pill error";
    $("#service-label").textContent = "无法连接本地代理";
    showToast(error.message, true);
  }
  setInterval(() => loadState({ preserveForm: true }).catch(() => {}), 5000);
}

start();
