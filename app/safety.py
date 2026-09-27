"""Garde-fous PAPER_ONLY.

Plusieurs couches indépendantes empêchent toute exécution réelle :

1. ``Settings`` refuse ``PAPER_ONLY=false`` et tout ``TRADING_MODE`` autre que ``paper``.
2. ``assert_paper_only`` est appelé au démarrage et à la création du broker.
3. ``ensure_paper_broker`` refuse tout broker dont ``is_live`` n'est pas ``False``.
4. ``detect_live_credentials`` refuse de démarrer si des clés d'exchange ou de wallet
   (clé privée, seed phrase) sont présentes dans l'environnement.
5. Le client de données de marché n'autorise que des chemins HTTP en lecture seule.
6. Aucun code d'envoi d'ordre réel n'existe dans le projet (vérifié par les tests).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class LiveTradingDisabledError(RuntimeError):
    """Levée à toute tentative d'activer ou d'utiliser le trading réel."""


class _PaperSettings(Protocol):
    paper_only: bool
    trading_mode: str


PAPER_ONLY_BANNER = r"""
################################################################################
#                                                                              #
#   MODE PAPER TRADING UNIQUEMENT — AUCUN ORDRE RÉEL NE PEUT ÊTRE ENVOYÉ        #
#                                                                              #
#   Tous les ordres sont simulés localement. Aucune clé d'exchange, aucune     #
#   clé privée de wallet et aucun argent réel ne sont utilisés.                #
#                                                                              #
################################################################################
"""

# Noms de variables d'environnement qui trahiraient une tentative de trading réel.
_LIVE_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(BINANCE|BYBIT|KRAKEN|COINBASE|ALPACA|IBKR|OKX|KUCOIN|BITGET|OANDA|BITMEX|"
        r"DERIBIT|HYPERLIQUID|GATE|MEXC|HTX|BROKER|EXCHANGE)_.*(KEY|SECRET|PASSPHRASE|TOKEN)$"
    ),
    re.compile(r"(PRIVATE_KEY|MNEMONIC|SEED_PHRASE|SECRET_RECOVERY_PHRASE|KEYSTORE)"),
)


def assert_paper_only(settings: _PaperSettings) -> None:
    """Lève ``LiveTradingDisabledError`` si la configuration n'est pas strictement paper."""
    if settings.paper_only is not True:
        raise LiveTradingDisabledError("PAPER_ONLY doit valoir true : le trading réel est désactivé.")
    if settings.trading_mode != "paper":
        raise LiveTradingDisabledError(
            f"TRADING_MODE={settings.trading_mode!r} refusé : seul 'paper' est autorisé."
        )


def ensure_paper_broker(broker: Any) -> None:
    """Refuse tout broker qui ne se déclare pas explicitement comme simulé."""
    if getattr(broker, "is_live", True) is not False:
        raise LiveTradingDisabledError(
            f"Broker {type(broker).__name__} refusé : seuls les brokers paper sont autorisés."
        )


def detect_live_credentials(environ: Mapping[str, str] | None = None,
                            extra_keys: Iterable[str] = ()) -> list[str]:
    """Retourne les noms de variables qui ressemblent à des identifiants de trading réel."""
    names = set((environ if environ is not None else os.environ).keys()) | set(extra_keys)
    return sorted(
        name for name in names
        if any(pattern.search(name.upper()) for pattern in _LIVE_CREDENTIAL_PATTERNS)
    )


def assert_no_live_credentials(environ: Mapping[str, str] | None = None,
                               extra_keys: Iterable[str] = ()) -> None:
    found = detect_live_credentials(environ, extra_keys)
    if found:
        raise LiveTradingDisabledError(
            "Identifiants de trading réel détectés dans l'environnement : "
            f"{', '.join(found)}. Retirez-les : ce bot est PAPER ONLY."
        )


def reject_live_mode_request(requested_mode: str) -> LiveTradingDisabledError:
    """Journalise une tentative de passage en live et retourne l'erreur à lever."""
    logger.critical("Tentative de passage en mode %r bloquée : PAPER ONLY.", requested_mode)
    return LiveTradingDisabledError(
        "Le passage en trading réel est bloqué dans cette version (PAPER ONLY)."
    )


def print_startup_warning() -> None:
    logger.warning("Démarrage en mode PAPER TRADING UNIQUEMENT — aucun ordre réel possible.")
    print(PAPER_ONLY_BANNER, flush=True)
