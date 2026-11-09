/* Surplus Router admin panel - vanilla JS, no build step. */
"use strict";

const API = "/admin/api";

const FILTERS = [
  {
    id: "price_input",
    label: "Input price cap",
    desc: "Reject models whose input price reaches this cap (per 1M tokens).",
    toggle: "enable_price_input",
    fields: [
      { key: "max_input_per_1m", label: "Max $ per 1M input", type: "number", step: "0.01", min: "0" },
    ],
  },
  {
    id: "price_output",
    label: "Output price cap",
    desc: "Reject models whose output price reaches this cap (per 1M tokens).",
    toggle: "enable_price_output",
    fields: [
      { key: "max_output_per_1m", label: "Max $ per 1M output", type: "number", step: "0.01", min: "0" },
    ],
  },
  {
    id: "discount",
    label: "Minimum discount",
    desc: "Only route to sellers at least this far below the list price (upstream /min{N}).",
    toggle: "enable_discount",
    fields: [
      { key: "min_discount_pct", label: "Min discount %", type: "number", step: "1", min: "0", max: "100" },
    ],
  },
  {
    id: "models_allow",
    label: "Allowed models",
    desc: "Only these model ids may be used. Empty list while enabled rejects everything.",
    toggle: "enable_models_allow",
    fields: [{ key: "allow_models", label: "Model ids, comma separated", type: "text" }],
  },
  {
    id: "models_deny",
    label: "Blocked models",
    desc: "These model ids are always rejected, even if also allowed.",
    toggle: "enable_models_deny",
    fields: [{ key: "deny_models", label: "Model ids, comma separated", type: "text" }],
  },
  {
    id: "providers_allow",
    label: "Allowed providers",
    desc: "Only models from these providers pass (case-insensitive).",
    toggle: "enable_providers_allow",
    fields: [{ key: "allow_providers", label: "Providers, comma separated", type: "text" }],
  },
  {
    id: "context_min",
    label: "Minimum context",
    desc: "Reject models with a smaller context window.",
    toggle: "enable_context_min",
    fields: [
      { key: "min_context_length", label: "Min context tokens", type: "number", step: "1000", min: "0" },
    ],
  },
];

let CONFIG = null;
let STATUS = null;
let searchTimer = null;

const $ = (sel) => document.querySelector(sel);

async function getJson(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  const body = await res.json();
  if (!res.ok) throw new Error(body?.error?.message || `${res.status} ${url}`);
  return body;
}

async function postJson(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body?.error?.message || body?.detail || `${res.status}`);
  return body;
}

function toast(message, kind = "info") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 2600);
  el.classList.remove("hidden");
}

function banner(message, kind) {
  const el = $("#banner");
  if (!message) {
    el.classList.add("hidden");
    return;
  }
  el.textContent = message;
  el.className = `banner ${kind}`;
}

function money(value) {
  if (value === null || value === undefined) return "—";
  if (value === 0) return "free";
  if (value >= 100) return value.toFixed(0);
  if (value >= 1) return value.toFixed(2);
  return Number(value.toFixed(4)).toString();
}

function uptime(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/* ------------------------------------------------------------------ render */

function renderStatus(status) {
  STATUS = status;
  const c = status.counts || {};
  $("#status-line").textContent =
    `v${status.version} · up ${uptime(status.uptime_seconds)} · ` +
    `${c.passing ?? 0} of ${c.total ?? 0} models pass the filters`;

  const badges = [];
  const keySrc = status.api_key?.source;
  badges.push(
    keySrc === "missing"
      ? `<span class="badge bad">API key missing</span>`
      : `<span class="badge ok">key: ${keySrc} ${status.api_key?.masked || ""}</span>`
  );
  const cat = status.catalog || {};
  badges.push(
    cat.stale
      ? `<span class="badge bad">prices stale</span>`
      : `<span class="badge ok">prices fresh (${Math.round(cat.age_seconds ?? 0)}s)</span>`
  );
  if (status.minimum_discount_prefix) {
    badges.push(`<span class="badge">routing ${status.minimum_discount_prefix}/v1</span>`);
  }
  $("#badges").innerHTML = badges.join("");

  // A saved host/port only takes effect after a restart; keep saying so.
  if (status.restart_required && status.listening_on) {
    const cfgServer = status.config?.server || {};
    banner(
      `Restart required: config says ${cfgServer.host}:${cfgServer.port}, but the router is ` +
        `still listening on ${status.listening_on.host}:${status.listening_on.port}.`,
      "warn"
    );
  }

  $("#key-source").textContent = keySrc || "—";
  $("#key-masked").textContent = status.api_key?.masked || "not set";
  $("#key-env").textContent = status.config?.auth?.api_key_env || "SURPLUS_API_KEY";

  const rows = [
    ["Last refresh", cat.last_fetch_iso || "never"],
    ["Age", cat.age_seconds === null || cat.age_seconds === undefined ? "—" : `${Math.round(cat.age_seconds)}s`],
    ["Upstream updated_at", cat.upstream_updated_at || "—"],
    ["Refreshes since start", cat.refresh_count ?? 0],
    ["TTL", `${status.config?.upstream?.catalog_ttl_seconds ?? "?"}s`],
    ["Last error", cat.last_error || "none"],
  ];
  $("#catalog-kv").innerHTML = rows
    .map(([k, v]) => `<span>${k}</span><strong>${escapeHtml(String(v))}</strong>`)
    .join("");

  const counts = [
    ["Total models", c.total],
    ["Passing", c.passing],
    ["Filtered out", c.filtered_out],
    ["Zero priced", c.zero_priced],
  ];
  $("#counts").innerHTML = counts
    .map(([label, value]) => `<div class="count"><b>${value ?? "—"}</b><span>${label}</span></div>`)
    .join("");

  renderSnippets(status.config);
}

function renderConfig(cfg) {
  CONFIG = cfg;
  $("#f-host").value = cfg.server.host;
  $("#f-port").value = cfg.server.port;
  $("#f-base").value = cfg.upstream.base_url;
  $("#f-ttl").value = cfg.upstream.catalog_ttl_seconds;
  $("#f-timeout").value = cfg.upstream.request_timeout_seconds;
  $("#f-allow-unknown").checked = !!cfg.filter.allow_unknown_models;

  for (const def of FILTERS) {
    $(`#tog-${def.id}`).checked = !!cfg.filter[def.toggle];
    for (const field of def.fields) {
      const el = $(`#fld-${field.key}`);
      const value = cfg.filter[field.key];
      el.value = Array.isArray(value) ? value.join(", ") : value;
    }
    syncFilterCard(def);
  }
  $("#filter-preview").textContent = "";
}

function syncFilterCard(def) {
  const on = $(`#tog-${def.id}`).checked;
  const card = $(`#card-${def.id}`);
  card.classList.toggle("disabled", !on);
  for (const field of def.fields) $(`#fld-${field.key}`).disabled = !on;
}

function buildFilterCards() {
  $("#filter-cards").innerHTML = FILTERS.map((def) => {
    const fields = def.fields
      .map(
        (f) => `
      <div class="field">
        <label for="fld-${f.key}">${f.label}</label>
        <input id="fld-${f.key}" type="${f.type}" ${f.step ? `step="${f.step}"` : ""}
          ${f.min !== undefined ? `min="${f.min}"` : ""} ${f.max !== undefined ? `max="${f.max}"` : ""}
          spellcheck="false">
      </div>`
      )
      .join("");
    return `
    <div class="filter-card" id="card-${def.id}">
      <div class="filter-head">
        <label class="switch">
          <input type="checkbox" id="tog-${def.id}">
          <span class="slider"></span>
        </label>
        <div><strong>${def.label}</strong><span class="desc">${def.desc}</span></div>
      </div>
      <div class="filter-body">${fields}</div>
    </div>`;
  }).join("");

  for (const def of FILTERS) {
    $(`#tog-${def.id}`).addEventListener("change", () => syncFilterCard(def));
  }
}

function collectFilters() {
  const payload = { allow_unknown_models: $("#f-allow-unknown").checked };
  for (const def of FILTERS) {
    payload[def.toggle] = $(`#tog-${def.id}`).checked;
    for (const field of def.fields) {
      const raw = $(`#fld-${field.key}`).value;
      if (field.type === "number") {
        const num = Number(raw);
        payload[field.key] = Number.isFinite(num) ? num : 0;
      } else {
        payload[field.key] = raw;
      }
    }
  }
  return payload;
}

function renderSnippets(cfg) {
  if (!cfg) return;
  const base = `http://${cfg.server.host === "0.0.0.0" ? "127.0.0.1" : cfg.server.host}:${cfg.server.port}`;
  const items = [
    {
      title: "OpenAI Python SDK",
      code: `from openai import OpenAI

client = OpenAI(base_url="${base}/v1", api_key="router")  # placeholder key is fine
models = client.models.list()                              # already filtered
resp = client.chat.completions.create(
    model=models.data[0].id,
    messages=[{"role": "user", "content": "hello"}],
    stream=True,
)`,
    },
    {
      title: "Any OpenAI-compatible tool (Aider, Cursor, Continue, OpenCode)",
      code: `OPENAI_API_BASE=${base}/v1
OPENAI_API_KEY=***`,
    },
    {
      title: "curl",
      code: `curl ${base}/v1/models

curl ${base}/v1/chat/completions \\
  -H 'Content-Type: application/json' \\
  -d '{"model":"openai-gpt-oss-120b","messages":[{"role":"user","content":"hi"}]}'`,
    },
    {
      title: "Anthropic SDK / Claude Code",
      code: `# enabled in stage 6: the /anthropic skin
ANTHROPIC_BASE_URL=${base}/anthropic
ANTHROPIC_API_KEY=***`,
    },
  ];
  $("#snippets").innerHTML = items
    .map(
      (item, i) => `
    <div class="snippet">
      <header><span>${item.title}</span><button class="btn" data-copy="${i}" type="button">Copy</button></header>
      <pre>${escapeHtml(item.code)}</pre>
    </div>`
    )
    .join("");
  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = items[Number(btn.dataset.copy)].code;
      try {
        await navigator.clipboard.writeText(text);
        toast("Copied to clipboard");
      } catch {
        toast("Clipboard blocked by the browser", "warn");
      }
    });
  });
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]
  );
}

/* ------------------------------------------------------------------ models */

async function loadModels() {
  const params = new URLSearchParams({
    q: $("#model-search").value.trim(),
    passed_only: $("#model-passed-only").checked ? "true" : "false",
    sort: $("#model-sort").value,
    limit: "400",
  });
  try {
    const data = await getJson(`${API}/models?${params}`);
    const tbody = $("#model-table tbody");
    tbody.innerHTML = data.models
      .map((m) => {
        const status = m.passed
          ? `<span class="pass">pass</span>`
          : `<span class="fail">blocked</span> <span class="reasons">${m.failed_filters.join(", ")}</span>`;
        const discount =
          m.estimated_discount_pct === null || m.estimated_discount_pct === undefined
            ? "—"
            : `${m.estimated_discount_pct.toFixed(0)}%`;
        return `<tr>
          <td><strong>${escapeHtml(m.id)}</strong><br><span class="reasons">${escapeHtml(m.name)}</span></td>
          <td>${escapeHtml(m.provider || "—")}</td>
          <td class="num">${money(m.input_per_1m)}</td>
          <td class="num">${money(m.output_per_1m)}</td>
          <td class="num">${discount}</td>
          <td class="num">${m.context_length ? m.context_length.toLocaleString() : "—"}</td>
          <td>${status}</td>
        </tr>`;
      })
      .join("");
    $("#model-count").textContent = `showing ${data.models.length} of ${data.count} matching`;
  } catch (err) {
    toast(`Catalog load failed: ${err.message}`, "bad");
  }
}

/* -------------------------------------------------------------------- init */

async function loadAll() {
  try {
    const [status, cfg] = await Promise.all([getJson(`${API}/status`), getJson(`${API}/config`)]);
    renderConfig(cfg);
    renderStatus(status);
    if (!status.restart_required) banner(null);
  } catch (err) {
    banner(`Could not reach the router admin API: ${err.message}`, "bad");
  }
}

async function save(payload, message) {
  try {
    const res = await postJson(`${API}/config`, payload);
    renderConfig(res.config);
    await Promise.all([loadAll(), loadModels()]);
    if (res.restart_required) {
      banner(
        `Saved. Host/port changed to ${res.config.server.host}:${res.config.server.port} — ` +
          `restart the router (still listening on ${res.listening_on.host}:${res.listening_on.port}).`,
        "warn"
      );
    } else {
      banner(null);
      toast(message);
    }
  } catch (err) {
    toast(`Save failed: ${err.message}`, "bad");
  }
}

function wire() {
  buildFilterCards();

  $("#save-connection").addEventListener("click", () =>
    save(
      {
        server: { host: $("#f-host").value.trim(), port: Number($("#f-port").value) },
        upstream: {
          base_url: $("#f-base").value.trim(),
          catalog_ttl_seconds: Number($("#f-ttl").value),
          request_timeout_seconds: Number($("#f-timeout").value),
        },
      },
      "Connection settings saved"
    )
  );

  $("#save-filters").addEventListener("click", () =>
    save({ filter: collectFilters() }, "Filters saved and active")
  );

  $("#preview-btn").addEventListener("click", () =>
    save({ filter: collectFilters() }, "Filters applied — counts refreshed")
  );

  $("#save-key").addEventListener("click", async () => {
    const value = $("#f-key").value.trim();
    if (!value) {
      toast("Enter a key first", "warn");
      return;
    }
    try {
      await postJson(`${API}/config`, { auth: { api_key: value } });
      $("#f-key").value = "";
      await loadAll();
      toast("API key saved to config.ini");
    } catch (err) {
      toast(`Key save failed: ${err.message}`, "bad");
    }
  });

  $("#clear-key").addEventListener("click", async () => {
    if (!window.confirm("Remove the stored API key from config.ini?")) return;
    try {
      await postJson(`${API}/config`, { auth: { clear_api_key: true } });
      await loadAll();
      toast("Stored key removed");
    } catch (err) {
      toast(`Could not clear key: ${err.message}`, "bad");
    }
  });

  const doRefresh = async (btn) => {
    btn.disabled = true;
    btn.textContent = "Refreshing…";
    try {
      const res = await postJson(`${API}/refresh`);
      await Promise.all([loadAll(), loadModels()]);
      toast(
        res.ok
          ? `Prices refreshed: ${res.counts.total} models, ${res.counts.passing} passing`
          : "Refresh failed — kept the last good prices",
        res.ok ? "info" : "warn"
      );
    } catch (err) {
      toast(`Refresh failed: ${err.message}`, "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = btn.id === "refresh-btn" ? "Refresh prices" : "Refresh now";
    }
  };
  $("#refresh-btn").addEventListener("click", (e) => doRefresh(e.currentTarget));
  $("#refresh-btn-2").addEventListener("click", (e) => doRefresh(e.currentTarget));

  $("#model-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadModels, 250);
  });
  $("#model-passed-only").addEventListener("change", loadModels);
  $("#model-sort").addEventListener("change", loadModels);

  // keep the freshness badge honest without a page reload
  setInterval(() => {
    if (!document.hidden) getJson(`${API}/status`).then(renderStatus).catch(() => {});
  }, 20000);
}

wire();
loadAll().then(loadModels);
