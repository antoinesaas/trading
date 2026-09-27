"""Emplacement prévu pour une future exécution via wallet (MetaMask / EIP-1193).

ÉTAT : DÉSACTIVÉ. Cette classe définit le point d'intégration d'un broker réel
dans l'architecture, mais toute instanciation ou tout appel lève
``LiveTradingDisabledError``. Elle ne contient, volontairement, aucun code de
signature ni d'envoi de transaction.

Ce qu'il faudrait pour une version réelle (hors périmètre de cette version) :
1. choisir une venue d'exécution on-chain (DEX / agrégateur) et son réseau ;
2. faire signer chaque transaction PAR L'UTILISATEUR dans MetaMask (le serveur ne
   détient jamais de clé privée) ;
3. gérer approvals, gas, slippage on-chain, confirmations et échecs de transaction ;
4. faire auditer le code, puis retirer délibérément les garde-fous de ``app/safety.py``
   (ce n'est PAS un simple changement de configuration).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, NoReturn

from app.core.types import Candle
from app.safety import LiveTradingDisabledError
from app.trading.broker import Broker
from app.trading.orders import Order, OrderRequest

_MESSAGE = "WalletBroker est désactivé : cette version est PAPER TRADING UNIQUEMENT."


def _blocked() -> NoReturn:
    raise LiveTradingDisabledError(_MESSAGE)


class WalletBroker(Broker):
    is_live: ClassVar[bool] = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _blocked()

    def submit_order(self, request: OrderRequest, now: datetime) -> Order:
        _blocked()

    def cancel_order(self, order_id: str, reason: str) -> bool:
        _blocked()

    def close_position(self, symbol: str, reason: str, now: datetime) -> Order | None:
        _blocked()

    def modify_stop(self, symbol: str, new_stop: float, now: datetime, reason: str,
                    kind: str = "trailing") -> None:
        _blocked()

    def modify_take_profit(self, symbol: str, new_target: float, now: datetime, reason: str) -> None:
        _blocked()

    def process_bar(self, candle: Candle) -> None:
        _blocked()

    def pending_orders(self, symbol: str | None = None) -> list[Order]:
        _blocked()
