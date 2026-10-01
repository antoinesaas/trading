// Dashboard du bot — PAPER TRADING UNIQUEMENT.
// Graphique au style TradingView (Lightweight Charts v5) : bougies en direct, volume, EMA, RSI,
// supports/résistances, structure de marché, entrées/sorties du bot, décisions de Claude et zone
// de la position ouverte (stop et objectif).
// Le wallet est utilisé en LECTURE SEULE (adresse, réseau, solde) : aucune transaction
// n'est jamais demandée ni signée depuis cette interface.
"use strict";

(() => {
  const TOKEN_KEY = "ptb.token";
  const THEME_KEY = "ptb.theme.v2";
  const VIEW_KEY = "ptb.view.v3";
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* stockage indisponible */ } },
    remove(key) { try { localStorage.removeItem(key); } catch { /* stockage indisponible */ } },
  };
  const $ = (id) => document.getElementById(id);
  const LWC = window.LightweightCharts;
  const TF_SECONDS = { "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800 };
  const CLASS_LABELS = { crypto: "Crypto", stock: "Actions", etf: "ETF et indices", forex: "Forex" };
  const EXIT_LABELS = { STOP_LOSS: "SL", TAKE_PROFIT: "TP", TAKE_PROFIT_1: "TP1", TRAILING_STOP: "Trailing", MARKET: "Sortie" };

  // Lien ouvert par « Lancer le bot.bat » : …/dashboard/#token=… (le fragment n'est jamais envoyé au serveur).
  const hashToken = (location.hash.match(/token=([^&]+)/) || [])[1];
  if (hashToken) {
    store.set(TOKEN_KEY, decodeURIComponent(hashToken));
    history.replaceState(null, "", location.pathname + location.search);
  }
  window.addEventListener("hashchange", () => { if (/token=/.test(location.hash)) location.reload(); });
  const saved = (() => { try { return JSON.parse(store.get(VIEW_KEY) || "{}"); } catch { return {}; } })();
  const state = {
    token: store.get(TOKEN_KEY), status: null, symbol: saved.symbol || null, tf: saved.tf || "1h",
    toggles: { ema: true, volume: true, rsi: true, levels: true, structure: true, ai: true, ...(saved.toggles || {}) },
    ws: null, wsRetry: 1000, table: "trades", lastEquityTime: 0, timers: {},
    chart: null, bars: [], viewKey: "", maps: { vol: new Map(), emaF: new Map(), emaS: new Map(), rsi: new Map() },
    binance: null, binanceLive: false, lastUpdate: 0, watchlist: [], priceLines: { levels: [], position: [] },
    positionKey: "", trend: "",
  };
  const saveView = () => store.set(VIEW_KEY, JSON.stringify({ symbol: state.symbol, tf: state.tf, toggles: state.toggles }));

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
  const fmtSigned = (v, d = 2) => (isNum(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + nf(d).format(Math.abs(v)) : "—");
  const fmtPct = (v, signed = false) => (isNum(v) ? (signed ? fmtSigned(v * 100) : nf(2).format(v * 100)) + " %" : "—");
  const signClass = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) : "—");
  const toSec = (iso) => Math.floor(new Date(iso).getTime() / 1000);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const debounce = (name, fn, ms) => { clearTimeout(state.timers[name]); state.timers[name] = setTimeout(fn, ms); };

  function precisionFor(price, assetClass) {
    if (!isNum(price)) return 2;
    if (assetClass === "forex") return price < 20 ? 5 : 3;
    return price < 1 ? 6 : price < 10 ? 4 : 2;
  }
  const fmtPrice = (v, assetClass) => (isNum(v) ? nf(precisionFor(v, assetClass)).format(v) : "—");
  const fmtVolume = (v) => {
    if (!isNum(v)) return "—";
    const units = [[1e9, " Md"], [1e6, " M"], [1e3, " k"]];
    for (const [size, suffix] of units) if (Math.abs(v) >= size) return nf(2).format(v / size) + suffix;
    return nf(v < 10 ? 4 : 0).format(v);
  };

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

  // --- Thème ---------------------------------------------------------------------------------
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  function withAlpha(hex, alpha) {
    const value = parseInt(hex.replace("#", ""), 16);
    return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
  }
  const colors = () => ({
    bg: cssVar("--bg"), panel: cssVar("--panel"), fg: cssVar("--fg"), muted: cssVar("--muted"), line: cssVar("--line"),
    up: cssVar("--up"), down: cssVar("--down"), accent: cssVar("--accent"), orange: cssVar("--orange"), violet: cssVar("--violet"),
  });

  function chartOptions(background) {
    const c = colors();
    const grid = withAlpha(c.muted, 0.12);
    return {
      autoSize: true,
      layout: {
        background: { type: "solid", color: background || c.bg }, textColor: c.muted, fontSize: 11,
        fontFamily: cssVar("--sans"), attributionLogo: false,
        panes: { separatorColor: c.line, separatorHoverColor: withAlpha(c.accent, 0.3), enableResize: true },
      },
      grid: { vertLines: { color: grid }, horzLines: { color: grid } },
      rightPriceScale: { borderColor: c.line },
      timeScale: { borderColor: c.line, timeVisible: true, secondsVisible: false, rightOffset: 8 },
      crosshair: {
        mode: LWC.CrosshairMode.Normal,
        vertLine: { color: c.muted, labelBackgroundColor: c.panel, style: LWC.LineStyle.Dashed },
        horzLine: { color: c.muted, labelBackgroundColor: c.panel, style: LWC.LineStyle.Dashed },
      },
      localization: { locale: "fr-FR" },
    };
  }

  // --- Graphique principal ----------------------------------------------------------------------
  let chart, candleSeries, volumeSeries, emaFastSeries, emaSlowSeries, structureSeries, tpZone, slZone, rsiSeries;
  let candleMarkers, structureMarkers, equityChart, equitySeries;
  const quiet = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };

  function zoneOptions(color) {
    const fill = withAlpha(color, 0.16);
    return { ...quiet, baseValue: { type: "price", price: 0 }, lineVisible: false, lineWidth: 1,
      topLineColor: "transparent", bottomLineColor: "transparent",
      topFillColor1: fill, topFillColor2: fill, bottomFillColor1: fill, bottomFillColor2: fill };
  }

  function candleColors() {
    const c = colors();
    return { upColor: c.up, downColor: c.down, borderUpColor: c.up, borderDownColor: c.down, wickUpColor: c.up, wickDownColor: c.down };
  }

  function initCharts() {
    if (!LWC) {
      $("chart").textContent = "Bibliothèque de graphiques indisponible (accès au CDN jsDelivr requis).";
      return;
    }
    const c = colors();
    chart = LWC.createChart($("chart"), chartOptions());
    tpZone = chart.addSeries(LWC.BaselineSeries, zoneOptions(c.up));
    slZone = chart.addSeries(LWC.BaselineSeries, zoneOptions(c.down));
    volumeSeries = chart.addSeries(LWC.HistogramSeries, { ...quiet, priceFormat: { type: "volume" }, priceScaleId: "volume" });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    candleSeries = chart.addSeries(LWC.CandlestickSeries, candleColors());
    emaFastSeries = chart.addSeries(LWC.LineSeries, { ...quiet, color: c.accent, lineWidth: 1 });
    emaSlowSeries = chart.addSeries(LWC.LineSeries, { ...quiet, color: c.orange, lineWidth: 1 });
    structureSeries = chart.addSeries(LWC.LineSeries, { ...quiet, color: withAlpha(c.violet, 0.8), lineWidth: 1,
      lineStyle: LWC.LineStyle.Dashed, pointMarkersVisible: true, pointMarkersRadius: 2 });
    candleMarkers = LWC.createSeriesMarkers(candleSeries, []);
    structureMarkers = LWC.createSeriesMarkers(structureSeries, []);
    chart.subscribeCrosshairMove((param) => renderLegend(param.time ? param.time : null));
    applyToggles();

    equityChart = LWC.createChart($("equity-chart"), chartOptions(c.panel));
    equitySeries = equityChart.addSeries(LWC.AreaSeries, { ...equityColors(), lineWidth: 2, priceLineVisible: false });
  }

  function equityColors() {
    const up = cssVar("--up");
    return { lineColor: up, topColor: withAlpha(up, 0.35), bottomColor: withAlpha(up, 0.02) };
  }

  function ensureRsiPane() {
    if (!chart) return;
    if (state.toggles.rsi && !rsiSeries) {
      const c = colors();
      rsiSeries = chart.addSeries(LWC.LineSeries, {
        color: c.violet, lineWidth: 1, priceLineVisible: false, lastValueVisible: true,
        priceFormat: { type: "price", precision: 1, minMove: 0.1 },
        autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
      }, 1);
      for (const [price, color] of [[70, c.down], [50, c.muted], [30, c.up]]) {
        rsiSeries.createPriceLine({ price, color: withAlpha(color, 0.6), lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: false });
      }
      rsiSeries.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } });
      chart.panes()[1]?.setHeight(120);
      if (state.chart) rsiSeries.setData(state.chart.rsi);
    } else if (!state.toggles.rsi && rsiSeries) {
      chart.removeSeries(rsiSeries);
      rsiSeries = null;
      try { if (chart.panes().length > 1) chart.removePane(1); } catch { /* volet déjà retiré */ }
    }
  }

  function applyToggles() {
    const t = state.toggles;
    document.querySelectorAll("[data-toggle]").forEach((b) => b.classList.toggle("on", !!t[b.dataset.toggle]));
    if (!chart) return;
    emaFastSeries.applyOptions({ visible: t.ema });
    emaSlowSeries.applyOptions({ visible: t.ema });
    volumeSeries.applyOptions({ visible: t.volume });
    structureSeries.applyOptions({ visible: t.structure });
    ensureRsiPane();
    renderLevels();
    renderMarkers();
    renderStructureLabels();
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    store.set(THEME_KEY, theme);
    if (!chart) return;
    const c = colors();
    chart.applyOptions(chartOptions());
    equityChart.applyOptions(chartOptions(c.panel));
    candleSeries.applyOptions(candleColors());
    emaFastSeries.applyOptions({ color: c.accent });
    emaSlowSeries.applyOptions({ color: c.orange });
    structureSeries.applyOptions({ color: withAlpha(c.violet, 0.8) });
    tpZone.applyOptions(zoneOptions(c.up));
    slZone.applyOptions(zoneOptions(c.down));
    equitySeries.applyOptions(equityColors());
    if (rsiSeries) { state.toggles.rsi = false; ensureRsiPane(); state.toggles.rsi = true; ensureRsiPane(); }
    if (state.chart) setChartData(state.chart, false);
  }

  // --- Chargement et dessin du graphique ------------------------------------------------------
  async function loadChart() {
    if (!chart || !state.symbol) return;
    const symbol = state.symbol, tf = state.tf;
    const data = await api(`/api/chart?symbol=${encodeURIComponent(symbol)}&timeframe=${tf}`);
    if (symbol !== state.symbol || tf !== state.tf) return;
    const key = `${symbol}|${tf}`;
    const sameView = key === state.viewKey;
    const range = sameView ? chart.timeScale().getVisibleLogicalRange() : null;
    state.viewKey = key;
    setChartData(data, !sameView);
    if (sameView && range) chart.timeScale().setVisibleLogicalRange(range);
    if (!sameView) subscribeBinance();
  }

  function setChartData(data, resetView) {
    state.chart = data;
    state.bars = data.candles.map((c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume }));
    const last = state.bars[state.bars.length - 1];
    const precision = precisionFor(last?.close, data.asset_class);
    candleSeries.applyOptions({ priceFormat: { type: "price", precision, minMove: 10 ** -precision } });
    candleSeries.setData(state.bars.map(({ volume, ...bar }) => bar));
    volumeSeries.setData(state.bars.map(volumeBar));
    emaFastSeries.setData(data.ema_fast);
    emaSlowSeries.setData(data.ema_slow);
    if (rsiSeries) rsiSeries.setData(data.rsi);
    state.maps = {
      vol: new Map(state.bars.map((b) => [b.time, b.volume])),
      emaF: new Map(data.ema_fast.map((p) => [p.time, p.value])),
      emaS: new Map(data.ema_slow.map((p) => [p.time, p.value])),
      rsi: new Map(data.rsi.map((p) => [p.time, p.value])),
    };
    const points = new Map();
    data.structure.forEach((p) => points.set(p.time, p));
    state.structure = [...points.values()].sort((a, b) => a.time - b.time);
    structureSeries.setData(state.structure.map((p) => ({ time: p.time, value: p.value })));
    state.positionKey = "";
    renderLevels();
    renderStructureLabels();
    renderPosition();
    renderMarkers();
    renderHeader();
    renderLegend(null);
    if (resetView && state.bars.length) {
      const n = state.bars.length;
      chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 160), to: n + 8 });
    }
  }

  function volumeBar(b) {
    const c = colors();
    return { time: b.time, value: b.volume || 0, color: withAlpha(b.close >= b.open ? c.up : c.down, 0.45) };
  }

  // Heure de la bougie affichée qui contient l'instant t (les événements sont placés sur leur bougie).
  function snap(t) {
    const bars = state.bars;
    if (!bars.length || t < bars[0].time) return null;
    let lo = 0, hi = bars.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (bars[mid].time <= t) lo = mid; else hi = mid - 1;
    }
    return bars[lo].time;
  }

  function currentPosition() {
    const live = (state.status?.positions || []).find((p) => p.symbol === state.symbol);
    return live || state.chart?.position || null;
  }

  function renderMarkers() {
    if (!candleMarkers || !state.chart) return;
    const c = colors();
    const markers = [];
    const push = (iso, marker) => { const time = snap(toSec(iso)); if (time != null) markers.push({ time, ...marker }); };
    for (const t of state.chart.trades) {
      const long = t.direction === "LONG";
      push(t.entry_time, { position: long ? "belowBar" : "aboveBar", color: long ? c.up : c.down,
        shape: long ? "arrowUp" : "arrowDown", text: long ? "Achat" : "Vente" });
      push(t.timestamp, { position: long ? "aboveBar" : "belowBar", color: t.net_pnl >= 0 ? c.up : c.down,
        shape: "circle", text: `${EXIT_LABELS[t.order_type] || "Sortie"} ${fmtSigned(t.net_pnl, 0)}` });
    }
    const p = currentPosition();
    if (p) {
      const long = p.direction === "LONG";
      push(p.entry_time, { position: long ? "belowBar" : "aboveBar", color: long ? c.up : c.down,
        shape: long ? "arrowUp" : "arrowDown", text: long ? "Achat (ouvert)" : "Vente (ouvert)" });
    }
    if (state.toggles.ai) {
      for (const d of state.chart.decisions) {
        if (d.status === "lesson" || d.action === "-") continue;
        const opening = d.action === "OPEN_LONG" || d.action === "OPEN_SHORT";
        const label = opening ? `IA ${d.action === "OPEN_LONG" ? "long" : "short"} ${fmtNum(d.confidence, 2)}` : "";
        push(d.timestamp, { position: d.action === "OPEN_SHORT" ? "aboveBar" : "belowBar", color: c.violet,
          shape: opening ? "square" : "circle", size: opening ? 0.8 : 0.4, text: label });
      }
    }
    markers.sort((a, b) => a.time - b.time);
    candleMarkers.setMarkers(markers);
  }

  function clearLines(group) {
    state.priceLines[group].forEach((line) => candleSeries.removePriceLine(line));
    state.priceLines[group] = [];
  }

  function addLine(group, price, title, color, lineStyle) {
    if (!isNum(price)) return;
    state.priceLines[group].push(candleSeries.createPriceLine({ price, title, color, lineStyle, lineWidth: 1, axisLabelVisible: true }));
  }

  function renderLevels() {
    if (!candleSeries) return;
    clearLines("levels");
    if (!state.toggles.levels || !state.chart) return;
    const c = colors();
    state.chart.levels.resistances.forEach((price) => addLine("levels", price, "Résistance", withAlpha(c.down, 0.7), LWC.LineStyle.SparseDotted));
    state.chart.levels.supports.forEach((price) => addLine("levels", price, "Support", withAlpha(c.up, 0.7), LWC.LineStyle.SparseDotted));
  }

  // Structure de marché : chaque sommet/creux est comparé au précédent (HH, HL, LH, LL).
  function renderStructureLabels() {
    if (!structureMarkers) return;
    const c = colors();
    const markers = [];
    let lastHigh = null, lastLow = null, highLabel = "", lowLabel = "";
    for (const p of state.structure || []) {
      if (p.kind === "H") {
        highLabel = lastHigh == null ? "H" : p.value > lastHigh ? "HH" : "LH";
        lastHigh = p.value;
        markers.push({ time: p.time, position: "aboveBar", shape: "circle", size: 0.1, text: highLabel,
          color: highLabel === "HH" ? c.up : highLabel === "LH" ? c.down : c.muted });
      } else {
        lowLabel = lastLow == null ? "L" : p.value > lastLow ? "HL" : "LL";
        lastLow = p.value;
        markers.push({ time: p.time, position: "belowBar", shape: "circle", size: 0.1, text: lowLabel,
          color: lowLabel === "HL" ? c.up : lowLabel === "LL" ? c.down : c.muted });
      }
    }
    state.trend = highLabel === "HH" && lowLabel === "HL" ? "haussière" : highLabel === "LH" && lowLabel === "LL" ? "baissière" : "sans tendance claire";
    structureMarkers.setMarkers(state.toggles.structure ? markers : []);
  }

  // Position ouverte : zone verte jusqu'à l'objectif, zone rouge jusqu'au stop (comme l'outil « position » de TradingView).
  function renderPosition() {
    if (!candleSeries) return;
    const p = currentPosition();
    const lastTime = state.bars.length ? state.bars[state.bars.length - 1].time : 0;
    const key = p ? [p.id, p.stop_loss, p.take_profit, p.tp1_done, p.trailing_active, lastTime, state.viewKey].join("|") : `none|${state.viewKey}`;
    if (key === state.positionKey) return;
    state.positionKey = key;
    clearLines("position");
    if (!p || !state.bars.length) {
      tpZone.setData([]);
      slZone.setData([]);
      return;
    }
    const start = snap(toSec(p.entry_time)) ?? state.bars[0].time;
    const times = state.bars.filter((b) => b.time >= start).map((b) => b.time);
    tpZone.applyOptions({ baseValue: { type: "price", price: p.entry_price } });
    slZone.applyOptions({ baseValue: { type: "price", price: p.entry_price } });
    tpZone.setData(times.map((time) => ({ time, value: p.take_profit })));
    slZone.setData(times.map((time) => ({ time, value: p.stop_loss })));
    const c = colors(), style = LWC.LineStyle;
    addLine("position", p.entry_price, `Entrée ${p.direction === "LONG" ? "achat" : "vente"}`, c.accent, style.Solid);
    addLine("position", p.stop_loss, p.trailing_active ? "Stop suiveur" : "Stop loss", c.down, style.Dashed);
    addLine("position", p.take_profit, "Take profit", c.up, style.Dashed);
    if (p.tp1_price && !p.tp1_done) addLine("position", p.tp1_price, "TP1 (partiel)", c.violet, style.Dotted);
  }

  function renderHeader() {
    const d = state.chart;
    if (!d) return;
    const last = state.bars[state.bars.length - 1];
    $("sym-name").textContent = `${d.symbol} · ${d.name}`;
    $("sym-tv").textContent = `${d.tradingview || ""} · ${d.currency}`;
    const row = state.watchlist.find((r) => r.symbol === d.symbol);
    const open = row ? row.market_open : d.market_open;
    const feed = d.asset_class === "crypto"
      ? (state.binanceLive ? "temps réel (Binance)" : "temps réel")
      : open ? "mise à jour chaque minute" : "marché fermé";
    const tag = $("sym-market");
    tag.textContent = feed;
    tag.className = "tag " + (open ? "ok" : "attention");
    $("last-price").textContent = fmtPrice(last?.close, d.asset_class);
    const change = row?.change_pct;
    $("sym-change").textContent = isNum(change) ? `${fmtSigned(change)} % (24 h)` : "";
    $("sym-change").className = signClass(change);
    document.title = last ? `${d.symbol} ${fmtPrice(last.close, d.asset_class)} — Paper Trading Bot` : "Paper Trading Bot";
  }

  function renderLegend(time) {
    const d = state.chart;
    if (!d || !state.bars.length) { $("ohlc").innerHTML = ""; return; }
    let index = state.bars.length - 1;
    if (time != null) {
      const found = state.bars.findIndex((b) => b.time === time);
      if (found >= 0) index = found;
    }
    const bar = state.bars[index], prev = state.bars[index - 1];
    const change = prev ? (bar.close / prev.close - 1) * 100 : null;
    const cls = bar.close >= bar.open ? "pos" : "neg";
    const fp = (v) => fmtPrice(v, d.asset_class);
    const parts = [
      `<span><b>${esc(d.symbol)}</b> · ${esc(state.tf)}</span>`,
      `<span>O <b class="${cls}">${fp(bar.open)}</b> H <b class="${cls}">${fp(bar.high)}</b> L <b class="${cls}">${fp(bar.low)}</b> C <b class="${cls}">${fp(bar.close)}</b>` +
        (isNum(change) ? ` <b class="${signClass(change)}">${fmtSigned(change)} %</b>` : "") + "</span>",
      `<span>Vol <b>${fmtVolume(state.maps.vol.get(bar.time))}</b></span>`,
    ];
    if (state.toggles.ema) {
      parts.push(`<span class="ema-f">EMA 20 ${fp(state.maps.emaF.get(bar.time))}</span>`);
      parts.push(`<span class="ema-s">EMA 50 ${fp(state.maps.emaS.get(bar.time))}</span>`);
    }
    if (state.toggles.rsi) parts.push(`<span>RSI 14 <b>${fmtNum(state.maps.rsi.get(bar.time), 1)}</b></span>`);
    if (state.toggles.structure && state.trend) parts.push(`<span>Structure <b>${esc(state.trend)}</b></span>`);
    $("ohlc").innerHTML = parts.join("");
  }

  // --- Mises à jour en direct -----------------------------------------------------------------
  // Crypto : flux kline public de Binance (lecture seule, sans clé) à la seconde.
  // Actions, ETF, forex : bougies envoyées par le serveur (Yahoo, rafraîchi chaque minute marché ouvert).
  function closeBinance() {
    const ws = state.binance;
    state.binance = null;
    state.binanceLive = false;
    if (ws) { ws.onclose = null; ws.close(); }
  }

  function subscribeBinance() {
    closeBinance();
    if (!state.chart || state.chart.asset_class !== "crypto") return;
    const key = state.viewKey, symbol = state.symbol;
    let ws;
    try {
      ws = new WebSocket(`wss://data-stream.binance.vision/ws/${symbol.toLowerCase()}@kline_${state.tf}`);
    } catch { return; }
    state.binance = ws;
    ws.onmessage = (event) => {
      if (state.viewKey !== key) return;
      const k = JSON.parse(event.data).k;
      if (!k) return;
      if (!state.binanceLive) { state.binanceLive = true; renderHeader(); }
      applyTick({ symbol, time: Math.floor(k.t / 1000), open: +k.o, high: +k.h, low: +k.l, close: +k.c, volume: +k.v, closed: k.x }, true);
    };
    ws.onclose = () => {
      if (state.binance !== ws) return;
      state.binance = null;
      state.binanceLive = false;
      setTimeout(() => { if (state.viewKey === key && !state.binance) subscribeBinance(); }, 5000);
    };
  }

  function onServerCandle(c) {
    if (c.symbol !== state.symbol || !state.chart || state.binanceLive) return;
    applyTick(c, state.tf === state.status?.bot.timeframe);
  }

  function applyTick(c, native) {
    const bars = state.bars;
    if (!bars.length) return;
    const last = bars[bars.length - 1];
    let bar;
    if (native) {
      if (c.time < last.time) return;
      bar = { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume };
      if (c.time > last.time) bars.push(bar); else bars[bars.length - 1] = bar;
      state.maps.vol.set(bar.time, bar.volume);
      volumeSeries.update(volumeBar(bar));
    } else {
      // Bougie 1h reçue alors que le graphique est en 4h/1J/1S : elle complète la bougie en cours.
      if (c.time < last.time) return;
      if (c.time >= last.time + TF_SECONDS[state.tf]) { debounce("chart", () => loadChart().catch(console.error), 500); return; }
      bar = { ...last, high: Math.max(last.high, c.high), low: Math.min(last.low, c.low), close: c.close };
      bars[bars.length - 1] = bar;
    }
    const { volume, ...ohlc } = bar;
    candleSeries.update(ohlc);
    updateEma(emaFastSeries, "emaF", 20, bar);
    updateEma(emaSlowSeries, "emaS", 50, bar);
    state.lastUpdate = Date.now();
    renderHeader();
    renderLegend(null);
    renderPosition();
    if (c.closed) debounce("chart", () => loadChart().catch(console.error), 1500);
  }

  // EMA de la bougie en cours recalculée à partir de la valeur de la bougie précédente.
  function updateEma(series, mapName, period, bar) {
    const bars = state.bars, map = state.maps[mapName];
    const prev = bars.length > 1 ? map.get(bars[bars.length - 2].time) : undefined;
    if (!isNum(prev)) return;
    const k = 2 / (period + 1);
    const value = prev + k * (bar.close - prev);
    map.set(bar.time, value);
    series.update({ time: bar.time, value });
  }

  // --- Liste de surveillance ---------------------------------------------------------------------
  async function loadWatchlist() {
    state.watchlist = await api("/api/watchlist");
    renderWatchlist();
    renderHeader();
  }

  function renderWatchlist() {
    const rows = state.watchlist;
    const prices = state.status?.prices || {};
    const groups = {};
    rows.forEach((r) => (groups[r.asset_class] ||= []).push(r));
    const order = ["crypto", "stock", "etf", "forex"];
    $("wl-count").textContent = `${rows.filter((r) => r.market_open).length}/${rows.length} ouverts`;
    $("watchlist").innerHTML = order.filter((k) => groups[k]).map((k) =>
      `<div class="wl-group">${esc(CLASS_LABELS[k] || k)}</div>` + groups[k].map((r) => {
        const price = isNum(prices[r.symbol]) ? prices[r.symbol] : r.price;
        const title = r.error ? `Erreur de données : ${r.error}` : r.market_open ? "Marché ouvert" : "Marché fermé";
        return `<div class="wl-row${r.symbol === state.symbol ? " active" : ""}" data-symbol="${esc(r.symbol)}" title="${esc(title)}">
          <span class="dot${r.market_open ? " open" : ""}"></span>
          <span><span class="sym">${esc(r.symbol)}</span>${r.position ? `<span class="pos-flag ${esc(r.position)}">${r.position === "LONG" ? "L" : "S"}</span>` : ""}
            <span class="nm">${esc(r.name)}</span></span>
          <span class="px">${r.error && !isNum(price) ? '<span class="neg">erreur</span>' : fmtPrice(price, r.asset_class)}</span>
          <span class="ch ${signClass(r.change_pct)}">${isNum(r.change_pct) ? fmtSigned(r.change_pct) + " %" : "—"}</span>
        </div>`;
      }).join("")).join("");
  }

  function selectSymbol(symbol) {
    if (!symbol || symbol === state.symbol) return;
    state.symbol = symbol;
    saveView();
    renderWatchlist();
    closeBinance();
    loadChart().catch(console.error);
  }

  function selectTimeframe(tf) {
    if (!TF_SECONDS[tf] || tf === state.tf) return;
    state.tf = tf;
    saveView();
    document.querySelectorAll("[data-tf]").forEach((b) => b.classList.toggle("active", b.dataset.tf === tf));
    closeBinance();
    loadChart().catch(console.error);
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
  function setText(id, text, cls = "") {
    const el = $(id);
    el.textContent = text;
    el.className = cls;
  }

  function facts(id, rows) {
    $(id).innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  }

  function renderStatus(s) {
    state.status = s;
    if (!state.symbol || !s.bot.symbols.includes(state.symbol)) state.symbol = s.bot.symbols[0];
    const status = s.bot.status;
    $("bot-status").textContent = status;
    $("bot-status").className = "status" + (status === "RUNNING" ? " running" : status === "HALTED" ? " halted" : "");
    document.querySelector('[data-action="start"]').disabled = status === "RUNNING" || status === "HALTED";
    document.querySelector('[data-action="pause"]').disabled = status !== "RUNNING";
    document.querySelector('[data-action="stop"]').disabled = status === "STOPPED";
    $("reset-halt").hidden = status !== "HALTED";

    const a = s.account, cur = a.currency, perf = s.performance, costs = s.ai.costs;
    setText("k-initial", `${fmtNum(a.initial_capital)} ${cur}`);
    setText("k-balance", `${fmtNum(a.balance)} ${cur}`);
    setText("k-equity", `${fmtNum(a.equity)} ${cur}`);
    setText("k-pnl", `${fmtSigned(a.pnl)} ${cur}`, signClass(a.pnl));
    setText("k-pnl-pct", fmtPct(a.pnl_pct, true), signClass(a.pnl_pct));
    setText("k-dd", fmtPct(a.drawdown));
    setText("k-trades", String(perf.trades));
    setText("k-winrate", perf.trades ? fmtPct(perf.win_rate) : "—");
    setText("k-pf", perf.profit_factor == null ? (perf.wins ? "∞" : "—") : fmtNum(perf.profit_factor));
    setText("k-budget", `${fmtNum(costs.spent_today_usd)} / ${fmtNum(costs.daily_budget_usd)} $`);

    renderPositions(s);
    renderRisk(s);
    renderClaude(s);
    renderClock(s);
    renderLearning();
    if (state.watchlist.length) renderWatchlist();
    renderPosition();
  }

  function renderClaude(s) {
    const ai = s.ai, threshold = ai.min_confidence_effective;
    $("ai-mode").textContent = ai.enabled ? "mode IA actif" : `mode ${ai.mode === "ai" ? "IA inactif (clé absente)" : "règles"}`;
    facts("ai-live", [
      ["Décisions", ai.enabled ? ai.decision_model : "désactivées"],
      ["Revues / actualités", `${ai.review_model} / ${ai.briefing_model}`],
      ["Confiance minimale", threshold ? `${fmtNum(threshold.value, 2)} (${threshold.reason})` : fmtNum(ai.min_confidence, 2)],
      ["Budget restant ce jour", `${fmtNum(ai.costs.remaining_usd)} $`],
    ]);
  }

  function renderClock(s) {
    const m = s.market_clock, b = s.ai.briefing, mk = m.markets || {};
    const open = (v) => (v ? "ouvert" : "fermé");
    $("clock-utc").textContent = new Date(m.utc).toISOString().slice(11, 16) + " UTC";
    facts("clock", [
      ["Crypto", "ouvert 24/7"],
      ["Bourse US (NYSE/Nasdaq)", m.us_equity_open ? `ouverte (${m.minutes_since_us_open} min)` : `fermée — ouverture ${fmtTime(m.next_us_open)}`],
      ["Euronext Paris", open(mk.euronext)],
      ["Forex", open(mk.forex)],
      ["Sessions actives", m.active_sessions.join(", ") || "aucune"],
      ["Liquidité", m.liquidity],
      ["Jour férié US", m.us_holiday || "non"],
      ["Blackout d'annonce", b && b.blackout ? b.blackout.reason : "aucun"],
    ]);
    if (!b) {
      $("briefing").innerHTML = '<p class="muted small">Le briefing (actualités, agenda économique) est généré au premier cycle IA.</p>';
      return;
    }
    const events = b.events.map((e) => `<div class="row"><span>${e.time_utc ? fmtTime(e.time_utc) : "—"} ${esc(e.name)}</span><span class="tag impact-${esc(e.impact)}">${esc(e.impact)}</span></div>`).join("");
    const views = b.per_symbol.map((v) => `<span class="tag bias-${esc(v.bias)}">${esc(v.symbol)} ${esc(v.bias)}</span>`).join(" ");
    $("briefing").innerHTML = `<div class="card"><div class="row head"><span>Briefing ${fmtTime(b.generated_at)}</span>
      <span>risque ${esc(b.risk_level)} · sentiment ${fmtSigned(b.sentiment, 2)}</span></div>
      <div>${views}</div>${events}<details><summary>Synthèse</summary><p>${esc(b.summary)}</p></details></div>`;
  }

  async function loadDecisions() {
    const data = await api("/api/ai/decisions?limit=12");
    state.track = data.track_record;
    $("ai-decisions").innerHTML = data.decisions.filter((d) => d.status !== "lesson").slice(0, 8).map((d) => `<div class="card">
      <div class="row head"><span>${esc(d.symbol)} ${esc(d.action)}${d.confidence != null ? ` · ${fmtNum(d.confidence, 2)}` : ""}</span>
      <span class="tag status-${esc(d.status)}">${esc(d.status)}</span></div>
      <div class="row muted"><span>${fmtTime(d.timestamp)} · ${esc(d.trigger)}</span><span class="${signClass(d.outcome_r)}">${d.outcome_r != null ? fmtSigned(d.outcome_r, 2) + " R" : ""}</span></div>
      <p>${esc(d.thesis || d.status_reason)}</p></div>`).join("") || '<p class="muted small">Aucune décision pour le moment.</p>';
    renderLearning();
  }

  function renderLearning() {
    const t = state.track, s = state.status;
    if (!t || !s) return;
    const o = s.optimizer, threshold = s.ai.min_confidence_effective;
    const calib = Object.entries(t.calibration_par_confiance || {}).map(([k, v]) =>
      `${k} : ${v.trades} trades, ${fmtPct(v.taux_reussite)} gagnants, ${fmtSigned(v.r_moyen, 2)} R`).join(" · ");
    facts("learning", [
      ["Trades de Claude clôturés", String(t.trades_ia_clotures)],
      ["Taux de réussite de Claude", t.taux_reussite_ia == null ? "—" : fmtPct(t.taux_reussite_ia)],
      ["Calibration", calib || "pas encore de trades clôturés"],
      ["Seuil de confiance", threshold ? `${fmtNum(threshold.value, 2)} — ${threshold.reason}` : "—"],
      ["Leçons retenues", String((t.lecons_apprises || []).length)],
      ["Réglage du scanner", o.available ? (o.scheduled ? `automatique toutes les ${o.interval_hours} h${o.auto_apply ? ", appliqué s'il est validé" : ""}` : "inactif") : "clé API absente"],
      ["Stratégie active", (() => { const st = s.params.strategy, ex = s.params.exits; return `EMA ${st.ema_fast}/${st.ema_slow} · RSI ${st.rsi_length} · SL ${ex.stop_loss_atr_multiplier}×ATR · TP ${ex.take_profit_risk_reward} R`; })()],
    ]);
    $("lessons").innerHTML = (t.lecons_apprises || []).slice(-4).reverse().map((l) => `<div class="card">
      <div class="row head"><span>${esc(l.symbole)}</span><span class="${signClass(l.resultat_r)}">${fmtSigned(l.resultat_r, 2)} R</span></div>
      <p>${esc(l.regle || l.lecon)}</p></div>`).join("") ||
      '<p class="muted small">Après chaque trade clôturé, Claude rédige une leçon réutilisée dans ses décisions suivantes.</p>';
  }

  function renderPositions(s) {
    const cur = s.account.currency;
    $("positions").innerHTML = s.positions.length ? s.positions.map((p) => {
      const ac = state.watchlist.find((r) => r.symbol === p.symbol)?.asset_class;
      const fp = (v) => fmtPrice(v, ac);
      return `
      <div class="card ${p.direction === "LONG" ? "long" : "short"}" data-symbol="${esc(p.symbol)}" style="cursor:pointer">
        <div class="row head"><span>${esc(p.symbol)} ${p.direction === "LONG" ? "ACHAT" : "VENTE"}</span>
          <span class="${signClass(p.unrealized_pnl)}">${fmtSigned(p.unrealized_pnl)} ${esc(cur)} (${fmtPct(p.unrealized_pct, true)})</span></div>
        <div class="row"><span>Qté ${fmtNum(p.quantity, 4)}</span><span>Entrée ${fp(p.entry_price)}</span></div>
        <div class="row"><span class="neg">${p.trailing_active ? "Stop suiveur" : "Stop loss"} ${fp(p.stop_loss)}</span><span class="pos">Take profit ${fp(p.take_profit)}</span></div>
        ${p.tp1_price ? `<div class="row"><span>TP1 ${fp(p.tp1_price)} (${fmtNum(p.tp1_fraction * 100, 0)} %)</span><span>${p.tp1_done ? "encaissé" : p.breakeven_done ? "stop au point mort" : "en attente"}</span></div>` : ""}
        ${p.confidence != null ? `<div class="row muted"><span>Décision Claude #${p.decision_id}</span><span>confiance ${fmtNum(p.confidence, 2)}</span></div>` : ""}
        <div class="row muted"><span>Dernier ${fp(p.last_price)}</span><span>${fmtTime(p.entry_time)}</span></div>
      </div>`;
    }).join("") : '<p class="muted">Aucune position.</p>';
    $("pending").innerHTML = s.pending_orders.map((o) =>
      `<div class="card"><div class="row"><span>Ordre #${o.seq} ${esc(o.order_type)} ${esc(o.side)} ${esc(o.symbol)}</span><span>en attente</span></div></div>`).join("");
  }

  function renderRisk(s) {
    const r = s.risk, l = r.limits;
    facts("risk", [
      ["Kill-switch", r.halted ? `ACTIF — ${r.halt_reason}` : "inactif"],
      ["Nouvelles entrées", r.entries_blocked ? "bloquées" : "autorisées"],
      ["Perte du jour", `${fmtPct(r.daily_loss)} / ${fmtPct(l.max_daily_loss)}`],
      ["Perte de la semaine", `${fmtPct(r.weekly_loss)} / ${fmtPct(l.max_weekly_loss)}`],
      ["Risque ouvert", `${fmtPct(r.open_risk.total)} / ${fmtPct(l.max_portfolio_risk)}`],
      ["Risque max par trade", fmtPct(l.hard_max_risk_per_trade)],
      ["Drawdown", `${fmtPct(s.account.drawdown)} / ${fmtPct(l.max_drawdown)}`],
      ["Positions max", String(l.max_open_positions)],
      ["Source des signaux", s.bot.signal_source],
      ["Dernier cycle", s.bot.last_error ? `erreur : ${s.bot.last_error}` : fmtTime(s.bot.last_tick)],
    ]);
  }

  async function loadReadiness() {
    const r = await api("/api/readiness");
    $("ready-verdict").textContent = r.verdict;
    $("ready-verdict").className = "verdict " + (r.ready ? "pos" : "neg");
    $("readiness").innerHTML = r.checks.map((c) => `<div class="card"><div class="row head"><span>${esc(c.name)}</span>
      <span class="tag ${esc(c.status)}">${esc(c.status)}</span></div><p class="muted">${esc(c.detail)}</p></div>`).join("");
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
    decisions: {
      url: "/api/ai/decisions?limit=100",
      pick: (data) => data.decisions,
      head: ["Heure", "Symbole", "Déclencheur", "Action", "Confiance", "Statut", "Résultat", "Thèse / raison"],
      row: (d) => [fmtTime(d.timestamp), esc(d.symbol), esc(d.trigger), esc(d.action),
        d.confidence != null ? fmtNum(d.confidence, 2) : "—", esc(d.status),
        d.outcome_r != null ? `<span class="${signClass(d.outcome_r)}">${fmtSigned(d.outcome_r, 2)} R</span>` : "—",
        esc((d.thesis || d.status_reason || "").slice(0, 160))],
    },
    signals: {
      url: "/api/signals?limit=100",
      head: ["Heure", "Symbole", "Sens", "Prix", "Source", "Statut", "Raison"],
      row: (s) => [fmtTime(s.timestamp), esc(s.symbol), esc(s.direction), fmtPrice(s.price), esc(s.source), esc(s.status), esc(s.reason)],
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

  // --- Temps réel (serveur) --------------------------------------------------------------------
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
    const refresh = (name, fn, ms = 400) => debounce(name, () => fn().catch(console.error), ms);
    switch (message.type) {
      case "status": renderStatus(message.data); break;
      case "candle": onServerCandle(message.data); break;
      case "equity": onEquity(message.data); break;
      case "trade":
        refresh("chart", loadChart, 800);
        refresh("watchlist", loadWatchlist);
        refresh("readiness", loadReadiness, 2000);
        if (state.table === "trades") refresh("table", loadTable);
        break;
      case "order": case "position_opened": case "position_closed":
        refresh("chart", loadChart, 800);
        break;
      case "signal":
        if (state.table === "signals") refresh("table", loadTable);
        break;
      case "ai_decision":
        refresh("decisions", loadDecisions);
        refresh("chart", loadChart, 800);
        if (state.table === "decisions") refresh("table", loadTable);
        break;
      case "optimization": case "bot": case "risk":
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
    if (!status.bot.symbols.includes(state.symbol)) state.symbol = status.bot.symbols[0];
    document.querySelectorAll("[data-tf]").forEach((b) => b.classList.toggle("active", b.dataset.tf === state.tf));
    renderStatus(status);
    await Promise.all([loadWatchlist(), loadChart(), loadEquity(), loadTable(), loadDecisions(), loadReadiness()]);
    if (!state.ws) connectWs();
    if (!state.timers.poll) {
      state.timers.poll = setInterval(() => loadWatchlist().catch(console.error), 15000);
      setInterval(() => loadReadiness().catch(console.error), 300000);
      // Filet de sécurité : si aucune mise à jour n'est arrivée depuis 2 minutes, recharger.
      setInterval(() => { if (Date.now() - state.lastUpdate > 120000) loadChart().catch(console.error); }, 60000);
    }
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
    $("watchlist").addEventListener("click", (event) => selectSymbol(event.target.closest("[data-symbol]")?.dataset.symbol));
    $("positions").addEventListener("click", (event) => selectSymbol(event.target.closest("[data-symbol]")?.dataset.symbol));
    $("tf-buttons").addEventListener("click", (event) => selectTimeframe(event.target.closest("[data-tf]")?.dataset.tf));
    $("toggles").addEventListener("click", (event) => {
      const name = event.target.closest("[data-toggle]")?.dataset.toggle;
      if (!name) return;
      state.toggles[name] = !state.toggles[name];
      saveView();
      applyToggles();
      renderLegend(null);
    });
    document.querySelectorAll("[data-table]").forEach((tab) => tab.addEventListener("click", () => {
      document.querySelectorAll("[data-table]").forEach((t) => t.classList.toggle("active", t === tab));
      state.table = tab.dataset.table;
      loadTable().catch(console.error);
    }));
    $("capital-open").addEventListener("click", () => {
      $("capital-input").value = state.status ? String(state.status.account.initial_capital) : "";
      $("capital-error").hidden = true;
      $("capital-dialog").showModal();
    });
    $("capital-cancel").addEventListener("click", () => $("capital-dialog").close());
    $("capital-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const amount = Number($("capital-input").value);
      try {
        const status = await api("/api/account/capital", { method: "POST", body: JSON.stringify({ amount }) });
        $("capital-dialog").close();
        renderStatus(status);
        await Promise.all([loadEquity(), loadReadiness()]);
      } catch (error) {
        $("capital-error").textContent = error.message;
        $("capital-error").hidden = false;
      }
    });
    $("wallet-connect").addEventListener("click", readWallet);
    $("theme-toggle").addEventListener("click", () =>
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  }

  document.documentElement.dataset.theme = store.get(THEME_KEY) || "dark";
  initCharts();
  bindEvents();
  if (state.token) start().catch(() => askToken(false)); else askToken(false);
})();
