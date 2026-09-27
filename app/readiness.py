"""Check-list « prêt pour de l'argent réel », calculée à partir des résultats réels du bot.

Chaque critère est évalué sur les données (historique paper, rentabilité, drawdown,
calibration de Claude, stabilité technique, hébergement). Le verdict reste NON tant que
l'exécution réelle n'existe pas : elle est volontairement verrouillée dans cette version.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal

from app.backtest.metrics import compute_metrics, max_drawdown
from app.core.types import utcnow

if TYPE_CHECKING:
    from app.services import Services

MIN_PAPER_DAYS = 60
MIN_TRADES = 100
MIN_PROFIT_FACTOR = 1.3


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Literal["ok", "attention", "bloquant"]
    detail: str


def evaluate_readiness(services: Services) -> dict[str, Any]:
    portfolio, repo, settings = services.stack.portfolio, services.repo, services.settings
    trades = portfolio.trades
    metrics = compute_metrics(trades, [portfolio.equity()], portfolio.initial_capital, 1.0)
    days = (utcnow() - trades[0].entry_time).days if trades else 0
    curve = [row["equity"] for row in repo.equity_curve(20_000)]
    drawdown = max_drawdown([portfolio.initial_capital, *curve]) if curve else 0.0
    track = repo.ai_track_record()
    high = track["calibration_par_confiance"].get("0.80+")
    errors = services.runner.symbol_errors
    checks = [
        Check("Historique de paper trading",
              "ok" if days >= MIN_PAPER_DAYS and len(trades) >= MIN_TRADES else "bloquant",
              f"{days} jours et {len(trades)} trades (minimum {MIN_PAPER_DAYS} jours et {MIN_TRADES} trades)"),
        Check("Rentabilité après frais",
              "ok" if metrics.profit_factor and metrics.profit_factor >= MIN_PROFIT_FACTOR and len(trades) >= 30
              else "bloquant",
              f"profit factor {metrics.profit_factor or 0:.2f} (minimum {MIN_PROFIT_FACTOR}), "
              f"espérance {metrics.expectancy_r:+.2f} R, taux de réussite {metrics.win_rate:.0%}"),
        Check("Drawdown maîtrisé", "ok" if drawdown < settings.max_drawdown * 0.8 else "bloquant",
              f"drawdown maximal {drawdown:.1%} (limite {settings.max_drawdown:.0%})"),
        Check("Calibration de Claude",
              "ok" if high and high["trades"] >= 20 and high["r_moyen"] > 0 else "attention",
              f"trades à confiance ≥ 0,80 : {high['trades']} ; R moyen {high['r_moyen']:+.2f}" if high
              else "pas encore assez de trades décidés par Claude"),
        Check("Stabilité technique", "ok" if not errors and not services.runner.last_error else "attention",
              "aucune erreur de données" if not errors else f"sources en erreur : {', '.join(errors)}"),
        Check("Fonctionnement 24/7", "ok" if os.environ.get("RENDER") else "bloquant",
              "hébergé sur Render" if os.environ.get("RENDER") else "le bot tourne sur ce PC : il s'arrête avec lui"),
        Check("Base de données persistante", "ok" if services.db.backend == "postgresql" else "attention",
              "PostgreSQL (Supabase)" if services.db.backend == "postgresql" else "SQLite locale"),
        Check("Exécution sur un vrai compte", "bloquant",
              "aucun broker réel branché (verrou PAPER_ONLY) : exécution à développer, tester en petit et auditer"),
        Check("Cadre légal et fiscal", "attention",
              "broker ou exchange régulé (MiFID II / MiCA), déclaration des comptes à l'étranger et des gains"),
    ]
    ready = all(c.status == "ok" for c in checks if c.status != "attention")
    return {"ready": ready, "verdict": "OUI" if ready else "NON", "checks": [asdict(c) for c in checks]}
