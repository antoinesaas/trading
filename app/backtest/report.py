"""Rapports de backtest : texte console, JSON, CSV de la courbe d'equity et HTML noir et blanc."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from app.backtest.engine import BacktestResult


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f} %"


def _num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}".replace(",", " ")


def summary_rows(result: BacktestResult, currency: str) -> list[tuple[str, str]]:
    m = result.metrics
    profit_factor = _num(m.profit_factor) if m.profit_factor is not None else (
        "∞" if m.wins else "n/a")
    return [
        ("Symbole", result.symbol),
        ("Timeframe", result.timeframe),
        ("Période", f"{result.start:%Y-%m-%d %H:%M} → {result.end:%Y-%m-%d %H:%M} UTC ({result.bars} bougies)"),
        ("Capital initial", f"{_num(m.initial_capital)} {currency}"),
        ("Equity finale", f"{_num(m.final_equity)} {currency}"),
        ("Rendement total", _pct(m.total_return)),
        ("Nombre de trades", str(m.trades)),
        ("Win rate", _pct(m.win_rate)),
        ("Gain moyen", f"{_num(m.avg_win)} {currency}"),
        ("Perte moyenne", f"{_num(m.avg_loss)} {currency}"),
        ("Profit factor", profit_factor),
        ("Expectancy", f"{_num(m.expectancy)} {currency} / trade ({m.expectancy_r:.2f} R)"),
        ("Drawdown maximal", _pct(m.max_drawdown)),
        ("Sharpe (annualisé)", _num(m.sharpe_ratio) if m.sharpe_ratio is not None else "n/a"),
        ("Frais payés", f"{_num(m.fees_paid)} {currency}"),
    ]


def format_summary(result: BacktestResult, currency: str) -> str:
    rows = summary_rows(result, currency)
    width = max(len(label) for label, _ in rows)
    lines = [f"{label.ljust(width)} : {value}" for label, value in rows]
    params = result.params
    lines.append(f"{'Paramètres'.ljust(width)} : {params.strategy_name} {params.strategy} "
                 f"| sorties {params.exits.model_dump()}")
    return "\n".join(lines)


def write_reports(result: BacktestResult, directory: Path, currency: str) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"backtest_{result.symbol}_{result.timeframe}_{result.start:%Y%m%d}_{result.end:%Y%m%d}"
    paths = {"json": directory / f"{stem}.json", "csv": directory / f"{stem}_equity.csv",
             "html": directory / f"{stem}.html"}
    paths["json"].write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    with paths["csv"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("timestamp", "equity"))
        writer.writerows((p.timestamp.isoformat(), f"{p.equity:.2f}") for p in result.equity_curve)
    paths["html"].write_text(_render_html(result, currency), encoding="utf-8")
    return paths


def _equity_svg(result: BacktestResult, width: int = 960, height: int = 280) -> str:
    values = [p.equity for p in result.equity_curve]
    if len(values) < 2:
        return "<p>Courbe d'equity indisponible.</p>"
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    step = width / (len(values) - 1)
    points = " ".join(f"{i * step:.1f},{height - (v - low) / span * height:.1f}"
                      for i, v in enumerate(values))
    return (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Courbe d\'equity">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{points}"/></svg>'
            f'<p class="axis">min {low:,.2f} — max {high:,.2f}</p>')


def _render_html(result: BacktestResult, currency: str) -> str:
    rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>"
                   for k, v in summary_rows(result, currency))
    trades = "".join(
        f"<tr><td>{t.entry_time:%Y-%m-%d %H:%M}</td><td>{t.direction}</td><td>{t.entry_price:.6g}</td>"
        f"<td>{t.exit_price:.6g}</td><td>{t.quantity:g}</td><td>{t.net_pnl:.2f}</td>"
        f"<td>{html.escape(t.reason)}</td></tr>" for t in result.trades)
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Backtest {html.escape(result.symbol)} {html.escape(result.timeframe)}</title>
<style>body{{font:14px/1.5 ui-monospace,Menlo,Consolas,monospace;background:#fff;color:#000;
max-width:1000px;margin:24px auto;padding:0 16px}}h1{{font-size:18px;border-bottom:2px solid #000}}
table{{border-collapse:collapse;width:100%;margin:16px 0}}th,td{{border:1px solid #000;padding:4px 8px;
text-align:left}}svg{{width:100%;height:auto;border:1px solid #000}}.axis{{color:#555}}</style></head>
<body><h1>Backtest — {html.escape(result.symbol)} {html.escape(result.timeframe)} (paper)</h1>
<table>{rows}</table><h2>Courbe d'equity</h2>{_equity_svg(result)}
<h2>Trades</h2><table><tr><th>Entrée</th><th>Sens</th><th>Prix entrée</th><th>Prix sortie</th>
<th>Qté</th><th>P&amp;L net</th><th>Raison</th></tr>{trades}</table></body></html>"""
