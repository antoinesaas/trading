"""Interface en ligne de commande : ``python -m app <commande>``.

Commandes : init-env, serve, backtest, fetch-data, optimize, reset-paper.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import sys
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.core.types import Candle, ensure_utc
from app.logging_setup import configure_logging
from app.safety import print_startup_warning

logger = logging.getLogger("app.cli")


def cmd_init_env(args: argparse.Namespace) -> int:
    target, template = Path(args.path), Path(".env.example")
    if target.exists():
        print(f"{target} existe déjà : rien n'est modifié.")
        return 1
    lines = []
    for line in template.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip()
        if key in ("WEBHOOK_SECRET", "DASHBOARD_TOKEN"):
            line = f"{key}={secrets.token_urlsafe(32)}"
        lines.append(line)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{target} créé avec un WEBHOOK_SECRET et un DASHBOARD_TOKEN aléatoires.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from app.main import create_app

    settings = Settings()
    app = create_app(settings)
    uvicorn.run(app, host=args.host or settings.host, port=args.port or settings.port,
                log_config=None)
    return 0


def _load_candles(args: argparse.Namespace, settings: Settings) -> list[Candle]:
    from app.market.data_provider import (
        BinanceMarketDataProvider, generate_synthetic_candles, load_candles_csv, save_candles_csv,
    )

    if args.synthetic:
        return generate_synthetic_candles(args.symbol, args.timeframe, args.bars, seed=args.seed)
    if args.csv:
        return load_candles_csv(Path(args.csv), args.symbol, args.timeframe)
    candles = BinanceMarketDataProvider(settings.binance_base_url).fetch_history(
        args.symbol, args.timeframe, args.bars)
    path = Path("data") / f"{args.symbol}_{args.timeframe}.csv"
    save_candles_csv(path, candles)
    print(f"{len(candles)} bougies téléchargées et figées dans {path} (relancer avec --csv {path}).")
    return candles


def _filter_period(candles: list[Candle], start: str | None, end: str | None) -> list[Candle]:
    lo = ensure_utc(datetime.fromisoformat(start)) if start else None
    hi = ensure_utc(datetime.fromisoformat(end)) if end else None
    return [c for c in candles if (lo is None or c.open_time >= lo) and (hi is None or c.open_time < hi)]


def cmd_backtest(args: argparse.Namespace) -> int:
    from app.backtest.engine import run_backtest
    from app.backtest.report import format_summary, write_reports
    from app.engine.params import ParameterSet, risk_limits_from_settings

    settings = Settings()
    params = ParameterSet.from_settings(settings)
    if args.active:
        from app.database.database import Database
        from app.database.repository import TradingRepository

        db = Database(settings.database_url)
        db.create_schema()
        active = TradingRepository(db).active_version()
        if active is not None:
            params = active[1].validated()
            print(f"Paramètres de la version active #{active[0]} utilisés.")
    candles = _filter_period(_load_candles(args, settings), args.start, args.end)
    result = run_backtest(candles, params, risk_limits_from_settings(settings), settings.initial_capital,
                          currency=settings.account_currency, lookback_bars=settings.lookback_bars,
                          verbose=args.verbose)
    print("\n=== BACKTEST (mêmes règles que le paper trading) ===")
    print(format_summary(result, settings.account_currency))
    paths = write_reports(result, Path(args.out), settings.account_currency)
    print("\nRapports : " + ", ".join(str(p) for p in paths.values()))
    return 0


def cmd_fetch_data(args: argparse.Namespace) -> int:
    from app.market.data_provider import BinanceMarketDataProvider, save_candles_csv

    settings = Settings()
    candles = BinanceMarketDataProvider(settings.binance_base_url).fetch_history(
        args.symbol, args.timeframe, args.bars)
    out = Path(args.out or f"data/{args.symbol}_{args.timeframe}.csv")
    save_candles_csv(out, candles)
    print(f"{len(candles)} bougies {args.symbol} {args.timeframe} enregistrées dans {out}")
    return 0


def cmd_optimize(_: argparse.Namespace) -> int:
    from app.services import build_services

    settings = Settings()
    services = build_services(settings)
    if not services.optimizer.available:
        print("ANTHROPIC_API_KEY absente : optimisation IA impossible.")
        return 1
    result = asyncio.run(services.optimizer.run_once("cli"))
    print(result)
    return 0 if result["status"] != "error" else 1


def cmd_reset(args: argparse.Namespace) -> int:
    from app.database.database import Database
    from app.database.repository import TradingRepository

    if not args.yes:
        print("Ajoutez --yes pour confirmer la remise à zéro du compte paper.")
        return 1
    db = Database(Settings().database_url)
    db.create_schema()
    TradingRepository(db).reset_paper_account()
    print("Compte paper remis à zéro (les versions de paramètres et optimisations sont conservées).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app", description="Bot de trading — PAPER ONLY")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-env", help="Crée .env depuis .env.example avec des secrets aléatoires")
    p.add_argument("--path", default=".env")
    p.set_defaults(func=cmd_init_env)

    p = sub.add_parser("serve", help="Lance le serveur (dashboard + webhook + bot)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("backtest", help="Backtest avec le même moteur que le paper trading")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--bars", type=int, default=3_000)
    source = p.add_mutually_exclusive_group()
    source.add_argument("--csv", help="Fichier CSV figé (reproductible)")
    source.add_argument("--synthetic", action="store_true", help="Données synthétiques déterministes")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--start", help="Date ISO de début (incluse)")
    p.add_argument("--end", help="Date ISO de fin (exclue)")
    p.add_argument("--active", action="store_true", help="Utiliser la version de paramètres active en base")
    p.add_argument("--out", default="reports")
    p.add_argument("--verbose", action="store_true", help="Afficher chaque signal et ordre simulé")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("fetch-data", help="Télécharge et fige un historique Binance en CSV")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--bars", type=int, default=3_000)
    p.add_argument("--out")
    p.set_defaults(func=cmd_fetch_data)

    p = sub.add_parser("optimize", help="Lance un cycle d'optimisation Claude")
    p.set_defaults(func=cmd_optimize)

    p = sub.add_parser("reset-paper", help="Remet le compte paper à zéro")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # console Windows (cp1252)
    args = build_parser().parse_args(argv)
    if args.command != "init-env":
        settings = Settings()
        configure_logging(settings.log_level, settings.log_dir)
        if args.command != "serve":
            print_startup_warning()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
