# Bot de trading algorithmique — PAPER TRADING UNIQUEMENT

> **Ce bot ne peut pas envoyer d'ordre réel.** Tous les ordres sont simulés localement.
> Il n'utilise aucune clé d'exchange, aucune clé privée de wallet et aucun argent réel.
> Ce projet n'est pas un conseil en investissement ; les performances passées ou simulées
> ne préjugent pas des performances futures.

Bot Python modulaire : données de marché → indicateurs → stratégie → gestion du risque →
broker simulé → portefeuille → base de données → dashboard temps réel. Il reçoit aussi les
signaux TradingView par webhook, et s'améliore en continu grâce à Claude (Opus 5.5), avec
validation systématique par backtest hors-échantillon.

---

## 1. Démarrage rapide

Prérequis : Python 3.12+ et Git.

```bash
git clone https://github.com/antoinesaas/trading.git
cd trading
python -m venv .venv
# Windows : .venv\Scripts\activate    —    macOS / Linux : source .venv/bin/activate
pip install -r requirements.txt
python -m app init-env          # crée .env avec un WEBHOOK_SECRET et un DASHBOARD_TOKEN aléatoires
pytest                          # 102 tests
python -m app serve             # dashboard + bot + webhook sur http://127.0.0.1:8000
```

Ouvrez http://127.0.0.1:8000, saisissez le `DASHBOARD_TOKEN` du fichier `.env`, puis **START**.

| Commande | Rôle |
|---|---|
| `python -m app serve` | Lance le serveur : dashboard, API, WebSocket, webhook et boucle du bot |
| `python -m app backtest --csv data/BTCUSDT_1h.csv` | Backtest reproductible sur données figées |
| `python -m app backtest --symbol ETHUSDT --timeframe 4h --bars 3000` | Télécharge l'historique Binance, le fige en CSV, puis backteste |
| `python -m app fetch-data --symbol BTCUSDT --timeframe 1h --bars 4000` | Télécharge et fige un historique |
| `python -m app optimize` | Un cycle d'amélioration par Claude (nécessite `ANTHROPIC_API_KEY`) |
| `python -m app reset-paper --yes` | Remet le compte paper à zéro |
| `pytest` | Lance la suite de tests |
| `docker compose up -d --build` | Lance le bot dans Docker (voir §9) |

Le bot et le dashboard tournent dans **le même processus** (`serve`) : le dashboard pilote le
bot (START / PAUSE / STOP).

---

## 2. Architecture

```text
 Binance (API publique, lecture seule)        TradingView (alerte Pine -> webhook HTTPS)
            │                                              │
            ▼                                              ▼
  MarketDataProvider ──► Indicators ──► Strategy      POST /webhook/tradingview
            │                               │          (secret, schéma, âge, doublon,
            │                               ▼           symbole, timeframe, écart de prix)
            │                          Signal ◄──────────────────┘
            │                               ▼
            │                        RiskManager  (taille, SL, TP, limites de compte)
            │                               ▼
            └──────────────────────► PaperBroker (MARKET, LIMIT, SL, TP, trailing, frais, slippage)
                                            ▼
                                        Portfolio (balance, equity, P&L, drawdown)
                                            ▼
                               EventBus ──► Database (SQLite / Supabase PostgreSQL)
                                        └─► WebSocket ──► Dashboard noir et blanc
```

Le **même** `TradingEngine` (assemblé par `build_trading_stack`) sert au backtest, au paper
trading temps réel et aux validations de l'optimiseur : il n'existe qu'une seule
implémentation des règles.

```text
trading/
├── app/
│   ├── main.py              application FastAPI (lifespan, routes, fichiers statiques)
│   ├── cli.py / __main__.py commandes `python -m app ...`
│   ├── config.py            configuration .env (pydantic-settings), refus du mode live
│   ├── safety.py            garde-fous PAPER_ONLY
│   ├── services.py          racine de composition (assemble tous les composants)
│   ├── logging_setup.py     logging console + fichier rotatif
│   ├── core/                types de domaine (Candle, Signal…) et bus d'événements
│   ├── market/              MarketDataProvider (Binance, CSV), indicateurs, timeframes
│   ├── strategy/            Strategy (abstraite) + EmaRsiStrategy — à remplacer librement
│   ├── risk/                RiskManager, taille de position, SL/TP, trailing, limites
│   ├── trading/             Broker (abstrait), PaperBroker, WalletBroker (bloqué), ordres,
│   │                        positions, portefeuille
│   ├── engine/              TradingEngine commun + ParameterSet versionnable
│   ├── backtest/            moteur, métriques, rapports JSON/CSV/HTML
│   ├── bot/                 boucle temps réel (STOPPED / RUNNING / PAUSED / HALTED)
│   ├── ai/                  conseiller Claude + optimiseur walk-forward + planification
│   ├── database/            modèles SQLAlchemy, connexion, repository
│   └── api/                 webhook TradingView, API du dashboard, WebSocket
├── dashboard/               interface web (HTML/CSS/JS, TradingView Lightweight Charts)
├── pine/tradingview_strategy.pine
├── supabase/migrations/     schéma PostgreSQL pour Supabase
├── data/BTCUSDT_1h.csv      historique figé pour des backtests reproductibles
├── tests/                   pytest (102 tests, aucun accès réseau)
├── .env.example  requirements.txt  pyproject.toml  Dockerfile  docker-compose.yml
```

Écarts par rapport à la structure demandée, et pourquoi :
- `engine/` : un moteur unique partagé par backtest et temps réel (exigence « mêmes règles »).
- `bot/`, `ai/`, `core/`, `services.py`, `cli.py` : séparation boucle temps réel / IA / types
  communs / assemblage / ligne de commande.
- `trading/broker.py` + `wallet_broker.py` + `factory.py` : l'abstraction `Broker` prévue pour
  un futur exécuteur via wallet, verrouillée dans cette version.
- `database/repository.py` : toute la persistance au même endroit, pour migrer facilement.

Pour **remplacer la stratégie** : créer une sous-classe de `Strategy` (`app/strategy/base.py`)
qui implémente `warmup_bars`, `analyze` et `indicator_series`, puis l'enregistrer dans
`STRATEGIES` (`app/strategy/__init__.py`).

---

## 3. Stratégie initiale et gestion du risque

**LONG** : EMA 20 > EMA 50, clôture > EMA 20, RSI 14 > 50. **SHORT** : EMA 20 < EMA 50,
clôture < EMA 20, RSI 14 < 50. Évaluation sur bougie **clôturée** uniquement ; le signal est
émis quand les conditions **deviennent** vraies (sinon le bot ré-entrerait à chaque bougie
après un stop). Un signal opposé clôture la position et ouvre l'inverse.

- **Stop-loss** = entrée ∓ ATR 14 × `STOP_LOSS_ATR_MULTIPLIER` ;
  **take-profit** = entrée ± distance du stop × `TAKE_PROFIT_RISK_REWARD`.
- **Taille** = `equity × RISK_PER_TRADE / risque réel par unité`, où le risque réel inclut la
  distance au stop, les frais d'entrée et de sortie et le slippage au stop. Exemple de la
  spécification (sans frais) : 10 000 × 1 % = 100 ; stop à 2 → **50 unités** (testé).
- Arrondi vers le bas à `QTY_STEP`, refus sous `MIN_QTY` / `MIN_NOTIONAL`, plafonné par le
  cash disponible et `MAX_POSITION_PCT` (pas de levier).
- `MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS` (entrées bloquées jusqu'au lendemain UTC),
  `MAX_DRAWDOWN` (**kill-switch** : entrées bloquées, positions clôturées, réinitialisation
  manuelle depuis le dashboard).
- Trailing stop optionnel (`TRAILING_STOP_ENABLED`), activé après `TRAILING_ACTIVATION_R` R.

**Règles d'exécution simulée** (identiques en backtest et en temps réel) : un ordre MARKET est
exécuté à l'ouverture de la première bougie ouverte **après** sa création, avec slippage
défavorable ; LIMIT au prix limite (ou meilleur) sans slippage ; stop-loss au niveau du stop
(ou à l'ouverture en cas de gap) avec slippage ; take-profit au niveau de l'objectif sans
slippage ; si stop et objectif sont touchés dans la même bougie, le **stop** est retenu
(hypothèse prudente). Chaque ordre a un identifiant unique (`PO-000142`).

---

## 4. Backtest

```bash
python -m app backtest --symbol BTCUSDT --timeframe 1h --csv data/BTCUSDT_1h.csv
```

Résultat réel obtenu avec les paramètres par défaut sur les données figées du dépôt
(BTCUSDT 1h, 13/04/2026 → 27/09/2026, 4 000 bougies) :

| Métrique | Valeur |
|---|---|
| Rendement total | **−10,07 %** |
| Trades | 32 |
| Win rate | 28,1 % |
| Gain moyen / perte moyenne | +125,90 / −93,04 USDT |
| Profit factor | 0,53 |
| Expectancy | −31,46 USDT par trade (−0,37 R) |
| Drawdown maximal | 10,07 % (le kill-switch s'est déclenché) |
| Sharpe annualisé | −2,83 |
| Frais payés | 482,22 USDT |

La stratégie initiale **perd de l'argent** sur cette période : elle sert à valider
l'architecture, pas à gagner. Le rapport affiche aussi période, timeframe, symbole et
paramètres, et écrit dans `reports/` un JSON complet (trades + courbe d'equity), un CSV de la
courbe d'equity et une page HTML noir et blanc. Deux exécutions sur les mêmes données donnent
exactement le même résultat (testé). Le Sharpe est annualisé sur une base 24/7 (crypto).

---

## 5. Connecter TradingView

### Ce que TradingView permet, et ce qu'il ne permet pas
- TradingView **n'offre pas d'API publique** pour lire les données d'un graphique ni pour
  envoyer des ordres vers un serveur tiers ; l'intégration « broker » est réservée aux
  courtiers partenaires.
- Le mécanisme officiel est l'**alerte avec webhook** : à chaque déclenchement, TradingView
  envoie un `POST` HTTP vers une URL. D'après la documentation TradingView (à revérifier, les
  conditions évoluent) : abonnement payant et authentification à deux facteurs requis, ports
  **80/443 uniquement** (donc HTTPS public), délai de réponse court, **pas d'en-têtes
  personnalisés** — le secret est donc placé dans le corps JSON.
- Les données de marché utilisées par le bot (stops, objectifs, signaux internes, graphique)
  viennent de l'API publique Binance. Le dashboard utilise **TradingView Lightweight Charts**
  (bibliothèque open source de TradingView).

Architecture retenue : **TradingView → alerte → webhook HTTPS → FastAPI → validation →
Risk Manager → Paper Broker → base de données → dashboard.**

### Étapes
1. **Exposer le serveur en HTTPS.** Le bot écoute sur `127.0.0.1:8000`. Utilisez un tunnel :
   ```bash
   cloudflared tunnel --url http://127.0.0.1:8000
   ```
   (ou `ngrok http 8000`, ou `docker compose --profile tunnel up -d`). Notez l'URL `https://…`.
2. **Configurer `.env`** : `SIGNAL_SOURCE=tradingview` (ou `both`), `SYMBOLS` contient le
   symbole du graphique, `TIMEFRAME` = timeframe du graphique (ex. `15m`). Relancez `serve`,
   puis **START** : un signal reçu quand le bot est arrêté est refusé (409).
3. **Ajouter le script** : TradingView → Pine Editor → coller `pine/tradingview_strategy.pine`
   → Enregistrer → Ajouter au graphique (ex. `BINANCE:BTCUSDT`, 15 min — spot, pas `.P`).
4. **Paramètres du script** : champ « WEBHOOK_SECRET » = valeur de `WEBHOOK_SECRET` du `.env` ;
   alignez risque, frais et slippage sur le `.env` (et la commission dans *Propriétés*).
5. **Créer l'alerte** : Alerte → Condition : « EMA/RSI Paper Bot » →
   **« alert() function calls only »** → Notifications : cocher **Webhook URL** =
   `https://<votre-tunnel>/webhook/tradingview`. Le message JSON est produit par le script.

### Exemple de signal et test manuel
```json
{"secret": "<WEBHOOK_SECRET>", "symbol": "BTCUSDT", "action": "LONG", "price": 84802.0,
 "atr": 180.5, "timestamp": 1790500977000, "bar_time": 1790500500000, "timeframe": "15",
 "strategy": "ema_rsi_tv"}
```
`timestamp` et `bar_time` : ISO 8601 ou epoch en millisecondes. Test sans TradingView
(bot démarré, `SIGNAL_SOURCE=tradingview` ou `both`) :
```bash
curl -X POST http://127.0.0.1:8000/webhook/tradingview -H "Content-Type: application/json" -d '{"secret":"<WEBHOOK_SECRET>","symbol":"BTCUSDT","action":"LONG","price":84802.0,"atr":180.5,"timestamp":"2026-09-27T10:22:57Z","timeframe":"15"}'
```
Remplacez le prix par le prix courant (écart max `WEBHOOK_MAX_PRICE_DEVIATION`, 2 %) et
l'horodatage par l'heure actuelle (âge max `WEBHOOK_MAX_SIGNAL_AGE_SECONDS`, 120 s).

| Code | Signification |
|---|---|
| 202 | Reçu ; `status` = `accepted` (ordre créé), `rejected` (Risk Manager) ou `ignored` |
| 400 / 413 | JSON invalide / corps trop volumineux |
| 401 | Secret absent ou incorrect (comparaison en temps constant, rien n'est stocké) |
| 403 | IP hors `TRADINGVIEW_IP_ALLOWLIST` (si renseignée) |
| 409 | Doublon, bot non démarré, ou `SIGNAL_SOURCE=internal` |
| 422 | Schéma, symbole, sens, horodatage, timeframe ou prix invalide |
| 429 | Limite de débit dépassée |
| 503 | `WEBHOOK_SECRET` absent ou trop faible : webhook désactivé |

Tout signal authentifié (accepté ou non) est enregistré dans la table `signals`, sans le secret.

---

## 6. Dashboard

Interface sobre noir et blanc (bouton ◐ pour inverser), servie sur `/dashboard/`, mise à jour
en temps réel par WebSocket : statut du bot, capital initial, balance, equity, P&L, P&L %,
drawdown, nombre de trades, win rate, profit factor, positions ouvertes avec P&L latent,
ordres en attente, graphique en chandeliers avec EMA, marqueurs d'entrée/sortie et niveaux
entrée/SL/TP, courbe d'equity, derniers trades, derniers signaux, journal, état du risque,
panneau Claude et wallet. Boutons **START / PAUSE / STOP** (et réinitialisation du
kill-switch) — **aucun bouton ne permet le trading réel** ; `PUT /api/mode` avec
`{"mode": "live"}` répond toujours 403 et journalise une alerte CRITICAL.

Toutes les routes `/api/*` et le WebSocket exigent le `DASHBOARD_TOKEN`. Si le serveur est
exposé par un tunnel, le dashboard l'est aussi : gardez un jeton long et secret.

---

## 7. Amélioration continue avec Claude Opus 5.5

Activez-la dans `.env` : `ANTHROPIC_API_KEY=...`, `AI_OPTIMIZER_ENABLED=true`
(planification toutes les `AI_OPTIMIZER_INTERVAL_HOURS`) ; le bouton **Optimiser
maintenant** et `python -m app optimize` lancent un cycle à la demande.

À chaque cycle :
1. l'historique (`AI_TRAIN_BARS` + `AI_VALIDATION_BARS` bougies par symbole) est coupé en
   **entraînement** et **validation hors-échantillon** ;
2. Claude (`claude-opus-5-5`, sortie structurée validée par schéma, effort `AI_EFFORT`) reçoit
   les paramètres actuels, l'espace de recherche, les métriques d'entraînement, des
   statistiques de marché, les résultats paper récents et l'historique des cycles précédents,
   et propose `AI_CANDIDATES` jeux de paramètres justifiés ;
3. chaque candidat est vérifié (bornes) puis backtesté avec le **même moteur** ;
4. il n'est retenu que s'il améliore le score de validation d'au moins `AI_MIN_IMPROVEMENT`,
   ne dégrade pas l'entraînement, fait au moins `AI_MIN_TRADES` trades et reste sous
   `MAX_DRAWDOWN` ; il est alors appliqué à chaud (`AI_AUTO_APPLY=true`) ou proposé pour
   validation manuelle dans le dashboard.

Garde-fous : Claude ne passe aucun ordre, ne voit jamais la période de validation et ne peut
pas modifier les limites de risque (risque par trade, perte journalière, drawdown…). Chaque
jeu de paramètres est versionné (`strategy_versions`) ; chaque cycle est tracé
(`optimization_runs` : analyse, candidats, scores, décision). Coût : un appel API par cycle.

---

## 8. Base de données et Supabase

SQLite par défaut (`data/trading_bot.db`). Tables : `trades`, `orders`, `positions`,
`equity_snapshots`, `signals`, `bot_events`, plus `strategy_versions`, `optimization_runs` et
`bot_state`. L'état (balance, positions ouvertes, kill-switch, compteurs d'ordres) est
**restauré au redémarrage**, avec rattrapage des stops/objectifs sur les bougies manquées.

Pour utiliser Supabase (projet `kyfpiltdbfnhbxzdqocl`) :
1. Supabase → **SQL Editor** → exécuter
   `supabase/migrations/20260927000000_init_trading_bot.sql` (ou `supabase db push`).
2. Supabase → **Connect** → copier la chaîne de connexion *Session pooler* (port 5432).
3. Dans `.env` : `DATABASE_URL=postgresql://postgres.kyfpiltdbfnhbxzdqocl:<MOT_DE_PASSE>@<hôte-pooler>:5432/postgres`
   (le préfixe est converti automatiquement vers le driver `psycopg`).

La migration active RLS **sans politique** sur toutes les tables : les clés publiques de
Supabase n'y ont aucun accès via l'API REST ; seul le bot, connecté directement à PostgreSQL,
lit et écrit.

---

## 9. Docker

```bash
docker compose up -d --build                     # dashboard sur http://127.0.0.1:8000
docker compose --profile tunnel up -d --build    # + tunnel HTTPS cloudflared pour TradingView
docker compose logs tunnel                       # affiche l'URL https://….trycloudflare.com
```

---

## 10. Wallet (MetaMask) : ce qui est préparé

- **Abstraction `Broker`** : le moteur ne dépend que de cette interface ; un exécuteur réel
  s'y brancherait sans toucher à la stratégie ni au risque.
- **`WalletBroker`** (`app/trading/wallet_broker.py`) : emplacement prévu, **désactivé** —
  toute instanciation lève `LiveTradingDisabledError`. Il ne contient aucun code de signature.
- **Dashboard** : connexion MetaMask **en lecture seule** (`eth_requestAccounts`,
  `eth_chainId`, `eth_getBalance`) : adresse, réseau et solde affichés, aucune transaction
  demandée ni signée.

Passer au réel ne sera **pas** un changement de configuration : il faudra implémenter
l'exécution (choix d'un DEX / agrégateur, signature de chaque transaction par l'utilisateur
dans MetaMask — le serveur ne doit jamais détenir de clé privée —, approvals, gas, slippage
on-chain, confirmations et échecs), la faire auditer, puis retirer délibérément les
garde-fous ci-dessous.

---

## 11. Sécurité PAPER_ONLY

| Protection | Où | Test |
|---|---|---|
| `PAPER_ONLY=false` ou `TRADING_MODE≠paper` → refus de démarrer (env, `.env` ou code) | `config.py`, `safety.py` | `test_paper_only.py` |
| Seul `PaperBroker` (`is_live=False`) est constructible | `trading/factory.py` | idem |
| `WalletBroker` lève toujours une erreur | `trading/wallet_broker.py` | idem |
| Refus de démarrer si des clés d'exchange, clés privées ou seed phrases sont présentes | `safety.py`, `services.py` | idem |
| Client HTTP de marché limité à `GET /api/v3/klines`, sans méthode POST | `market/data_provider.py` | idem |
| Aucun code d'ordre réel ou de signature (scan du code et du dashboard) | — | idem |
| Aucun bouton « live » ; `PUT /api/mode` live → 403 + log CRITICAL | `api/dashboard.py` | `test_api.py` |
| Bannière d'avertissement au démarrage, badge PAPER permanent dans l'interface | `safety.py`, `dashboard/` | — |

---

## 12. Configuration principale (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `INITIAL_CAPITAL` | 10000 | Capital du compte paper (figé au premier lancement ; `reset-paper` pour changer) |
| `RISK_PER_TRADE` | 0.01 | Risque par trade (max 5 %) |
| `MAX_OPEN_POSITIONS` | 3 | Positions simultanées |
| `MAX_DAILY_LOSS` / `MAX_DRAWDOWN` | 0.03 / 0.10 | Limites de compte |
| `STOP_LOSS_ATR_MULTIPLIER` / `TAKE_PROFIT_RISK_REWARD` | 2 / 2 | Sorties initiales |
| `SLIPPAGE` / `TRADING_FEES` | 0.0005 / 0.001 | Coûts simulés |
| `SYMBOLS` / `TIMEFRAME` | BTCUSDT,ETHUSDT / 15m | Marchés suivis |
| `SIGNAL_SOURCE` | internal | `internal`, `tradingview` ou `both` |
| `DATABASE_URL` | SQLite | SQLite ou PostgreSQL / Supabase |
| `AI_*` | — | Optimiseur Claude (§7) |

Logs : console + `logs/trading_bot.log` (rotation 5 × 5 Mo), niveaux INFO / WARNING / ERROR /
CRITICAL, par exemple :
```text
[INFO] app.engine.trading_engine: Signal LONG détecté sur BTCUSDT (tradingview) — ...
[INFO] app.risk.risk_manager: Risk Manager: risque autorisé = 100.00 USDT
[INFO] app.trading.paper_broker: Paper Order #1 créé : MARKET BUY BTCUSDT qty=0.1177 (...)
[INFO] app.trading.paper_broker: Position ouverte à 84858.788 (LONG BTCUSDT qty=0.1177)
[INFO] app.trading.paper_broker: Stop Loss = 84497.788
[INFO] app.trading.paper_broker: Take Profit = 85580.788
```

---

## 13. Limitations connues

- **Non testé en conditions réelles** : le Pine Script n'a pas été compilé dans TradingView ;
  aucune alerte TradingView réelle n'a atteint le webhook (le webhook a été testé avec des
  requêtes identiques envoyées localement) ; l'API Claude n'a pas été appelée (tests avec un
  client simulé) ; la migration Supabase n'a pas été appliquée et le mode PostgreSQL n'a pas
  été exécuté (seul SQLite l'a été) ; les fichiers Docker n'ont pas été construits.
- Hypothèses de simulation : pas d'exécution partielle ni de carnet d'ordres, pas de frais de
  financement pour les shorts, pas de levier, stop prioritaire si stop et objectif sont
  touchés dans la même bougie.
- Les EMA sont calculées sur une fenêtre glissante (`LOOKBACK_BARS`) : écart négligeable mais
  non nul avec TradingView, qui calcule depuis le début de l'historique.
- L'anti-rejeu du webhook est en mémoire (perdu au redémarrage ; l'âge maximal des signaux
  limite le risque). Un seul processus : l'état du bot n'est pas partagé entre instances.
- L'API publique Binance peut être inaccessible depuis certains pays : remplacez le
  `MarketDataProvider` si besoin.
- La stratégie par défaut est perdante sur la période testée ; l'optimiseur ne garantit
  aucune performance future (risque de sur-apprentissage malgré la validation hors-échantillon).
