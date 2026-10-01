# Bot de trading multi-marchés piloté par Claude — PAPER TRADING

> **Aucun ordre réel ne peut être envoyé.** Les prix sont réels et en direct ; les ordres
> sont simulés avec les frais, le slippage, les tailles minimales et les horaires d'un vrai
> compte, comme le paper trading de TradingView. Ce projet n'est pas un conseil en
> investissement ; aucune performance future n'est garantie.

Claude (Opus 5.5) choisit lui-même, parmi 14 marchés (crypto, actions US, ETF S&P 500 /
Nasdaq / CAC 40, forex), les meilleures opportunités compte tenu de son capital, de ses
positions et de son historique. Il fixe l'entrée, le **stop loss**, le **take profit**, la
taille et l'horizon (intraday, swing ou long terme). Un Risk Manager codé a le dernier mot
sur chaque chiffre. Le bot apprend de chacun de ses trades, sans intervention.

---

## 1. Démarrage rapide (sur votre PC)

**Windows, sans ligne de commande** : double-cliquez sur **`Lancer le bot.bat`**. La première fois,
il installe tout (2-3 minutes) et ouvre le fichier `.env` pour y coller votre clé Claude
(`ANTHROPIC_API_KEY=`) ; relancez-le ensuite. Le dashboard s'ouvre tout seul dans le
navigateur, déjà connecté. Fermer la fenêtre noire arrête le bot.
**`Tester les agents IA.bat`** vérifie la clé et chacun des 5 agents (décision, revue,
leçon, actualités, optimisation) sans passer d'ordre, pour environ 0,40 $.

En ligne de commande :

```bash
git clone https://github.com/antoinesaas/trading.git
cd trading
python -m venv .venv
# Windows : .venv\Scripts\activate    —    macOS / Linux : source .venv/bin/activate
pip install -r requirements.txt
python -m app init-env            # crée .env (secrets aléatoires)
# éditez .env : ANTHROPIC_API_KEY=...  (et DATABASE_URL pour Supabase, voir §9)
pytest                            # 158 tests, sans réseau
python -m app serve               # http://127.0.0.1:8000
```

Ouvrez le dashboard, saisissez le `DASHBOARD_TOKEN` du fichier `.env`, puis **Start**.

| Commande | Rôle |
|---|---|
| `python -m app serve` | Bot + dashboard + API + webhook TradingView |
| `python -m app check-ai` | Teste la clé API et chaque agent IA (aucun ordre) |
| `python -m app backtest --symbol BTCUSDT --timeframe 4h --csv data/BTCUSDT_4h.csv` | Backtest reproductible du scanner |
| `python -m app fetch-data --symbol ETHUSDT --timeframe 4h --bars 3000` | Télécharge et fige un historique crypto |
| `python -m app reset-paper --yes` | Remet le compte paper à zéro |

---

## 2. Comment le bot fonctionne, étape par étape

1. **Flux de prix permanent.** Dès que le serveur tourne, il lit les prix réels : Binance
   pour la crypto (toutes les 10 s), Yahoo Finance pour les actions, ETF et forex (chaque
   minute quand le marché est ouvert, toutes les 15 min sinon). Ce flux tourne même bot
   arrêté : les graphiques restent en direct et les stops des positions ouvertes sont
   toujours surveillés.
2. **Scanner.** À chaque clôture de bougie 1h, un score technique de 0 à 100 est calculé pour
   chaque marché (tendance EMA 20/50/200, RSI, ADX, MACD, Bollinger, volume, ATR).
3. **Filtres gratuits.** Avant tout appel à Claude : bot en marche, marché ouvert (horaires
   NYSE, Euronext, forex, crypto 24/7, jours fériés), pas d'annonce économique majeure dans
   les 30 min, limites de perte non atteintes, nombre de positions sous le maximum.
4. **Classement.** Les marchés ouverts les mieux notés passent en premier (2 analyses par
   cycle au maximum, pour maîtriser le coût).
5. **Contexte réel envoyé à Claude** : analyse 1h / 4h / 1j / 1 semaine (RSI 7/14/21,
   divergences RSI, volume relatif, MFI, OBV, supports et résistances…), son **milieu
   d'influence** (pour NVIDIA : Nasdaq 100, semi-conducteurs, VIX, taux US 10 ans ; pour le
   CAC 40 : Euro Stoxx 50, EUR/USD, S&P 500 ; pour le forex : dollar index, taux…),
   actualités et agenda économique (Fed, BCE, BoE, BoJ, résultats d'entreprises) trouvés par
   recherche web, panorama de tous les marchés, portefeuille, et ses leçons passées.
6. **Décision de Claude** : ouvrir à l'achat ou à la vente, ou attendre ; ordre au marché ou
   limite ; stop loss, take profit, prise de profit partielle, passage au point mort,
   durée maximale, horizon et pourcentage du capital risqué, avec une thèse écrite.
7. **Risk Manager (non contournable).** Stop entre 0,5 et 6 ATR, rendement/risque ≥ 1,5,
   risque ≤ 2 % du capital par trade, ≤ 5 % au total et ≤ 3 % dans le même sens, taille
   calculée dans la devise du compte (ETF CAC 40 en EUR, USD/JPY en JPY), frais et
   slippage inclus. Toute proposition hors règles est refusée et journalisée.
8. **Exécution simulée** au prix réel, avec les frais et le slippage propres à chaque marché
   (crypto 0,10 %, actions 0,05 %, forex 0,005 %) et les tailles minimales (1 action, 1 000
   unités de devise…).
9. **Gestion de la position** : stop loss et take profit toujours posés ; prise de profit
   partielle puis stop au point mort ; stop suiveur ; clôture si le trade ne progresse pas ;
   revue par Claude toutes les 2 h (il peut resserrer le stop ou clôturer, jamais élargir le
   stop).
10. **Apprentissage.** À chaque trade clôturé, Claude rédige une leçon (ce qui a marché, ce
    qui a échoué, une règle pour la suite) réinjectée dans ses décisions suivantes. Le seuil
    de confiance minimal monte automatiquement si les trades pris à faible confiance perdent,
    et le scanner est réoptimisé chaque jour (validé hors échantillon avant d'être appliqué).
    Il n'y a pas de bouton « optimiser » : tout est automatique.
11. **Sécurités globales** : −3 % sur la journée ou −6 % sur la semaine = plus de nouvelles
    entrées ; 4 pertes d'affilée = pause de 6 h ; −10 % de drawdown = kill-switch et
    clôture des positions.

### Marchés suivis

| Classe | Marchés | Source | Horaires |
|---|---|---|---|
| Crypto | BTC, ETH, SOL (en USDT) | Binance, temps réel | 24/7 |
| Actions US | NVIDIA, Tesla, Netflix, Apple, Microsoft | Yahoo Finance | NYSE/Nasdaq, 15h30-22h (Paris) |
| ETF | S&P 500 (SPY), Nasdaq 100 (QQQ), CAC 40 (Amundi, en EUR) | Yahoo Finance | NYSE ; Euronext 9h-17h30 |
| Forex | EUR/USD, GBP/USD, USD/JPY | Yahoo Finance | dimanche 23h - vendredi 23h (Paris) |

Ajouter un marché : une ligne dans `app/market/universe.py` et son symbole dans `SYMBOLS`.

---

## 3. Dashboard (style TradingView)

- Graphique en chandeliers **en direct** (flux Binance à la seconde pour la crypto ; mise à
  jour chaque minute pour les autres marchés), unités de temps 1h, 4h, 1J, 1S, légende OHLC
  au survol, volume, EMA 20/50, RSI 14 dans un volet séparé.
- Ce que fait le bot, sur la courbe : flèches d'**achat / vente**, points de **sortie** avec
  le P&L et la raison (SL, TP, TP1, trailing), **zone de la position ouverte** (verte jusqu'à
  l'objectif, rouge jusqu'au stop) avec les lignes entrée / stop / take profit / TP1,
  **supports et résistances**, **structure de marché** (sommets et creux HH, HL, LH, LL) et
  décisions de Claude (carrés violets avec la confiance).
- Liste des 14 marchés avec prix, variation sur 24 h, marché ouvert ou fermé, position.
- Décisions de Claude, positions, risque et limites, horaires des marchés et actualités,
  auto-apprentissage (taux de réussite de Claude, calibration, leçons), check-list
  « prêt pour l'argent réel », courbe d'equity, historique complet.
- Bouton **Capital** : change le capital de base (équivaut à un dépôt ou un retrait :
  l'historique est conservé et les limites de risque s'adaptent).

---

## 4. Coûts de l'API Claude

Mesurés en réel : décision Opus 5.5 ≈ 0,03-0,05 $, briefing avec recherche web (Sonnet 5)
≈ 0,19 $ ; une leçon après trade (Sonnet 5) coûte quelques centimes. Usage typique : 1 à 4 $
par jour. `AI_DAILY_BUDGET_USD` (5 $ par
défaut) plafonne la dépense ; au-delà, plus de nouvelle décision IA jusqu'au lendemain (les
stops restent gérés).

---

## 5. Résultats honnêtes

Backtests du **scanner seul** (Claude ne peut pas être backtesté honnêtement : ses
connaissances couvrent déjà ces périodes), BTC + ETH + SOL, frais et slippage inclus :

| Période | Meilleure configuration | Rendement moyen | Taux de réussite |
|---|---|---|---|
| 4h, 500 jours | EMA/RSI | −0,23 % | 34,8 % |
| 1h, 1 an | trailing stop | −2,66 % | 32,9 % |
| 1h, 5,5 mois | confluence tendance | −0,84 % | 28,2 % |

Aucune règle technique simple n'est rentable de façon robuste après frais, et un taux de
réussite élevé n'implique pas des gains. La valeur ajoutée de Claude se mesure **en paper
trading, en avant** : le dashboard affiche sa calibration et le résultat de chaque décision.

---

## 6. Prêt pour de l'argent réel ? (check-list)

Le panneau « Prêt pour l'argent réel ? » l'évalue en continu sur les résultats réels :

| Critère | Condition |
|---|---|
| Historique | ≥ 60 jours et ≥ 100 trades de paper trading |
| Rentabilité | profit factor ≥ 1,3 après frais |
| Drawdown | < 80 % de la limite |
| Calibration de Claude | trades à forte confiance gagnants en moyenne |
| Stabilité technique | aucune source de données en erreur |
| 24/7 | hébergé sur un serveur (voir §7) |
| Exécution réelle | **verrouillée** : à développer, tester en petit et auditer |
| Cadre légal | broker ou exchange régulé, déclarations fiscales |

Aujourd'hui la réponse est **NON**, par construction. MetaMask ne permet de trader que des
crypto sur des exchanges décentralisés : les actions, ETF et le forex exigent un broker
régulé avec API (par exemple Interactive Brokers). L'architecture le prévoit (interface
`Broker`, `WalletBroker` en lecture seule), mais aucun code d'envoi d'ordre ou de signature
n'existe et plusieurs garde-fous l'interdisent (`app/safety.py`, `tests/test_paper_only.py`).

---

## 7. Fonctionnement 24 h/24 (PC éteint)

Sur votre PC, le bot s'arrête quand le PC s'éteint. Pour qu'il tourne en continu :
**Render** + **Supabase**.

1. Supabase : les tables sont créées dans le projet `trading` (RLS actif). Appliquez aussi
   `supabase/migrations/20260927150000_multi_market.sql` (le bot le fait seul au démarrage).
2. Récupérez le mot de passe de la base (Supabase > Project Settings > Database) :
   `postgresql://postgres.kyfpiltdbfnhbxzdqocl:MOT_DE_PASSE@aws-1-eu-west-1.pooler.supabase.com:5432/postgres`
3. Render (https://render.com) > **New > Blueprint** > dépôt `antoinesaas/trading` : le fichier
   `render.yaml` configure tout. Render demande `DATABASE_URL` et `ANTHROPIC_API_KEY` ;
   `WEBHOOK_SECRET` et `DASHBOARD_TOKEN` sont générés (visibles dans Render > Environment).
4. Le bot démarre seul (`BOT_AUTO_START=true`) ; le dashboard est accessible sur
   `https://<votre-service>.onrender.com`, depuis n'importe quel appareil.

Le plan Render « Starter » (environ 7 $/mois) est nécessaire : l'offre gratuite se met en
veille. Alternative : un VPS avec Docker (`docker compose up -d --build`). Chaque push sur
`main` redéploie le bot ; GitHub Actions lance les tests. L'état (positions, balance,
kill-switch, compteurs) est restauré au redémarrage et les stops manqués pendant une coupure
sont rattrapés.

---

## 8. TradingView

TradingView n'offre pas d'API pour lire un graphique ou passer des ordres ; le mécanisme
officiel est l'**alerte webhook** (`/webhook/tradingview`, secret obligatoire).

1. Pine Editor > coller `pine/tradingview_strategy.pine` > ajouter au graphique.
   Paramètre « WEBHOOK_SECRET » = la valeur du bot.
2. Alerte > Condition : ce script > « alert() function calls only » > Webhook URL
   `https://<votre-service>.onrender.com/webhook/tradingview`.
3. `SIGNAL_SOURCE=tradingview` ou `both`. En mode IA, chaque alerte est soumise à Claude.

---

## 9. Configuration (`.env`)

Toutes les variables sont listées et commentées dans `.env.example`. Principales :

| Variable | Défaut | Rôle |
|---|---|---|
| `SYMBOLS` | 14 marchés | Marchés suivis |
| `TIMEFRAME` / `CONTEXT_TIMEFRAMES` | 1h / 1h,4h,1d,1w | Bougie de décision / analyse |
| `MARKET_DATA_PROVIDER` | live | Binance + Yahoo Finance, sans clé |
| `INITIAL_CAPITAL` / `ACCOUNT_CURRENCY` | 10000 / USD | Capital (modifiable dans le dashboard) |
| `DECISION_MODE` | ai | `ai` : Claude décide ; `rules` : scanner seul |
| `AI_MIN_CONFIDENCE` | 0.65 | Seuil de départ (relevé automatiquement si besoin) |
| `AI_DAILY_BUDGET_USD` | 5 | Plafond de dépense API par jour |
| `HARD_MAX_RISK_PER_TRADE` | 0.02 | Risque max par trade, quoi que demande Claude |
| `MAX_OPEN_POSITIONS` | 5 | Positions simultanées |
| `MAX_PORTFOLIO_RISK` / `MAX_CORRELATED_RISK` | 0.05 / 0.03 | Risque total / même sens |
| `MAX_DAILY_LOSS` / `MAX_WEEKLY_LOSS` / `MAX_DRAWDOWN` | 3 % / 6 % / 10 % | Limites de perte |
| `DATABASE_URL` | SQLite | Supabase en production |

---

## 10. Limitations connues

- Performance non prouvée : voir §5.
- Données Yahoo Finance : API publique non officielle, rafraîchie chaque minute, qui peut
  limiter les requêtes depuis certains hébergeurs ; certaines places (Euronext) peuvent être
  différées d'environ 15 min. Pour du réel, un flux de données
  du broker est préférable.
- Conversion de devise figée à l'entrée de la position (l'effet de change pendant le trade
  est négligé).
- Simulation : pas d'exécution partielle ni de carnet réel pour les actions et le forex, pas
  de frais de financement (swap, emprunt de titres pour la vente à découvert).
- Les demi-séances boursières (veille de Noël…) ne sont pas modélisées.
- Recherche web : Claude peut manquer ou mal dater une information.
- Le Pine Script n'a pas été compilé dans TradingView ; déploiement Render non testé ici
  (nécessite votre compte et le mot de passe Supabase).
