// Dashboard du bot — PAPER TRADING UNIQUEMENT.
// Le wallet est utilisé en LECTURE SEULE (adresse, réseau, solde) : aucune transaction
// n'est jamais demandée ni signée depuis cette interface.
"use strict";

(() => {
  const TOKEN_KEY = "ptb.token";
  const THEME_KEY = "ptb.theme.v2";
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* stockage indisponible */ } },
    remove(key) { try { localStorage.removeItem(key); } catch { /* stockage indisponible */ } },
  };
  const $ = (id) => document.getElementById(id);
  const state = {
    token: store.get(TOKEN_KEY), status: null, symbol: null, symbols: [], ws: null, wsRetry: 1000,
    table: "trades", lastEquityTime: 0, lastCandleTime: 0, priceLines: [], priceLineKey: "",
    symbolTrades: [], started: false, timers: {},
  };

  // --- Formatage ------------------------------------------------------------------------
  const formatters = new Map();
  const nf = (digits) => {
    if (!formatters.has(digits)) {
      formatters.set(digits, new Intl.NumberFormat("fr-FR", { minimumFractionDigits: digits, maximumFractionDigits: digits }));
    }
    return formatters.get(digits);
  };
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const fmtNum = (v, d = 2) => (isNum(v) ? nf(d).format(v) : "—");
  const fmtPrice = (v) => (isNum(v) ? nf(Math.abs(v) >= 1000 ? 2 : Math.abs(v) >= 1 ? 4 : 6).format(v) : "—");
  const fmtSigned = (v, d = 2) => (isNum(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + nf(d).format(Math.abs(v)) : "—");
  const fmtPct = (v, signed = false) => (isNum(v) ? (signed ? fmtSigned(v * 100) : nf(2).format(v * 100)) + " %" : "—");
  const signClass = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) : "—");
  const toSec = (iso) => Math.floor(new Date(iso).getTime() / 1000);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const debounce = (name, fn, ms) => { clearTimeout(state.timers[name]); state.timers[name] = setTimeout(fn, ms); };

  // --- API ---------------------------------------------------------------------------------
  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { Authorization: `Bearer ${state.token}`, "Content-Type": "application/json" },
    });
    if (response.status === 401) {
      askToken(true);
      throw new Error("Jeton refusé");
    }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || response.statusText);
    return body;
  }

  // --- Thème et graphiques -------------------------------------------------------------------
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  let priceChart, candleSeries, emaFastSeries, emaSlowSeries, equityChart, equitySeries;

  function chartOptions() {
    const panel = cssVar("--panel"), soft = cssVar("--soft"), muted = cssVar("--muted"), line = cssVar("--line");
    const accent = cssVar("--accent");
    return {
      autoSize: true,
      layout: { background: { type: "solid", color: panel }, textColor: muted, fontFamily: cssVar("--mono") },
      grid: { vertLines: { color: soft }, horzLines: { color: soft } },
      rightPriceScale: { borderColor: line },
      timeScale: { borderColor: line, timeVisible: true, secondsVisible: false },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { color: muted, labelBackgroundColor: accent },
        horzLine: { color: muted, labelBackgroundColor: accent },
      },
    };
  }

  function candleColors() {
    const up = cssVar("--up"), down = cssVar("--down");
    return { upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down };
  }

  function withAlpha(hex, alpha) {
    const value = parseInt(hex.replace("#", ""), 16);
    return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
  }

  function equityColors() {
    const up = cssVar("--up");
    return { lineColor: up, topColor: withAlpha(up, 0.35), bottomColor: withAlpha(up, 0.02) };
  }

  function initCharts() {
    if (!window.LightweightCharts) {
      $("price-chart").textContent = "Bibliothèque de graphiques indisponible (accès au CDN jsDelivr requis).";
      return;
    }
    const line = { lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    priceChart = LightweightCharts.createChart($("price-chart"), chartOptions());
    candleSeries = priceChart.addCandlestickSeries(candleColors());
    emaFastSeries = priceChart.addLineSeries({ ...line, color: cssVar("--accent") });
    emaSlowSeries = priceChart.addLineSeries({ ...line, color: cssVar("--accent-2"), lineStyle: LightweightCharts.LineStyle.Dashed });
    equityChart = LightweightCharts.createChart($("equity-chart"), chartOptions());
    equitySeries = equityChart.addAreaSeries({ ...equityColors(), lineWidth: 2, priceLineVisible: false });
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    store.set(THEME_KEY, theme);
    if (!priceChart) return;
    priceChart.applyOptions(chartOptions());
    equityChart.applyOptions(chartOptions());
    candleSeries.applyOptions(candleColors());
    emaFastSeries.applyOptions({ color: cssVar("--accent") });
    emaSlowSeries.applyOptions({ color: cssVar("--accent-2") });
    equitySeries.applyOptions(equityColors());
    setMarkers();
    state.priceLineKey = "";
    updatePriceLines();
  }

  // --- Graphique de prix ------------------------------------------------------------------
  async function loadCandles(symbol) {
    if (!candleSeries || !symbol) return;
    const data = await api(`/api/candles?symbol=${encodeURIComponent(symbol)}`);
    if (symbol !== state.symbol) return;
    candleSeries.setData(data.candles);
    state.lastCandleTime = data.candles.length ? data.candles[data.candles.length - 1].time : 0;
    const names = Object.keys(data.indicators).sort((a, b) => parseInt(a.slice(3), 10) - parseInt(b.slice(3), 10));
    emaFastSeries.setData(names[0] ? data.indicators[names[0]] : []);
    emaSlowSeries.setData(names[1] ? data.indicators[names[1]] : []);
    state.symbolTrades = data.trades;
    setMarkers();
    $("timeframe").textContent = data.timeframe;
    const last = data.candles[data.candles.length - 1];
    if (last && !isNum(state.status?.prices[symbol])) $("last-price").textContent = fmtPrice(last.close);
    state.priceLineKey = "";
    updatePriceLines();
  }

  function openPosition() {
    return (state.status?.positions || []).find((p) => p.symbol === state.symbol);
  }

  function setMarkers() {
    if (!candleSeries) return;
    const up = cssVar("--up"), down = cssVar("--down");
    const entry = (time, long) => ({ time, position: long ? "belowBar" : "aboveBar", color: long ? up : down, shape: long ? "arrowUp" : "arrowDown", text: long ? "L" : "S" });
    const markers = [];
    for (const t of state.symbolTrades) {
      const long = t.direction === "LONG";
      markers.push(entry(toSec(t.entry_time), long));
      markers.push({ time: toSec(t.timestamp), position: long ? "aboveBar" : "belowBar", color: t.net_pnl >= 0 ? up : down, shape: "circle", text: fmtSigned(t.net_pnl, 0) });
    }
    const p = openPosition();
    if (p) markers.push(entry(toSec(p.entry_time), p.direction === "LONG"));
    markers.sort((a, b) => a.time - b.time);
    candleSeries.setMarkers(markers);
  }

  function updatePriceLines() {
    if (!candleSeries) return;
    const p = openPosition();
    const key = p ? [p.id, p.stop_loss, p.take_profit, p.tp1_done].join("|") : "";
    if (key === state.priceLineKey) return;
    state.priceLineKey = key;
    state.priceLines.forEach((line) => candleSeries.removePriceLine(line));
    state.priceLines = [];
    if (!p) return;
    const style = LightweightCharts.LineStyle;
    const add = (price, title, lineStyle, color) => state.priceLines.push(
      candleSeries.createPriceLine({ price, title, lineStyle, color, lineWidth: 1, axisLabelVisible: true }));
    add(p.entry_price, "Entrée", style.Solid, cssVar("--accent"));
    add(p.stop_loss, p.trailing_active ? "Trailing SL" : "SL", style.Dashed, cssVar("--down"));
    add(p.take_profit, "TP", style.Dashed, cssVar("--up"));
    if (p.tp1_price && !p.tp1_done) add(p.tp1_price, "TP1", style.Dotted, cssVar("--violet"));
    setMarkers();
  }

  function onCandle(c) {
    if (!candleSeries || c.symbol !== state.symbol || c.time < state.lastCandleTime) return;
    if (!state.lastCandleTime) {  // graphique vide : charger tout l'historique
      debounce("candles", () => loadCandles(state.symbol).catch(console.error), 300);
      return;
    }
    candleSeries.update({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });
    state.lastCandleTime = c.time;
    if (c.closed) debounce("candles", () => loadCandles(state.symbol).catch(console.error), 1500);
  }

  // --- Equity -------------------------------------------------------------------------------
  async function loadEquity() {
    const rows = await api("/api/equity?limit=5000");
    const byTime = new Map();
    rows.forEach((r) => byTime.set(toSec(r.timestamp), r.equity));
    const data = [...byTime.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value }));
    $("equity-meta").textContent = data.length ? `${data.length} points` : "Pas encore de données";
    if (!equitySeries) return;
    equitySeries.setData(data);
    state.lastEquityTime = data.length ? data[data.length - 1].time : 0;
    equityChart.timeScale().fitContent();
  }

  function onEquity(snapshot) {
    const time = toSec(snapshot.timestamp);
    if (!equitySeries || time < state.lastEquityTime) return;
    equitySeries.update({ time, value: snapshot.equity });
    state.lastEquityTime = time;
  }

  // --- État du bot --------------------------------------------------------------------------
  function renderSymbolTabs() {
    $("symbol-tabs").innerHTML = state.symbols.map((s) =>
      `<button class="tab${s === state.symbol ? " active" : ""}" data-symbol="${esc(s)}">${esc(s)}</button>`).join("");
  }

  function setText(id, text, cls = "") {
    const el = $(id);
    el.textContent = text;
    el.className = cls;
  }

  function renderStatus(s) {
    state.status = s;
    if (state.symbols.join() !== s.bot.symbols.join()) {
      state.symbols = s.bot.symbols;
      if (!state.symbols.includes(state.symbol)) state.symbol = state.symbols[0];
      renderSymbolTabs();
    }
    const status = s.bot.status;
    $("bot-status").textContent = status;
    $("bot-status").className = "status" + (status === "RUNNING" ? " running" : status === "HALTED" ? " halted" : "");
    document.querySelector('[data-action="start"]').disabled = status === "RUNNING" || status === "HALTED";
    document.querySelector('[data-action="pause"]').disabled = status !== "RUNNING";
    document.querySelector('[data-action="stop"]').disabled = status === "STOPPED";
    $("reset-halt").hidden = status !== "HALTED";

    const a = s.account, cur = a.currency, perf = s.performance;
    setText("k-initial", `${fmtNum(a.initial_capital)} ${cur}`);
    setText("k-balance", `${fmtNum(a.balance)} ${cur}`);
    setText("k-equity", `${fmtNum(a.equity)} ${cur}`);
    setText("k-pnl", `${fmtSigned(a.pnl)} ${cur}`, signClass(a.pnl));
    setText("k-pnl-pct", fmtPct(a.pnl_pct, true), signClass(a.pnl_pct));
    setText("k-dd", fmtPct(a.drawdown));
    setText("k-trades", String(perf.trades));
    setText("k-winrate", perf.trades ? fmtPct(perf.win_rate) : "—");
    setText("k-pf", perf.profit_factor == null ? (perf.wins ? "∞" : "—") : fmtNum(perf.profit_factor));
    if (isNum(s.prices[state.symbol])) $("last-price").textContent = fmtPrice(s.prices[state.symbol]);

    renderPositions(s);
    renderRisk(s);
    renderAI(s);
    renderClaude(s);
    renderClock(s);
    updatePriceLines();
  }

  function renderClaude(s) {
    const ai = s.ai;
    $("ai-mode").textContent = ai.enabled ? "mode IA actif" : `mode ${ai.mode === "ai" ? "IA inactif (clé absente)" : "règles"}`;
    const c = ai.costs;
    facts("ai-live", [
      ["Décisions", ai.enabled ? ai.decision_model : "désactivées"],
      ["Revues / actualités", `${ai.review_model} / ${ai.briefing_model}`],
      ["Confiance minimale", fmtNum(ai.min_confidence, 2)],
      ["Budget API du jour", `${fmtNum(c.spent_today_usd, 2)} / ${fmtNum(c.daily_budget_usd, 2)} $`],
    ]);
    $("ai-analyze").disabled = !ai.enabled || s.bot.status !== "RUNNING";
    $("ai-briefing").disabled = !ai.enabled;
  }

  function renderClock(s) {
    const m = s.market_clock, b = s.ai.briefing;
    $("clock-utc").textContent = new Date(m.utc).toISOString().slice(11, 16) + " UTC";
    facts("clock", [
      ["Sessions ouvertes", m.active_sessions.join(", ") || "aucune"],
      ["Liquidité", m.liquidity],
      ["Wall Street", m.us_equity_open ? `ouverte (${m.minutes_since_us_open} min)` : `fermée — ouverture ${fmtTime(m.next_us_open)}`],
      ["Jour férié US", m.us_holiday || "non"],
      ["Future BTC CME", m.cme_btc_futures_open ? "ouvert" : "fermé"],
      ["Blackout d'annonce", b && b.blackout ? b.blackout.reason : "aucun"],
    ]);
    if (!b) {
      $("briefing").innerHTML = '<p class="muted">Aucun briefing pour le moment.</p>';
      return;
    }
    const events = b.events.map((e) => `<div class="row"><span>${e.time_utc ? fmtTime(e.time_utc) : "—"} ${esc(e.name)}</span><span class="tag impact-${esc(e.impact)}">${esc(e.impact)}</span></div>`).join("");
    const views = b.per_symbol.map((v) => `<span class="tag bias-${esc(v.bias)}">${esc(v.symbol)} ${esc(v.bias)}</span>`).join(" ");
    $("briefing").innerHTML = `<div class="card"><div class="row head"><span>Briefing ${fmtTime(b.generated_at)}</span>
      <span>risque ${esc(b.risk_level)} · sentiment ${fmtSigned(b.sentiment, 2)}</span></div>
      <div>${views}</div>${events}<details><summary>Synthèse</summary><p>${esc(b.summary)}</p></details></div>`;
  }

  async function loadDecisions() {
    const data = await api("/api/ai/decisions?limit=8");
    $("ai-decisions").innerHTML = data.decisions.map((d) => `<div class="card">
      <div class="row head"><span>${esc(d.symbol)} ${esc(d.action)}${d.confidence != null ? ` · ${fmtNum(d.confidence, 2)}` : ""}</span>
      <span class="tag status-${esc(d.status)}">${esc(d.status)}</span></div>
      <div class="row muted"><span>${fmtTime(d.timestamp)} · ${esc(d.trigger)}</span><span class="${signClass(d.outcome_r)}">${d.outcome_r != null ? fmtSigned(d.outcome_r, 2) + " R" : ""}</span></div>
      <p>${esc(d.thesis || d.status_reason)}</p></div>`).join("") || '<p class="muted small">Aucune décision pour le moment.</p>';
  }

  function renderPositions(s) {
    const cur = s.account.currency;
    $("positions").innerHTML = s.positions.length ? s.positions.map((p) => `
      <div class="card ${p.direction === "LONG" ? "long" : "short"}">
        <div class="row head"><span>${esc(p.symbol)} ${esc(p.direction)}</span>
          <span class="${signClass(p.unrealized_pnl)}">${p.unrealized_pnl >= 0 ? "▲" : "▼"} ${fmtSigned(p.unrealized_pnl)} ${esc(cur)} (${fmtPct(p.unrealized_pct, true)})</span></div>
        <div class="row"><span>Qté ${fmtNum(p.quantity, 4)}</span><span>Entrée ${fmtPrice(p.entry_price)}</span></div>
        <div class="row"><span>${p.trailing_active ? "Trailing SL" : "SL"} ${fmtPrice(p.stop_loss)}</span><span>TP ${fmtPrice(p.take_profit)}</span></div>
        ${p.tp1_price ? `<div class="row"><span>TP1 ${fmtPrice(p.tp1_price)} (${fmtNum(p.tp1_fraction * 100, 0)} %)</span><span>${p.tp1_done ? "encaissé" : p.breakeven_done ? "point mort" : "en attente"}</span></div>` : ""}
        ${p.confidence != null ? `<div class="row muted"><span>Décision Claude #${p.decision_id}</span><span>confiance ${fmtNum(p.confidence, 2)}</span></div>` : ""}
        <div class="row muted"><span>Dernier ${fmtPrice(p.last_price)}</span><span>${fmtTime(p.entry_time)}</span></div>
      </div>`).join("") : '<p class="muted">Aucune position.</p>';
    $("pending").innerHTML = s.pending_orders.map((o) =>
      `<div class="card"><div class="row"><span>Ordre #${o.seq} ${esc(o.order_type)} ${esc(o.side)} ${esc(o.symbol)}</span><span>en attente</span></div></div>`).join("");
  }

  function facts(id, rows) {
    $(id).innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  }

  function renderRisk(s) {
    const r = s.risk, l = r.limits;
    facts("risk", [
      ["Kill-switch", r.halted ? `ACTIF — ${r.halt_reason}` : "inactif"],
      ["Nouvelles entrées", r.entries_blocked ? `bloquées` : "autorisées"],
      ["Perte du jour", `${fmtPct(r.daily_loss)} / ${fmtPct(l.max_daily_loss)}`],
      ["Perte de la semaine", `${fmtPct(r.weekly_loss)} / ${fmtPct(l.max_weekly_loss)}`],
      ["Risque ouvert", `${fmtPct(r.open_risk.total)} / ${fmtPct(l.max_portfolio_risk)}`],
      ["Risque max par trade", fmtPct(l.hard_max_risk_per_trade)],
      ["Drawdown", `${fmtPct(s.account.drawdown)} / ${fmtPct(l.max_drawdown)}`],
      ["Positions max", String(l.max_open_positions)],
      ["Frais / slippage", `${fmtPct(l.fee_rate)} / ${fmtPct(l.slippage_rate)}`],
      ["Source des signaux", s.bot.signal_source],
      ["Dernier cycle", s.bot.last_error ? `erreur : ${s.bot.last_error}` : fmtTime(s.bot.last_tick)],
    ]);
  }

  function renderAI(s) {
    const o = s.optimizer, p = s.params, st = p.strategy, ex = p.exits;
    facts("ai", [
      ["Modèle", o.model || "non configuré"],
      ["Disponible", o.available ? "oui" : "non (ANTHROPIC_API_KEY)"],
      ["Planification", o.scheduled ? `toutes les ${o.interval_hours} h` : "manuelle"],
      ["Application auto", o.auto_apply ? "oui (après validation)" : "non (approbation)"],
      ["État", o.running ? "optimisation en cours…" : "au repos"],
      ["Stratégie", `EMA ${st.ema_fast}/${st.ema_slow} · RSI ${st.rsi_length} (${st.rsi_long_threshold}/${st.rsi_short_threshold}) · ATR ${st.atr_length}`],
      ["Sorties", `SL ${ex.stop_loss_atr_multiplier}×ATR · TP ${ex.take_profit_risk_reward}R · trailing ${ex.trailing_stop_enabled ? "on" : "off"}`],
    ]);
    $("optimize-now").disabled = !o.available || o.running;
  }

  async function loadOptimizer() {
    const data = await api("/api/optimizer");
    const runs = data.runs.map((r) => `<div class="card"><div class="row"><span>#${r.id} ${esc(r.status)}</span>
      <span>${fmtTime(r.started_at)}</span></div><div class="muted">${esc(r.summary || r.error || "")}</div></div>`);
    const proposed = data.versions.filter((v) => v.status === "proposed").map((v) => `<div class="card">
      <div class="row"><span>Version #${v.id} proposée</span><button data-activate="${v.id}">Activer</button></div>
      <div class="muted">${esc(v.rationale)}</div></div>`);
    $("ai-runs").innerHTML = [...proposed, ...runs].join("") || '<p class="muted small">Aucune optimisation pour le moment.</p>';
  }

  // --- Tableaux d'historique ------------------------------------------------------------------
  const TABLES = {
    trades: {
      url: "/api/trades?limit=100",
      head: ["Sortie", "Symbole", "Sens", "Type", "Entrée", "Sortie px", "Qté", "SL", "TP", "Frais", "Slippage", "P&L brut", "P&L net", "Raison"],
      row: (t) => [fmtTime(t.timestamp), esc(t.symbol), esc(t.direction), esc(t.order_type), fmtPrice(t.entry_price),
        fmtPrice(t.exit_price), fmtNum(t.quantity, 4), fmtPrice(t.stop_loss), fmtPrice(t.take_profit), fmtNum(t.fees),
        fmtNum(t.slippage), fmtSigned(t.gross_pnl), `<span class="${signClass(t.net_pnl)}">${fmtSigned(t.net_pnl)}</span>`, esc(t.reason)],
    },
    signals: {
      url: "/api/signals?limit=100",
      head: ["Heure", "Symbole", "Sens", "Prix", "Source", "Statut", "Raison"],
      row: (s) => [fmtTime(s.timestamp), esc(s.symbol), esc(s.direction), fmtPrice(s.price), esc(s.source), esc(s.status), esc(s.reason)],
    },
    decisions: {
      url: "/api/ai/decisions?limit=100",
      pick: (data) => data.decisions,
      head: ["Heure", "Symbole", "Déclencheur", "Action", "Confiance", "Statut", "Résultat", "Thèse / raison"],
      row: (d) => [fmtTime(d.timestamp), esc(d.symbol), esc(d.trigger), esc(d.action),
        d.confidence != null ? fmtNum(d.confidence, 2) : "—", esc(d.status),
        d.outcome_r != null ? `<span class="${signClass(d.outcome_r)}">${fmtSigned(d.outcome_r, 2)} R</span>` : "—",
        esc((d.thesis || d.status_reason || "").slice(0, 160))],
    },
    events: {
      url: "/api/events?limit=150",
      head: ["Heure", "Niveau", "Type", "Message"],
      row: (e) => [fmtTime(e.timestamp), esc(e.level), esc(e.event_type), esc(e.message)],
    },
  };

  async function loadTable() {
    const spec = TABLES[state.table];
    const data = await api(spec.url);
    const rows = spec.pick ? spec.pick(data) : data;
    const head = `<thead><tr>${spec.head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>`;
    const body = rows.length
      ? rows.map((r) => `<tr>${spec.row(r).map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")
      : `<tr><td colspan="${spec.head.length}" class="muted">Aucune donnée.</td></tr>`;
    $("history-table").innerHTML = head + `<tbody>${body}</tbody>`;
  }

  // --- Wallet (lecture seule) -------------------------------------------------------------------
  const CHAINS = { "0x1": "Ethereum", "0xa": "Optimism", "0x38": "BNB Chain", "0x89": "Polygon", "0x2105": "Base",
    "0xa4b1": "Arbitrum One", "0xaa36a7": "Sepolia (test)" };

  async function readWallet() {
    const provider = window.ethereum;
    if (!provider) {
      facts("wallet", [["État", "MetaMask non détecté dans ce navigateur"]]);
      return;
    }
    try {
      const accounts = await provider.request({ method: "eth_requestAccounts" });
      const chainId = await provider.request({ method: "eth_chainId" });
      const balance = await provider.request({ method: "eth_getBalance", params: [accounts[0], "latest"] });
      facts("wallet", [
        ["Adresse", accounts[0]],
        ["Réseau", CHAINS[chainId] || chainId],
        ["Solde natif", `${fmtNum(Number(BigInt(balance)) / 1e18, 6)}`],
        ["Mode", "lecture seule — trading réel désactivé"],
      ]);
      if (!state.walletListeners) {
        provider.on?.("accountsChanged", readWallet);
        provider.on?.("chainChanged", readWallet);
        state.walletListeners = true;
      }
    } catch (error) {
      facts("wallet", [["Erreur", error.message || String(error)]]);
    }
  }

  // --- Temps réel ----------------------------------------------------------------------------
  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(state.token)}`);
    state.ws = ws;
    ws.onopen = () => { $("ws-state").classList.add("on"); state.wsRetry = 1000; };
    ws.onmessage = (event) => handleMessage(JSON.parse(event.data));
    ws.onclose = (event) => {
      $("ws-state").classList.remove("on");
      state.ws = null;
      if (event.code === 1008) { askToken(true); return; }
      setTimeout(connectWs, state.wsRetry);
      state.wsRetry = Math.min(state.wsRetry * 2, 15000);
    };
  }

  function handleMessage(message) {
    const refresh = (name, fn) => debounce(name, () => fn().catch(console.error), 400);
    switch (message.type) {
      case "status": renderStatus(message.data); break;
      case "candle": onCandle(message.data); break;
      case "equity": onEquity(message.data); break;
      case "trade":
        refresh("candles", () => loadCandles(state.symbol));
        if (state.table === "trades") refresh("table", loadTable);
        break;
      case "signal":
        if (state.table === "signals") refresh("table", loadTable);
        break;
      case "ai_decision":
        refresh("decisions", loadDecisions);
        if (state.table === "decisions") refresh("table", loadTable);
        break;
      case "optimization":
        refresh("optimizer", loadOptimizer);
        if (state.table === "events") refresh("table", loadTable);
        break;
      case "bot": case "risk":
        if (state.table === "events") refresh("table", loadTable);
        break;
      default: break;
    }
  }

  // --- Démarrage et interactions ---------------------------------------------------------------
  function askToken(error = false) {
    $("login-error").hidden = !error;
    if (!$("login").open) $("login").showModal();
  }

  async function start() {
    const status = await api("/api/status");
    store.set(TOKEN_KEY, state.token);
    state.symbol = state.symbol || status.bot.symbols[0];
    renderStatus(status);
    renderSymbolTabs();
    await Promise.all([loadCandles(state.symbol), loadEquity(), loadTable(), loadOptimizer(), loadDecisions()]);
    if (!state.ws) connectWs();
    state.started = true;
  }

  function bindEvents() {
    $("login-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      state.token = $("token-input").value.trim();
      try {
        await start();
        $("login").close();
      } catch {
        store.remove(TOKEN_KEY);
        $("login-error").hidden = false;
      }
    });
    document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await api(`/api/bot/${button.dataset.action}`, { method: "POST" });
      } catch (error) {
        window.alert(error.message);
      }
    }));
    $("symbol-tabs").addEventListener("click", (event) => {
      const symbol = event.target.closest("[data-symbol]")?.dataset.symbol;
      if (!symbol || symbol === state.symbol) return;
      state.symbol = symbol;
      renderSymbolTabs();
      $("last-price").textContent = fmtPrice(state.status?.prices[symbol]);
      loadCandles(symbol).catch(console.error);
    });
    document.querySelectorAll("[data-table]").forEach((tab) => tab.addEventListener("click", () => {
      document.querySelectorAll("[data-table]").forEach((t) => t.classList.toggle("active", t === tab));
      state.table = tab.dataset.table;
      loadTable().catch(console.error);
    }));
    $("optimize-now").addEventListener("click", async () => {
      $("optimize-now").disabled = true;
      try { await api("/api/optimizer/run", { method: "POST" }); } catch (error) { window.alert(error.message); }
    });
    $("ai-runs").addEventListener("click", async (event) => {
      const id = event.target.closest("[data-activate]")?.dataset.activate;
      if (!id || !window.confirm(`Activer la version de paramètres #${id} ?`)) return;
      try {
        await api(`/api/optimizer/versions/${id}/activate`, { method: "POST" });
        await loadOptimizer();
      } catch (error) { window.alert(error.message); }
    });
    $("wallet-connect").addEventListener("click", readWallet);
    $("ai-analyze").addEventListener("click", async () => {
      $("ai-analyze").disabled = true;
      try {
        const r = await api(`/api/ai/analyze/${encodeURIComponent(state.symbol)}`, { method: "POST" });
        $("ai-analyze").textContent = r.kind === "review" ? "Revue en cours…" : "Analyse en cours…";
        setTimeout(() => { $("ai-analyze").textContent = "Analyser maintenant"; }, 30000);
      } catch (error) { window.alert(error.message); }
    });
    $("ai-briefing").addEventListener("click", async () => {
      $("ai-briefing").disabled = true;
      try { await api("/api/ai/briefing", { method: "POST" }); } catch (error) { window.alert(error.message); }
    });
    $("theme-toggle").addEventListener("click", () =>
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  }

  document.documentElement.dataset.theme = store.get(THEME_KEY) || "dark";
  initCharts();
  bindEvents();
  if (state.token) start().catch(() => askToken(false)); else askToken(false);
})();
