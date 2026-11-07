const state = {
  flats: [],
  flatId: null,
  page: "overview",
  mode: "balanced",
  usage: null,
  appliances: [],
  recommendations: [],
  market: null,
  quality: null,
  dailyNote: null,
  dailyMode: "balanced",
};

const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const number = (value, digits = 1) => Number(value || 0).toLocaleString("en-US", {
  minimumFractionDigits: digits,
  maximumFractionDigits: digits,
});
const hourLabel = (hour) => `${String(hour).padStart(2, "0")}:00`;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2800);
}

async function loadData() {
  const [usage, appliances, recommendations, market, quality, dailyNote] = await Promise.all([
    api(`/api/flats/${state.flatId}/usage?days=7`),
    api(`/api/flats/${state.flatId}/appliances`),
    api(`/api/flats/${state.flatId}/recommendations?mode=${state.mode}`),
    api(`/api/market/tomorrow?flat_id=${state.flatId}&mode=${state.mode}`),
    api("/api/data-quality"),
    api(`/api/flats/${state.flatId}/daily-note?mode=${state.dailyMode}`),
  ]);
  state.usage = usage;
  state.appliances = appliances.appliances;
  state.recommendations = recommendations.recommendations;
  state.market = market;
  state.quality = quality;
  state.dailyNote = dailyNote;
  $("#action-count").textContent = String(state.recommendations.length);
  render();
}

function setPage(page) {
  state.page = page;
  document.querySelectorAll(".nav-link").forEach((item) => item.classList.toggle("active", item.dataset.page === page));
  const title = {
    overview: ["A clearer view of your energy.", "A few practical actions can make a meaningful difference."],
    actions: ["Small actions. Real impact.", "Suggestions are calculated from this flat’s readings, occupancy and market data."],
    circuits: ["Get to know your circuits.", "Usage patterns suggest a likely appliance — you’re always in control of the label."],
    quality: ["Trust starts with good data.", "Every incoming record is cleaned and classified before it reaches your analytics."],
    daily: ["A useful nudge for tomorrow.", "A quick, data-backed timing suggestion from this flat’s usage patterns and next-day forecast."],
  }[page];
  $("#breadcrumb-current").textContent = page === "daily" ? "Daily nudge" : page === "quality" ? "Data quality" : page === "circuits" ? "Circuits" : page === "actions" ? "Your actions" : "Overview";
  $("#page-title").textContent = title[0];
  $("#page-description").textContent = title[1];
  render();
}

function chartSvg(series) {
  const width = 660, height = 190, left = 37, right = 10, top = 13, bottom = 25;
  const values = series.map((item) => item.energy_kwh);
  const max = Math.max(.05, ...values);
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const step = plotWidth / Math.max(1, series.length - 1);
  const points = series.map((item, index) => {
    const x = left + index * step;
    const y = top + plotHeight - (item.energy_kwh / max) * plotHeight;
    return [x, y];
  });
  const line = points.map((point, index) => `${index ? "L" : "M"}${point[0].toFixed(1)},${point[1].toFixed(1)}`).join(" ");
  const area = `${line} L${(left + plotWidth).toFixed(1)},${(top + plotHeight).toFixed(1)} L${left},${(top + plotHeight).toFixed(1)} Z`;
  const bars = series.filter((_, index) => index % 8 === 0).map((item, index) => {
    const x = left + index * 8 * step;
    const occupants = Math.max(0, Number(item.occupants_home || 0)) / Math.max(1, state.usage.flat.residents);
    return `<rect class="occupancy-bar" x="${x.toFixed(1)}" y="${(top + plotHeight - occupants * 12).toFixed(1)}" width="3" height="${(occupants * 12).toFixed(1)}" rx="1"/>`;
  }).join("");
  const grids = [0, .33, .66, 1].map((fraction) => {
    const y = top + plotHeight * fraction;
    return `<line class="chart-grid" x1="${left}" y1="${y}" x2="${width - right}" y2="${y}"/><text class="chart-label" x="0" y="${y + 3}">${number(max * (1 - fraction), 2)}</text>`;
  }).join("");
  const labels = [0, .25, .5, .75, 1].map((fraction) => {
    const idx = Math.min(series.length - 1, Math.round((series.length - 1) * fraction));
    const label = series[idx]?.timestamp_utc?.slice(5, 10) || "";
    return `<text class="chart-label" x="${left + plotWidth * fraction}" y="${height - 4}" text-anchor="${fraction === 0 ? "start" : fraction === 1 ? "end" : "middle"}">${label}</text>`;
  }).join("");
  return `<svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Hourly energy and occupancy for the last week"><defs><linearGradient id="usageGradient" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#83aa8b" stop-opacity=".25"/><stop offset="1" stop-color="#83aa8b" stop-opacity="0"/></linearGradient></defs>${grids}${bars}<path class="usage-fill" d="${area}"/><path class="usage-line" d="${line}"/>${labels}</svg>`;
}

function renderDetailedMarket() {
  const hours = state.market?.hours || [];
  if (!hours.length) return `<div class="empty-state">No hourly market data is available for this date.</div>`;
  const width = 900, height = 270, left = 58, right = 58, top = 23, bottom = 36;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const prices = hours.map((item) => item.spot_sek_per_kwh);
  const carbons = hours.map((item) => item.carbon_gco2e_per_kwh);
  const priceMin = Math.min(...prices), priceMax = Math.max(...prices);
  const carbonMin = Math.min(...carbons), carbonMax = Math.max(...carbons);
  const scaleY = (value, min, max) => top + plotHeight - ((value - min) / (max - min || 1)) * plotHeight;
  const x = (hour) => left + (hour / 23) * plotWidth;
  const pricePath = hours.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${scaleY(item.spot_sek_per_kwh, priceMin, priceMax).toFixed(1)}`).join(" ");
  const carbonPath = hours.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${scaleY(item.carbon_gco2e_per_kwh, carbonMin, carbonMax).toFixed(1)}`).join(" ");
  const windows = state.dailyNote.daily_note.window_summary;
  const highlighted = new Map();
  for (const [mode, point] of Object.entries(windows)) {
    if (!point) continue;
    highlighted.set(point.hour, [...(highlighted.get(point.hour) || []), mode]);
  }
  const points = hours.map((item, index) => {
    const modes = highlighted.get(index) || [];
    const title = `${hourLabel(item.hour_local)} Sweden time · ${number(item.spot_sek_per_kwh, 3)} SEK/kWh · ${number(item.carbon_gco2e_per_kwh, 1)} gCO₂e/kWh${modes.length ? ` · best ${modes.join(", ")}` : ""}`;
    const radius = modes.length ? 5 : 2.5;
    const extraClass = modes.length ? " highlighted-point" : "";
    return `<circle class="price-point${extraClass}" cx="${x(index).toFixed(1)}" cy="${scaleY(item.spot_sek_per_kwh, priceMin, priceMax).toFixed(1)}" r="${radius}"><title>${esc(title)}</title></circle><circle class="carbon-point${extraClass}" cx="${x(index).toFixed(1)}" cy="${scaleY(item.carbon_gco2e_per_kwh, carbonMin, carbonMax).toFixed(1)}" r="${radius}"><title>${esc(title)}</title></circle>`;
  }).join("");
  const grid = [0, .5, 1].map((fraction) => {
    const y = top + fraction * plotHeight;
    const priceValue = priceMax - fraction * (priceMax - priceMin);
    const carbonValue = carbonMax - fraction * (carbonMax - carbonMin);
    return `<line class="detail-grid" x1="${left}" y1="${y}" x2="${width - right}" y2="${y}"/><text class="detail-axis price-axis" x="${left - 8}" y="${y + 3}" text-anchor="end">${number(priceValue, 3)}</text><text class="detail-axis carbon-axis" x="${width - right + 8}" y="${y + 3}">${number(carbonValue, 1)}</text>`;
  }).join("");
  const ticks = [0, 4, 8, 12, 16, 20, 23].map((hour) =>
    `<text class="detail-axis" x="${x(hour)}" y="${height - 8}" text-anchor="middle">${hourLabel(hours[hour].hour_local)}</text>`
  ).join("");
  const modeChip = (mode, label) => {
    const point = windows[mode];
    return `<span class="window-chip ${mode}"><i></i>${label}: <strong>${point ? hourLabel(point.hour_local) : "n/a"} local</strong></span>`;
  };
  return `<div class="detail-chart-legend"><span><i class="detail-line price-line"></i>Price · SEK/kWh</span><span><i class="detail-line carbon-line"></i>CO₂ · gCO₂e/kWh</span></div>
    <svg class="detail-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="24-hour spot price and carbon intensity with cheapest, cleanest and balanced recommended hours">${grid}<path class="price-line" d="${pricePath}"/><path class="carbon-line" d="${carbonPath}"/>${points}${ticks}</svg>
    <div class="window-chip-row">${modeChip("cheapest", "Cheapest")}${modeChip("cleanest", "Cleanest")}${modeChip("balanced", "Best balance")}</div>`;
}

function miniAction(action) {
  const reduce = action.type === "reduce";
  const impact = reduce
    ? `${number(action.estimated_kwh_change, 1)} kWh`
    : `${action.estimated_sek_change >= 0 ? "−" : "+"}${number(Math.abs(action.estimated_sek_change), 2)} SEK`;
  return `<div class="action-item"><div class="action-symbol ${reduce ? "" : "shift"}">${reduce ? "↓" : "↗"}</div><div class="action-text"><strong>${esc(action.title)}</strong><span>${esc(action.explanation)}</span></div><div class="action-impact">${impact}</div></div>`;
}

function renderOverview() {
  const metrics = state.usage.metrics;
  const reduce = state.recommendations.filter((item) => item.type === "reduce").length;
  const change = metrics.week_change_percent;
  const changeLabel = change == null ? "No previous week available" : `${change > 0 ? "↑" : "↓"} ${Math.abs(change)}% vs previous week`;
  const seenApplianceTypes = new Set();
  const overviewRecommendations = state.recommendations.filter((item) => {
    const type = item.appliance_type?.toLowerCase();
    if (!type || seenApplianceTypes.has(type)) return false;
    seenApplianceTypes.add(type);
    return true;
  }).slice(0, 3);
  return `
    <div class="grid kpi-grid">
      <article class="card kpi"><div class="kpi-top"><span>Electricity this week</span><span class="kpi-icon">ϟ</span></div><div class="kpi-value">${number(metrics.energy_kwh, 1)}<small> kWh</small></div><div class="kpi-caption"><span class="${change > 0 ? "kpi-warm" : "kpi-accent"}">${esc(changeLabel)}</span></div></article>
      <article class="card kpi"><div class="kpi-top"><span>Estimated spot cost</span><span class="kpi-icon">kr</span></div><div class="kpi-value">${number(metrics.spot_cost_sek, 2)}<small> SEK</small></div><div class="kpi-caption">Spot-energy component only · excl. VAT</div></article>
      <article class="card kpi"><div class="kpi-top"><span>Operational emissions</span><span class="kpi-icon">♧</span></div><div class="kpi-value">${number(metrics.co2e_kg, 2)}<small> kg</small></div><div class="kpi-caption">Synthetic hourly carbon factors</div></article>
      <article class="card kpi"><div class="kpi-top"><span>Energy while away</span><span class="kpi-icon">⌁</span></div><div class="kpi-value">${number(metrics.unoccupied_kwh, 1)}<small> kWh</small></div><div class="kpi-caption">${reduce} reduce ${reduce === 1 ? "opportunity" : "opportunities"} · occupancy coverage ${number(metrics.occupancy_coverage_percent, 0)}%</div></article>
    </div>
    <div class="grid overview-grid">
      <article class="card">
        <div class="card-header"><div><div class="card-title">Your energy rhythm</div><div class="card-subtitle">Hourly electricity use across the selected week</div></div><div class="chart-legend"><span><i class="legend-dot"></i>Energy</span><span><i class="legend-dot soft"></i>Home</span></div></div>
        <div class="usage-chart-wrap">${chartSvg(state.usage.series)}</div>
        <div class="chart-note">Occupancy is a synthetic signal. <strong>${number(metrics.unoccupied_kwh, 2)} kWh</strong> coincided with an empty flat.</div>
      </article>
      <article class="card actions-card"><div class="section-header"><div><h2>Worth a look</h2><p>Your strongest opportunities</p></div><button class="text-button" data-go="actions">See all →</button></div>
        ${overviewRecommendations.length ? overviewRecommendations.map(miniAction).join("") : `<div class="empty-state">No strong opportunities found this week.</div>`}
      </article>
    </div>
    <article class="card market-card">
      <div class="card-header market-header"><div><div class="card-title">Hourly market</div><div class="card-subtitle">${esc(state.market.date_local)} · ${state.market.is_historical_scenario ? "historical scenario" : "Sweden local time"}</div></div><div class="market-header-actions"><div class="mode-tabs" role="group" aria-label="Optimization preference">${["cheapest", "balanced", "greenest"].map((mode) => `<button class="mode-button ${state.mode === mode ? "active" : ""}" data-mode="${mode}">${mode[0].toUpperCase() + mode.slice(1)}</button>`).join("")}</div></div></div>
      <div class="detailed-chart-body overview-market-chart">${renderDetailedMarket()}</div>
      ${state.market.is_historical_scenario ? `<p class="detail-source-note">Historical data ends before today; this is a modeled scenario, not a live forecast.</p>` : ""}
    </article>
    <article class="card quality-mini page-section"><div class="mini-head"><div><h2>Data quality</h2><p>Readings cleaned through the ingestion pipeline</p></div><div class="trust-number">${number(state.quality.trustworthy_percent, 1)}%</div></div><div class="quality-track"><span style="width:${state.quality.trustworthy_percent}%"></span></div><div class="quality-labels"><span><strong>${state.quality.raw_readings.toLocaleString()}</strong> raw records</span><span><strong>${state.quality.repaired.toLocaleString()}</strong> repaired</span><span><strong>${state.quality.rejected.toLocaleString()}</strong> rejected</span></div></article>`;
}

function renderActions() {
  const grouped = ["reduce", "shift"];
  return grouped.map((type) => {
    const list = state.recommendations.filter((item) => item.type === type);
    return `<section class="page-section"><div class="section-header"><div><h2>${type === "reduce" ? "Reduce · use less" : "Shift · use it at a better time"}</h2><p>${type === "reduce" ? "Potential consumption to avoid. Estimates include an explicit 50% avoidability assumption." : "Move a flexible load to a better hour. Shifting does not reduce kWh."}</p></div><span class="tag ${type === "shift" ? "shift-tag" : ""}">${list.length} ${list.length === 1 ? "action" : "actions"}</span></div>
      <div class="grid two-col">${list.length ? list.map((item) => `<article class="card recommendation-card"><div class="action-symbol ${type === "shift" ? "shift" : ""}">${type === "reduce" ? "↓" : "↗"}</div><div class="recommendation-body"><span class="tag ${type === "shift" ? "shift-tag" : ""}">${type}${type === "shift" && !item.confirmed ? " · likely label" : ""}</span><h3>${esc(item.title)}</h3><p>${esc(item.explanation)}</p><div class="impact-row">${type === "reduce" ? `<span><strong>${number(item.estimated_kwh_change, 2)}</strong> kWh avoided*</span>` : `<span><strong>0</strong> kWh change</span>`}<span><strong>${item.estimated_sek_change >= 0 ? "−" : "+"}${number(Math.abs(item.estimated_sek_change), 2)}</strong> SEK</span><span><strong>${item.estimated_co2e_change >= 0 ? "−" : "+"}${number(Math.abs(item.estimated_co2e_change), 3)}</strong> kg CO₂e</span></div></div></article>`).join("") : `<article class="card empty-state">No ${type} opportunities meet the current threshold.</article>`}</div></section>`;
  }).join("") + `<p class="projection-label">* Reduce opportunity is an estimate, not guaranteed savings. Spot costs exclude network fees, tax and VAT. SHIFT savings compare estimated use at two hours and never count as kWh reduction.</p>`;
}

function renderAppliances() {
  const options = ["Unknown appliance", "Dishwasher", "Washing machine", "Portable heater", "Refrigerator", "Cooking appliance", "Lighting", "Electronics"];
  return `<div class="grid appliance-grid">${state.appliances.map((item) => `<article class="card appliance-card"><div class="appliance-top"><span class="appliance-id">CIRCUIT · ${esc(item.appliance_id)}</span><span class="confidence">${Math.round(item.inference_confidence * 100)}% likely</span></div><div class="appliance-name">${esc(item.effective_type)}</div><div class="muted">${item.confirmed_type ? "Confirmed by student" : `Inferred · alternatives: ${item.candidates.slice(1).map((candidate) => `${esc(candidate.type)} ${Math.round(candidate.probability * 100)}%`).join(" · ")}`}</div><div class="appliance-stats"><div><small>WEEKLY ENERGY</small><strong>${number(item.weekly_kwh, 2)} kWh</strong></div><div><small>ACTIVE HOURS</small><strong>${item.active_hours}</strong></div><div><small>ACTIVE WHILE HOME</small><strong>${number(item.active_while_home_percent, 0)}%</strong></div></div><div class="appliance-confirm"><select aria-label="Confirm appliance type" id="confirm-${esc(item.appliance_id)}"><option value="">Choose a type…</option>${options.map((option) => `<option value="${esc(option)}" ${item.confirmed_type === option ? "selected" : ""}>${esc(option)}</option>`).join("")}</select><button data-confirm="${esc(item.appliance_id)}">Save label</button></div></article>`).join("")}</div>`;
}

function renderQuality() {
  const quality = state.quality;
  const stats = [
    ["Raw readings", quality.raw_readings, "from the synthetic source"],
    ["Valid", quality.valid, "usable without changes"],
    ["Repaired", quality.repaired, "deterministic fixes applied"],
    ["Suspicious", quality.suspicious, "retained, flagged for review"],
    ["Rejected", quality.rejected, "not used in analytics"],
  ];
  return `<div class="grid quality-grid page-section">${stats.map((item) => `<article class="card quality-stat"><span>${item[0]}</span><strong>${item[1].toLocaleString()}</strong><small>${item[2]}</small></article>`).join("")}</div>
    <article class="card page-section"><div class="section-header"><div><h2>From incoming reading to trusted insight</h2><p>Every CSV row and API reading uses the same normalizer before reaching SQLite. ${quality.supplemental_source_issues} incomplete market/context rows were separately logged and excluded, never replaced with zero.</p></div></div><div class="pipeline">${[["↗", "Incoming", "meter or CSV"],["⌕", "Validated", "clean & classify"],["▤", "SQLite", "persisted record"],["✳", "Analytics", "trusted data only"]].map((step, index) => `${index ? `<span class="pipeline-arrow">→</span>` : ""}<div class="pipeline-step"><div class="pipeline-icon">${step[0]}</div><strong>${step[1]}</strong><small>${step[2]}</small></div>`).join("")}</div></article>
    <article class="card large-card page-section"><h2>Recent ingestion activity</h2><p>Most recent classifications. Repaired values are normalized before they appear in flat analytics.</p><div class="event-list">${quality.recent_events.map((event) => `<div class="event-row"><span class="event-id">${esc(event.reading_id || "missing ID")}</span><span class="status-badge status-${esc(event.status)}">${esc(event.status)}</span><span class="event-note">${esc(event.reason || event.repairs.join(", ") || "No changes required")}</span></div>`).join("") || `<div class="empty-state">No events yet.</div>`}</div></article>`;
}

function renderDaily() {
  const response = state.dailyNote;
  if (!response?.daily_note) return `<article class="card empty-state">The daily note is not available yet.</article>`;
  const note = response.daily_note;
  const delivery = response.delivery;
  const currentMode = note.options[note.mode];
  const actions = currentMode.actions;
  const sentLabel = delivery.sent_today
    ? `Today's ${esc(delivery.mode)} note was simulated at ${esc(delivery.sent_at)} UTC.`
    : "Not sent today. Send a simulated in-app push to record today's delivery.";
  return `<div class="grid daily-layout">
    <section>
      <article class="card daily-note-card">
        <div class="daily-note-top"><span class="daily-push-badge"><span class="pulse-dot"></span> API-GENERATED · SIMULATED PUSH</span><span class="appliance-id">${note.is_historical_scenario ? "HISTORICAL SCENARIO" : "FORECAST"} · ${esc(note.forecast_date_local)} SWEDEN TIME</span></div>
        <div class="daily-note-icon">✳</div>
        <h2>${esc(note.title)}</h2>
        <p class="daily-message">${esc(note.message)}</p>
        <p class="daily-note-foot">${esc(note.note)}</p>
        <div class="daily-delivery-status">${delivery.sent_today ? "✓" : "◷"} ${sentLabel}</div>
        <button id="send-daily-button" class="button button-dark" ${delivery.sent_today ? "disabled" : ""}>${delivery.sent_today ? "Today's push simulated" : "Simulate today's push"}</button>
      </article>
      <p class="projection-label">This is an in-app simulation. The backend API records one simulated delivery per flat per UTC day; no OS notification permission, email service, or live push vendor is used.</p>
    </section>
    <section class="card daily-options-card">
      <div class="section-header"><div><h2>Choose what matters ${note.is_historical_scenario ? "for this modeled day" : "tomorrow"}</h2><p>Each mode is recomputed from hourly market data and this flat's recurring load patterns.</p></div></div>
      <div class="mode-tabs daily-mode-tabs" role="group" aria-label="Daily nudge optimization mode">${["cheapest", "balanced", "greenest"].map((mode) => `<button class="mode-button ${state.dailyMode === mode ? "active" : ""}" data-daily-mode="${mode}">${mode[0].toUpperCase() + mode.slice(1)}</button>`).join("")}</div>
      <div class="daily-window"><span>BEST START WINDOW · ${state.dailyMode.toUpperCase()}</span><strong>${currentMode.window ? `${hourLabel(currentMode.window.hour_local)} Sweden time` : "Unavailable"}</strong><small>${currentMode.window ? `${number(currentMode.window.spot_sek_per_kwh, 3)} SEK/kWh · ${number(currentMode.window.carbon_gco2e_per_kwh, 1)} gCO₂e/kWh` : "No forecast data"}</small></div>
      <div class="daily-actions">${actions.length ? actions.map((action) => `<article class="daily-action"><div class="daily-action-head"><div>      <span class="appliance-id">${esc(action.appliance_id)} · ${action.confirmed ? "CONFIRMED" : `${Math.round(action.confidence * 100)}% LIKELY`}</span><h3>Run the ${action.confirmed ? "" : "likely "}${esc(action.appliance_type.toLowerCase())} load around ${hourLabel(action.suggested_hour_local)} local time</h3></div><span class="action-symbol shift">↗</span></div><p>Usual start: ${hourLabel(action.usual_hour_local)} local time · typical run ${number(action.typical_run_kwh, 2)} kWh across ${action.runs_observed} distinct recent runs.</p><div class="impact-row"><span><strong>${action.estimated_sek_change >= 0 ? "−" : "+"}${number(Math.abs(action.estimated_sek_change), 2)}</strong> SEK</span><span><strong>${action.estimated_co2e_change_kg >= 0 ? "−" : "+"}${number(Math.abs(action.estimated_co2e_change_kg), 3)}</strong> kg CO₂e</span><span><strong>0</strong> kWh change</span></div></article>`).join("") : `<div class="empty-state">No recurring dishwasher or washing-machine run has a safe alternative hour that improves this mode. Confirm appliance labels or check again after more readings arrive.</div>`}</div>
    </section>
  </div>
  <article class="card detailed-market-card page-section">
    <div class="section-header"><div><h2>${state.dailyNote.daily_note.is_historical_scenario ? "Modeled day" : "Tomorrow"}, hour by hour</h2><p>Hourly values from the market API, plotted against separate price and carbon scales · Sweden local time.</p></div><span class="tag shift-tag">24 hourly points</span></div>
    <div class="detailed-chart-body">${renderDetailedMarket()}</div>
    <p class="detail-source-note">${state.dailyNote.daily_note.is_historical_scenario ? `This dataset ends before today. The displayed ${esc(state.dailyNote.daily_note.forecast_date_local)} is a historical scenario date, not tomorrow's real forecast.` : "Forecast values are returned by the backend market API."} When hourly source data is missing, the provider projects values from historical weekday/local-hour averages. These are estimates, not live market forecasts.</p>
  </article>`;
}

function render() {
  if (!state.usage) return;
  const content = {
    overview: renderOverview,
    actions: renderActions,
    circuits: renderAppliances,
    quality: renderQuality,
    daily: renderDaily,
  }[state.page]();
  $("#view-container").innerHTML = content;
  $("#view-container").querySelectorAll("[data-go]").forEach((button) => button.addEventListener("click", () => setPage(button.dataset.go)));
  $("#view-container").querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", async () => {
    state.mode = button.dataset.mode;
    try { await loadData(); } catch (error) { showToast(error.message); }
  }));
  $("#view-container").querySelectorAll("[data-daily-mode]").forEach((button) => button.addEventListener("click", async () => {
    state.dailyMode = button.dataset.dailyMode;
    try { await loadData(); } catch (error) { showToast(error.message); }
  }));
  $("#view-container").querySelector("#send-daily-button")?.addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      state.dailyNote = await api(`/api/flats/${state.flatId}/daily-notification`, {
        method: "POST",
        body: JSON.stringify({ mode: state.dailyMode }),
      });
      render();
      const status = state.dailyNote.delivery.status;
      showToast(status === "simulated" ? "Today's in-app energy push was simulated by the API." : "Today's energy note was already sent.");
    } catch (error) {
      event.currentTarget.disabled = false;
      showToast(error.message);
    }
  });
  $("#view-container").querySelectorAll("[data-confirm]").forEach((button) => button.addEventListener("click", async () => {
    const applianceId = button.dataset.confirm;
    const value = $(`#confirm-${CSS.escape(applianceId)}`).value;
    if (!value) return showToast("Choose an appliance label first.");
    try {
      await api(`/api/flats/${encodeURIComponent(state.flatId)}/appliances/${encodeURIComponent(applianceId)}`, {
        method: "PATCH",
        body: JSON.stringify({ confirmed_type: value }),
      });
      await loadData();
      showToast("Circuit label saved. Recommendations now use your label.");
    } catch (error) { showToast(error.message); }
  }));
}

async function start() {
  $("#date-label").textContent = new Intl.DateTimeFormat("en", { month: "short", day: "numeric", year: "numeric" }).format(new Date());
  try {
    const response = await api("/api/flats");
    state.flats = response.flats;
    state.flatId = state.flats[0]?.flat_id;
    $("#flat-select").innerHTML = state.flats.map((flat) => `<option value="${esc(flat.flat_id)}">${esc(flat.flat_id)} · ${flat.residents} residents</option>`).join("");
    await loadData();
  } catch (error) {
    $("#view-container").innerHTML = `<article class="card large-card"><h2>Couldn’t load the dashboard</h2><p>${esc(error.message)}. Check that the backend is running and refresh.</p></article>`;
  }
}

document.querySelectorAll(".nav-link").forEach((button) => button.addEventListener("click", () => setPage(button.dataset.page)));
$("#flat-select").addEventListener("change", async (event) => {
  state.flatId = event.target.value;
  try { await loadData(); } catch (error) { showToast(error.message); }
});
$("#refresh-button").addEventListener("click", async () => {
  try { await loadData(); showToast("Dashboard refreshed from SQLite."); } catch (error) { showToast(error.message); }
});
$("#replay-button").addEventListener("click", async () => {
  const button = $("#replay-button");
  const message = $("#replay-message");
  button.disabled = true;
  message.textContent = "Reading → validation → SQLite…";
  try {
    const result = await api("/api/demo/replay", { method: "POST", body: "{}" });
    await loadData();
    message.textContent = `${result.result.data_quality}: ${result.result.normalized_energy_kwh} kWh saved`;
    showToast("A repaired reading went through the real ingestion API.");
  } catch (error) {
    message.textContent = error.message;
    showToast(error.message);
  } finally { button.disabled = false; }
});
start();
