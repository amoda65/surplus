/* Surplus Router admin panel - vanilla JS, no build step. */
"use strict";

const API = "/admin/api";

/* Filter definitions, grouped. Keys map 1:1 onto config.ini [filter]. */
const FILTER_GROUPS = [
  {
    title: "Price caps",
    hint: "Applied to the ACTUAL marketplace price (what you pay), not the list price. Caps are inclusive.",
    filters: [
      {
        id: "price_input",
        label: "Input price cap",
        desc: "Reject models whose live input price is above this cap ($ per 1M tokens).",
        toggle: "enable_price_input",
        fields: [
          { key: "max_input_per_1m", label: "Max $ per 1M input", type: "number", step: "0.01", min: "0" },
        ],
      },
      {
        id: "price_output",
        label: "Output price cap",
        desc: "Reject models whose live output price is above this cap ($ per 1M tokens).",
        toggle: "enable_price_output",
        fields: [
          { key: "max_output_per_1m", label: "Max $ per 1M output", type: "number", step: "0.01", min: "0" },
        ],
      },
      {
        id: "cache_write_cap",
        label: "Cache-write price cap",
        desc: "Prompt-cache writes often cost ~10x the input price. Reject models above this cap ($ per 1M).",
        toggle: "enable_cache_write_cap",
        fields: [
          { key: "max_cache_write_per_1m", label: "Max $ per 1M cache write", type: "number", step: "0.1", min: "0" },
        ],
      },
      {
        id: "trusted_only",
        label: "Trusted providers only",
        desc: "Apply caps and the discount floor to the best TRUSTED-provider offer; reject models only untrusted sellers serve (no_trusted_offer).",
        toggle: "enable_trusted_only",
        fields: [],
      },
    ],
  },
  {
    title: "Discount & trend",
    filters: [
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
        id: "exclude_tightening",
        label: "Exclude tightening markets",
        desc: "Reject models whose discount trend is 'tightening' (offers getting worse over time).",
        toggle: "enable_exclude_tightening",
        fields: [],
      },
    ],
  },
  {
    title: "Market quality",
    hint: "Unmeasured markets pass (fail-open): the marketplace only samples models with traffic.",
    filters: [
      {
        id: "min_uptime",
        label: "Minimum uptime",
        desc: "Reject markets whose measured uptime is below this percentage.",
        toggle: "enable_min_uptime",
        fields: [
          { key: "min_uptime_pct", label: "Min uptime %", type: "number", step: "1", min: "0", max: "100" },
        ],
      },
      {
        id: "max_ttft",
        label: "Max time-to-first-token",
        desc: "Reject markets whose median TTFT is above this many milliseconds.",
        toggle: "enable_max_ttft",
        fields: [
          { key: "max_ttft_ms", label: "Max TTFT ms", type: "number", step: "100", min: "0" },
        ],
      },
      {
        id: "min_tps",
        label: "Minimum generation speed",
        desc: "Reject markets whose median tokens/second is below this.",
        toggle: "enable_min_tps",
        fields: [
          { key: "min_tps", label: "Min TPS", type: "number", step: "1", min: "0" },
        ],
      },
    ],
  },
  {
    title: "Market depth",
    hint: "Thin markets can flip to 'no live offer' while your agent is running.",
    filters: [
      {
        id: "min_sellers",
        label: "Minimum healthy sellers",
        desc: "Reject models with fewer healthy sellers than this.",
        toggle: "enable_min_sellers",
        fields: [
          { key: "min_healthy_sellers", label: "Min sellers", type: "number", step: "1", min: "0" },
        ],
      },
      {
        id: "min_uncapped",
        label: "Minimum uncapped offers",
        desc: "Capped offers run dry mid-job; require at least this many uncapped discounted offers.",
        toggle: "enable_min_uncapped",
        fields: [
          { key: "min_uncapped_offers", label: "Min uncapped offers", type: "number", step: "1", min: "0" },
        ],
      },
    ],
  },
  {
    title: "Capability",
    filters: [
      {
        id: "context_min",
        label: "Minimum context",
        desc: "Reject models with a smaller context window.",
        toggle: "enable_context_min",
        fields: [
          { key: "min_context_length", label: "Min context tokens", type: "number", step: "1000", min: "0" },
        ],
      },
      {
        id: "require_features",
        label: "Required features",
        desc: "Model must support ALL of these (e.g. streaming, tools, reasoning, vision).",
        toggle: "enable_require_features",
        fields: [{ key: "require_features", label: "Features, comma separated", type: "text" }],
      },
    ],
  },
  {
    title: "Lists",
    filters: [
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
    ],
  },
];

const ALL_FILTERS = FILTER_GROUPS.flatMap((g) => g.filters);

const REASON_LABELS = {
  no_market_offer: "no live offer",
  not_token_priced: "not token-priced",
  no_trusted_offer: "no trusted offer",
  unknown_model: "unknown model",
  models_deny: "blocked list",
  models_allow: "not in allow list",
  providers_allow: "provider not allowed",
  price_input: "input over cap",
  price_output: "output over cap",
  discount: "discount below floor",
  context_min: "context too small",
  min_uptime: "low uptime",
  max_ttft: "slow first token",
  min_tps: "slow generation",
  min_sellers: "too few sellers",
  min_uncapped: "no uncapped offers",
  cache_write_cap: "cache write over cap",
  require_features: "missing features",
  exclude_tightening: "discount tightening",
};

let CONFIG = null;
let STATUS = null;
let searchTimer = null;
let requestCursor = null;

const $ = (sel) => document.querySelector(sel);

const reason = (id) => REASON_LABELS[id] || id;

async function getJson(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  const body = await res.json();
  if (!res.ok) throw new Error(body?.error?.message || body?.error || `${res.status} ${url}`);
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
  if (value === 0) return "0";
  if (value < 0.001) return Number(value.toFixed(6)).toString();
  if (value < 1) return Number(value.toFixed(4)).toString();
  if (value >= 1000000) return `${(value / 1000000).toFixed(2)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  if (value >= 100) return value.toFixed(0);
  return value.toFixed(2);
}

function usd(value) {
  if (value === null || value === undefined) return "—";
  return `$${money(value)}`;
}

function compactInt(value) {
  if (value === null || value === undefined) return "—";
  if (value >= 1000000) return `${(value / 1000000).toFixed(1)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

function pct(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toFixed(digits)}%`;
}

function uptime(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]
  );
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
  $("#config-path").textContent = status.config?.config_path || "—";

  const rows = [
    ["Last refresh", cat.last_fetch_iso || "never"],
    ["Age", cat.age_seconds === null || cat.age_seconds === undefined ? "—" : `${Math.round(cat.age_seconds)}s`],
    ["Market source", cat.market_source || "—"],
    ["Live market offers", cat.market_offer_count ?? "—"],
    ["Market computed", cat.market_age_seconds === null || cat.market_age_seconds === undefined
      ? "—"
      : `${Math.round(cat.market_age_seconds)}s ago`],
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
    ["Token priced", c.class_token],
    ["Media/unit priced", c.class_media_unit],
    ["No live offer", c.class_no_offer],
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
  $("#f-open-browser").checked = cfg.server.open_browser !== false;
  $("#f-allow-unknown").checked = !!cfg.filter.allow_unknown_models;

  for (const def of ALL_FILTERS) {
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
  $("#filter-groups").innerHTML = FILTER_GROUPS.map(
    (group) => `
    <fieldset class="filter-group">
      <legend>${escapeHtml(group.title)}</legend>
      ${group.hint ? `<p class="hint">${escapeHtml(group.hint)}</p>` : ""}
      <div class="filter-grid">
        ${group.filters.map((def) => {
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
          ${fields ? `<div class="filter-body">${fields}</div>` : ""}
        </div>`;
        }).join("")}
      </div>
    </fieldset>`
  ).join("");

  for (const def of ALL_FILTERS) {
    $(`#tog-${def.id}`).addEventListener("change", () => syncFilterCard(def));
  }
}

function collectFilters() {
  const payload = { allow_unknown_models: $("#f-allow-unknown").checked };
  for (const def of ALL_FILTERS) {
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

/* ------------------------------------------------------------------ models */

async function loadModels() {
  const providerSel = $("#model-provider");
  const params = new URLSearchParams({
    q: $("#model-search").value.trim(),
    passed_only: $("#model-passed-only").checked ? "true" : "false",
    price_class: $("#model-class").value,
    provider: providerSel ? providerSel.value : "",
    sort: $("#model-sort").value,
    dir: $("#model-dir").value,
    limit: "400",
  });
  try {
    const data = await getJson(`${API}/models?${params}`);
    const tbody = $("#model-table tbody");
    tbody.innerHTML = data.models
      .map((m) => {
        const status = m.passed
          ? `<span class="pass">pass</span>`
          : `<span class="fail">blocked</span> <span class="reasons">${m.failed_filters.map(reason).join(", ")}</span>`;
        const discount =
          m.estimated_discount_pct === null || m.estimated_discount_pct === undefined
            ? "—"
            : `${m.estimated_discount_pct.toFixed(1)}%`;
        const priceNote =
          m.price_class === "media_unit"
            ? `<span class="reasons">${escapeHtml(m.media_unit || "unit")}: ${money(m.media_unit_price_usd)}</span>`
            : m.price_class === "token"
              ? ""
              : `<span class="reasons">${escapeHtml(m.price_class)}</span>`;
        const trendIcon =
          m.trend_direction === "deepening" ? "▼" : m.trend_direction === "tightening" ? "▲" : m.trend_direction ? "＝" : "";
        const trendCls =
          m.trend_direction === "tightening" ? "trend-bad" : m.trend_direction === "deepening" ? "trend-ok" : "";
        return `<tr data-model="${escapeHtml(m.id)}" class="clickable">
          <td><strong>${escapeHtml(m.id)}</strong><br><span class="reasons">${escapeHtml(m.name)} · ${escapeHtml(m.modality || "")}</span></td>
          <td>${escapeHtml(m.provider || "—")}</td>
          <td class="num">${money(m.market_input_per_1m)}</td>
          <td class="num">${money(m.market_output_per_1m)} ${priceNote}</td>
          <td class="num">${discount}</td>
          <td class="num" title="${m.healthy_seller_count || 0} healthy / ${m.uncapped_offer_count || 0} uncapped">${m.healthy_seller_count || 0}/${m.uncapped_offer_count || 0}</td>
          <td class="num">${m.uptime_pct === null || m.uptime_pct === undefined ? "—" : Number(m.uptime_pct).toFixed(0)}</td>
          <td class="num">${m.ttft_p50_ms === null || m.ttft_p50_ms === undefined ? "—" : Math.round(m.ttft_p50_ms).toLocaleString()}</td>
          <td class="num">${m.tps_p50 === null || m.tps_p50 === undefined ? "—" : Math.round(m.tps_p50)}</td>
          <td class="num">${compactInt(m.requests_24h)}</td>
          <td class="num">${m.context_length ? compactInt(m.context_length) : "—"}</td>
          <td class="num muted">${money(m.trusted_output_per_1m)}</td>
          <td>${status}</td>
        </tr>`;
      })
      .join("");
    $("#model-count").textContent = `showing ${data.models.length} of ${data.count} matching`;
    tbody.querySelectorAll("tr[data-model]").forEach((tr) => {
      tr.addEventListener("click", () => openDrawer(tr.dataset.model));
    });
    populateProviderFilter(data.models);
  } catch (err) {
    toast(`Catalog load failed: ${err.message}`, "bad");
  }
}

let providerOptionsLoaded = false;
function populateProviderFilter(models) {
  if (providerOptionsLoaded) return;
  const sel = $("#model-provider");
  if (!sel) return;
  const providers = [...new Set(models.map((m) => m.provider).filter(Boolean))].sort();
  sel.innerHTML =
    `<option value="">all providers</option>` +
    providers.map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("");
  providerOptionsLoaded = true;
}

/* ------------------------------------------------------------------ drawer */

async function openDrawer(modelId) {
  const drawer = $("#drawer");
  const backdrop = $("#drawer-backdrop");
  $("#drawer-title").textContent = modelId;
  $("#drawer-body").innerHTML = `<p class="hint">Loading ${escapeHtml(modelId)}…</p>`;
  drawer.classList.remove("hidden");
  drawer.setAttribute("aria-hidden", "false");
  backdrop.classList.remove("hidden");
  try {
    const m = await getJson(`${API}/models/${encodeURIComponent(modelId)}`);
    renderDrawer(m);
  } catch (err) {
    $("#drawer-body").innerHTML = `<p class="hint fail">Could not load: ${escapeHtml(err.message)}</p>`;
  }
}

function closeDrawer() {
  $("#drawer").classList.add("hidden");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawer-backdrop").classList.add("hidden");
}

function kvGrid(pairs) {
  return `<div class="kv">${pairs
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => `<span>${escapeHtml(k)}</span><strong>${v}</strong>`)
    .join("")}</div>`;
}

function renderDrawer(m) {
  const statusLine = m.passed
    ? `<span class="badge ok">passes all filters</span>`
    : `<span class="badge bad">blocked: ${m.failed_filters.map(reason).join(", ")}</span>`;
  const featureBadges = (m.features || [])
    .map((f) => `<span class="badge">${escapeHtml(f)}</span>`)
    .join(" ");
  const trendLabel =
    m.trend_direction === "deepening"
      ? "deepening (getting cheaper)"
      : m.trend_direction === "tightening"
        ? "tightening (getting more expensive)"
        : m.trend_direction || "no trend data";

  const providers = (m.providers || [])
    .map(
      (p) => `<tr>
        <td>${escapeHtml(p.provider || "?")} ${p.trusted ? `<span class="badge ok">trusted</span>` : `<span class="badge">untrusted</span>`}</td>
        <td class="num">${money(p.input_per_1m)}</td>
        <td class="num">${money(p.output_per_1m)}</td>
        <td class="num">${p.healthy_sellers ?? "—"}</td>
        <td class="num">${p.uncapped_offers ?? "—"}</td>
      </tr>`
    )
    .join("");

  $("#drawer-body").innerHTML = `
    <div class="drawer-status">${statusLine}</div>
    ${m.description ? `<p class="desc">${escapeHtml(m.description)}</p>` : ""}
    <h3>What you pay (live market)</h3>
    ${kvGrid([
      ["Market input $/1M", money(m.market_input_per_1m)],
      ["Market output $/1M", money(m.market_output_per_1m)],
      ["Cache read $/1M", money(m.cache_read_per_1m)],
      ["Cache write $/1M", money(m.cache_write_per_1m)],
      ["Discount (live)", pct(m.estimated_discount_pct)],
      ["Median live discount", pct(m.median_live_discount_pct ?? null)],
      ["List input/output $/1M", `${money(m.list_input_per_1m)} / ${money(m.list_output_per_1m)}`],
      ["Reference input/output $/1M", `${money(m.reference_input_per_1m)} / ${money(m.reference_output_per_1m)}`],
    ])}
    <h3>Trusted-provider view</h3>
    ${kvGrid([
      ["Trusted input $/1M", money(m.trusted_input_per_1m)],
      ["Trusted output $/1M", money(m.trusted_output_per_1m)],
      ["Best trusted discount", pct(m.best_trusted_offer_discount_pct)],
    ])}
    <h3>Market depth &amp; activity</h3>
    ${kvGrid([
      ["Healthy sellers", m.healthy_seller_count],
      ["Total sellers", m.num_sellers],
      ["Uncapped offers", m.uncapped_offer_count],
      ["Capped liquidity", usd(m.liquidity_usd)],
      ["Requests 24h", compactInt(m.requests_24h)],
      ["Volume 24h", usd(m.volume_24h_usd)],
      ["Discount trend", `${escapeHtml(trendLabel)}${m.trend_current_pct != null ? ` · now ${pct(m.trend_current_pct)}` : ""}`],
    ])}
    <h3>Quality (measured by the marketplace)</h3>
    ${kvGrid([
      ["Uptime", pct(m.uptime_pct)],
      ["TTFT p50", m.ttft_p50_ms == null ? "not measured" : `${Math.round(m.ttft_p50_ms).toLocaleString()} ms (${m.ttft_samples ?? 0} samples)`],
      ["TPS p50", m.tps_p50 == null ? "not measured" : `${Math.round(m.tps_p50)} tok/s (${m.tps_samples ?? 0} samples)`],
      ["Cache hit rate", pct(m.cache_hit_rate_pct)],
    ])}
    <h3>Capability</h3>
    ${kvGrid([
      ["Provider", escapeHtml(m.provider || "—")],
      ["Modality", escapeHtml(m.modality || "—")],
      ["Context", m.context_length ? m.context_length.toLocaleString() : "—"],
      ["Max completion tokens", m.max_completion_tokens ? m.max_completion_tokens.toLocaleString() : "—"],
      ["Moderated", m.is_moderated ? "yes" : "no"],
      ["Price class", escapeHtml(m.price_class)],
    ])}
    <div class="badges" style="margin:6px 0 12px">${featureBadges || `<span class="badge">no features listed</span>`}</div>
    ${providers ? `<h3>Per-provider offers (best price each)</h3>
    <div class="table-wrap small"><table>
      <thead><tr><th>Provider</th><th class="num">In $/1M</th><th class="num">Out $/1M</th><th class="num">Healthy</th><th class="num">Uncapped</th></tr></thead>
      <tbody>${providers}</tbody>
    </table></div>` : ""}
  `;
}

/* ----------------------------------------------------------------- account */

function scalarKv(obj, skip = new Set()) {
  const pairs = [];
  for (const [k, v] of Object.entries(obj || {})) {
    if (skip.has(k) || v === null || v === undefined) continue;
    if (typeof v === "object") continue;
    let shown;
    if (k.endsWith("_usd")) shown = usd(v);
    else if (k.endsWith("_pct")) shown = pct(v);
    else if (typeof v === "number" && v > 1000) shown = compactInt(v);
    else shown = escapeHtml(String(v));
    pairs.push([k.replace(/_/g, " "), shown]);
  }
  return pairs;
}

function renderOverview(title, ov) {
  if (!ov) return "";
  if (ov.coverage === false) {
    return `<div class="overview"><h3>${title}</h3><p class="hint">No aggregated data for this window yet.</p></div>`;
  }
  const skip = new Set(["coverage"]);
  const pairs = scalarKv(ov, skip);
  const topModels = ov.top_models || ov.topModels || null;
  let topHtml = "";
  if (Array.isArray(topModels) && topModels.length) {
    topHtml = `<div class="table-wrap small"><table><thead><tr><th>Top model</th><th class="num">Spend</th><th class="num">Requests</th></tr></thead><tbody>${topModels
      .slice(0, 8)
      .map((t) => {
        const spend = t.spend_usd ?? t.spent_usd ?? (t.spend_micro != null ? t.spend_micro / 1e6 : t.cost_usd);
        const reqs = t.requests ?? t.request_count ?? t.count;
        return `<tr><td>${escapeHtml(t.model || t.model_id || "?")}</td><td class="num">${usd(spend)}</td><td class="num">${compactInt(reqs)}</td></tr>`;
      })
      .join("")}</tbody></table></div>`;
  }
  return `<div class="overview"><h3>${title}</h3>${kvGrid(pairs)}${topHtml}</div>`;
}

function renderAccount(data) {
  const body = $("#account-body");
  if (!data || data.available === false) {
    const why =
      data?.reason === "no_api_key"
        ? "Set your Surplus API key above to see balance and usage."
        : `Account data unavailable (${escapeHtml(data?.reason || "unknown")}).`;
    body.innerHTML = `<p class="hint">${why}</p>`;
    return;
  }
  const bal = data.balance;
  let html = "";
  if (bal) {
    const statusBadge =
      bal.account_status === "active"
        ? `<span class="badge ok">active</span>`
        : `<span class="badge bad">${escapeHtml(bal.account_status || "?")}</span>`;
    html += `<div class="counts">
      <div class="count accent"><b>${usd(bal.credit_balance_usd)}</b><span>Credits (spendable)</span></div>
      <div class="count"><b>${usd(bal.fiat_available_usd)}</b><span>Fiat credit</span></div>
      <div class="count"><b>${usd(bal.usdc_available_usd)}</b><span>USDC (withdrawable)</span></div>
      <div class="count"><b>${usd((bal.fiat_held_usd || 0) + (bal.usdc_held_usd || 0))}</b><span>Held in-flight</span></div>
      <div class="count"><b>${statusBadge}</b><span>Account</span></div>
    </div>`;
  } else if (data.balance_error) {
    html += `<p class="hint">Balance unavailable: ${escapeHtml(data.balance_error)}</p>`;
  }
  html += `<div class="overview-row">`;
  html += renderOverview("Last 7 days", data.overview_7d);
  html += renderOverview("Last 30 days", data.overview_30d);
  html += `</div>`;
  html += `<h3>Recent requests <button class="btn" id="requests-more" type="button">Load more</button></h3>
    <div class="table-wrap small"><table id="requests-table">
      <thead><tr><th>Time</th><th>Model</th><th>Provider</th><th>Status</th><th class="num">Latency</th><th class="num">Cost</th></tr></thead>
      <tbody id="requests-body"><tr><td colspan="6" class="hint">Loading…</td></tr></tbody>
    </table></div>`;
  body.innerHTML = html;
  $("#requests-more").addEventListener("click", () => loadRequests(false));
  loadRequests(true);
}

function requestCell(entry, names) {
  for (const n of names) {
    const v = entry?.[n];
    if (v !== undefined && v !== null) return v;
  }
  return null;
}

function renderRequestRows(entries, replace) {
  const tbody = $("#requests-body");
  if (!tbody) return;
  const rows = (entries || [])
    .map((e) => {
      const when = requestCell(e, ["created_at", "createdAt", "timestamp", "time", "started_at"]);
      const model = requestCell(e, ["model", "model_id"]) || "?";
      const provider = requestCell(e, ["provider", "seller", "routed_provider", "seller_provider"]) || "—";
      const status = requestCell(e, ["status", "state"]) || "—";
      const latency = requestCell(e, ["latency_ms", "duration_ms", "total_latency_ms", "ttft_ms"]);
      const cost = requestCell(e, ["cost_usd", "buyer_cost_usd", "total_cost_usd", "settled_usd"]);
      const costMicro = requestCell(e, ["cost_micro", "buyer_cost_micro", "settled_micro"]);
      const costShown = cost != null ? usd(cost) : costMicro != null ? usd(costMicro / 1e6) : "—";
      const whenShown = when ? new Date(typeof when === "number" && when < 1e12 ? when * 1000 : when).toLocaleString() : "—";
      const okCls = String(status).match(/ok|success|complete|settled|200/i) ? "pass" : String(status).match(/fail|error|refus/i) ? "fail" : "";
      return `<tr>
        <td class="reasons">${escapeHtml(whenShown)}</td>
        <td>${escapeHtml(String(model))}</td>
        <td>${escapeHtml(String(provider))}</td>
        <td class="${okCls}">${escapeHtml(String(status))}</td>
        <td class="num">${latency != null ? `${Math.round(latency).toLocaleString()} ms` : "—"}</td>
        <td class="num">${costShown}</td>
      </tr>`;
    })
    .join("");
  if (replace) tbody.innerHTML = rows || `<tr><td colspan="6" class="hint">No requests recorded yet.</td></tr>`;
  else tbody.insertAdjacentHTML("beforeend", rows);
}

async function loadRequests(replace) {
  if (replace) requestCursor = null;
  const params = new URLSearchParams({ limit: "25" });
  if (requestCursor) params.set("cursor", requestCursor);
  try {
    const data = await getJson(`${API}/account/requests?${params}`);
    if (data.available === false) {
      renderRequestRows([], true);
      const tbody = $("#requests-body");
      if (tbody)
        tbody.innerHTML = `<tr><td colspan="6" class="hint">Request log unavailable (${escapeHtml(data.reason || "?")}).</td></tr>`;
      return;
    }
    renderRequestRows(data.entries, replace);
    requestCursor = data.cursor || data.next_cursor || null;
    const more = $("#requests-more");
    if (more) more.disabled = !requestCursor;
  } catch (err) {
    toast(`Request log failed: ${err.message}`, "bad");
  }
}

async function loadAccount(force = false) {
  try {
    const data = await getJson(`${API}/account${force ? "?refresh=true" : ""}`);
    renderAccount(data);
  } catch (err) {
    $("#account-body").innerHTML = `<p class="hint fail">Could not load account data: ${escapeHtml(err.message)}</p>`;
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
        server: {
          host: $("#f-host").value.trim(),
          port: Number($("#f-port").value),
          open_browser: $("#f-open-browser").checked,
        },
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
      loadAccount(true);
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
      loadAccount(true);
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
  $("#account-refresh").addEventListener("click", (e) => {
    e.currentTarget.disabled = true;
    loadAccount(true).finally(() => {
      e.currentTarget.disabled = false;
    });
  });

  $("#model-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadModels, 250);
  });
  $("#model-passed-only").addEventListener("change", loadModels);
  $("#model-class").addEventListener("change", loadModels);
  $("#model-sort").addEventListener("change", loadModels);
  $("#model-dir").addEventListener("change", loadModels);
  const providerSel = $("#model-provider");
  if (providerSel) providerSel.addEventListener("change", () => {
    // provider filtering is client-side over the loaded page
    loadModels();
  });

  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
  });

  // keep the freshness badge honest without a page reload
  setInterval(() => {
    if (!document.hidden) getJson(`${API}/status`).then(renderStatus).catch(() => {});
  }, 20000);
}

wire();
loadAll().then(loadModels);
loadAccount();
