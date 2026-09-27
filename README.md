# Bot de trading piloté par Claude — PAPER TRADING

> **Aucun ordre réel ne peut être envoyé.** Tous les ordres sont simulés, avec les frais,
> le slippage et les limites d'un vrai compte. Ce projet n'est pas un conseil en
> investissement ; aucune performance future n'est garantie.

Claude (Opus 5.5) prend les décisions de trading à partir d'un contexte complet et réel :
analyse technique multi-timeframe, carnet d'ordres, dérivés, sentiment, heures de marché,
actualités et calendrier économique trouvés par recherche web, état du portefeuille et
historique de ses propres décisions. Un Risk Manager codé a le dernier mot sur chaque
chiffre. Le bot tourne 24 h/24 dans le cloud, avec Supabase comme base de données.

---

## 1. Démarrage rapide (sur votre PC)

```bash
git clone https://github.com/antoinesaas/trading.git
cd trading
python -m venv .venv
# Windows : .venv\Scripts\activate    —    macOS / Linux : source .venv/bin/activate
pip install -r requirements.txt
python -m app init-env            # crée .env (secrets aléatoires)
# éditez .env : ANTHROPIC_API_KEY=...  (et DATABASE_URL pour Supabase, voir §7)
pytest                            # 133 tests, sans réseau
python -m app serve               # http://127.0.0.1:8000
```

Ouvrez le dashboard, saisissez le `DASHBOARD_TOKEN` du fichier `.env`, puis **START**.

| Commande | Rôle |
|---|---|
| `python -m app serve` | Bot + dashboard + API + webhook TradingView |
| `python -m app backtest --symbol BTCUSDT --timeframe 4h --csv data/BTCUSDT_4h.csv` | Backtest reproductible du scanner |
| `python -m app fetch-data --symbol ETHUSDT --timeframe 4h --bars 3000` | Télécharge et fige un historique |
| `python -m app optimize` | Un cycle d'optimisation du scanner par Claude |
| `python -m app reset-paper --yes` | Remet le compte paper à zéro |

---

## 2. Comment le bot décide

```text
Toutes les 10 s : prix Binance -> stops, prises de profit, point mort, trailing, time-stop
À chaque clôture de bougie 4h :
  Scanner (score de confluence 0-100)  ──►  score suffisant ?
  Blocages gratuits : pause, limites de perte, positions max, heures, blackout d'annonce
  Contexte réel  ──►  Claude Opus 5.5  ──►  OUVRIR / TENIR / (revue : AJUSTER / CLÔTURER)
  Risk Manager : stop 0,5-6 ATR, rendement/risque >= 1,5, % de risque plafonné, heat, corrélation
  Paper Broker : exécution simulée (frais, slippage), journalisation complète
Toutes les 2 h  : revue de chaque position ouverte par Claude (Sonnet 5)
Toutes les 4 h  : briefing d'actualité (recherche web) -> fenêtres de blackout
```

**Ce que Claude décide** : l'action, le type d'ordre (marché ou limite), le stop, l'objectif,
la prise de profit partielle, le passage au point mort, le time-stop et le **pourcentage du
capital risqué**. Il reçoit sa propre calibration (taux de réussite réel par niveau de
confiance) et ses derniers trades commentés : il apprend de ses résultats.

**Ce que Claude ne peut pas faire** (codé, non contournable) : dépasser 2 % de risque par
trade, élargir un stop, dépasser 5 % de risque total ouvert ou 3 % dans le même sens (BTC,
ETH et SOL sont très corrélés), ouvrir au-delà de 3 positions, trader pendant un blackout,
après −3 % sur la journée, −6 % sur la semaine, 4 pertes consécutives (pause de 6 h) ou −10 %
de drawdown (kill-switch). Toute proposition hors de ces règles est refusée et journalisée.

### Contexte réel transmis à Claude
- Analyse 1h / 4h / 1j : tendance, EMA 20/50/200, RSI, ADX, ATR, MACD, Bollinger, volume,
  supports et résistances, dernières bougies.
- Carnet d'ordres (spread, déséquilibre acheteurs/vendeurs), statistiques 24 h.
- Dérivés Binance Futures : funding, open interest et variation 24 h, ratio long/short,
  flux acheteurs/vendeurs agressifs. Indice Crypto Fear & Greed.
- Heures de marché : sessions Asie / Europe / US, chevauchement Londres-New York, jours fériés
  NYSE, horaires du future Bitcoin CME, week-end, liquidité.
- Briefing d'actualité par recherche web : faits marquants, calendrier économique US/EU avec
  heures, biais par actif ; les annonces à fort impact bloquent les entrées ± 30 min.
- Portefeuille, limites, calibration et journal des décisions précédentes.

### Money management
Le risque demandé est plafonné par la confiance (50 % du plafond à la confiance minimale),
par un Kelly fractionné (¼ de Kelly une fois 20 trades réalisés), réduit en drawdown (jusqu'à
¼ du risque au drawdown maximal) et après des pertes consécutives, puis limité par le risque
total ouvert et le risque corrélé. La taille de position intègre frais et slippage.

### Gestion des positions
Prise de profit partielle (TP1) avec passage automatique du stop au point mort, point mort
à +x R, trailing stop, time-stop (clôture d'un trade qui ne progresse pas), révision par
Claude (resserrer le stop, déplacer l'objectif, clôturer). En temps réel, une décision prise
en cours de bougie est exécutée au dernier prix, et ses stops ignorent les prix antérieurs.

---

## 3. Coûts de l'API Claude

Mesurés en réel : décision Opus 5.5 ≈ 0,03-0,05 $, briefing avec recherche web (Sonnet 5)
≈ 0,19 $, optimisation ≈ 0,06 $. Usage typique : 1 à 3 $ par jour.
`AI_DAILY_BUDGET_USD` (5 $ par défaut) plafonne la dépense ; au-delà, le bot ne prend plus de
nouvelle décision IA jusqu'au lendemain (les stops restent gérés). Le suivi est dans le
dashboard et la table `ai_usage`.

---

## 4. Résultats honnêtes

Backtests du **scanner seul** (Claude ne peut pas être backtesté honnêtement : ses
connaissances couvrent déjà ces périodes), BTC + ETH + SOL, frais et slippage inclus :

| Période | Meilleure configuration | Rendement moyen | Taux de réussite |
|---|---|---|---|
| 4h, 500 jours | EMA/RSI | −0,23 % | 34,8 % |
| 1h, 1 an | trailing stop | −2,66 % | 32,9 % |
| 1h, 5,5 mois | confluence tendance | −0,84 % | 28,2 % |

Aucune règle technique simple n'est rentable de façon robuste après frais sur ces périodes,
et un taux de réussite plus élevé n'implique pas plus de gains (retour à la moyenne avec
prise partielle : 38 % de réussite mais −4,6 %). Le premier cycle d'optimisation par Claude
a amélioré le score hors-échantillon du scanner (−4,25 contre −5,18) sans le rendre
positif. La valeur ajoutée de Claude se mesure donc **en paper trading, en avant** : le
dashboard affiche sa calibration et le résultat de chaque décision. Laissez tourner
plusieurs semaines avant toute conclusion.

---

## 5. Fonctionnement 24 h/24 (PC éteint)

Le bot doit tourner sur un serveur. Le plus simple : **Render** + **Supabase**.

1. Supabase (déjà fait) : les 12 tables sont créées dans le projet `trading` avec RLS actif.
2. Récupérez le mot de passe de la base (Supabase > Project Settings > Database ; vous pouvez
   le réinitialiser si besoin) et composez :
   `postgresql://postgres.kyfpiltdbfnhbxzdqocl:MOT_DE_PASSE@aws-1-eu-west-1.pooler.supabase.com:5432/postgres`
3. Render (https://render.com) > **New > Blueprint** > dépôt `antoinesaas/trading` : le fichier
   `render.yaml` configure tout. Render vous demande `DATABASE_URL` et `ANTHROPIC_API_KEY` ;
   `WEBHOOK_SECRET` et `DASHBOARD_TOKEN` sont générés (visibles dans Render > Environment).
4. Le bot démarre seul (`BOT_AUTO_START=true`) ; le dashboard est accessible sur
   `https://<votre-service>.onrender.com`, depuis n'importe quel appareil.

Le plan Render « Starter » (payant, environ 7 $/mois) est nécessaire : l'offre gratuite se met
en veille et arrêterait le bot. Alternative : n'importe quel VPS avec Docker
(`docker compose up -d --build`, voir `docker-compose.yml`). Chaque push sur `main` redéploie
le bot ; GitHub Actions lance les tests (`.github/workflows/tests.yml`).

L'état (positions, balance, kill-switch, compteurs) est restauré au redémarrage et les
stops manqués pendant une coupure sont rattrapés sur les bougies passées.

---

## 6. TradingView

TradingView n'offre pas d'API pour lire un graphique ou envoyer des ordres ; le mécanisme
officiel est l'**alerte webhook**. Avec le bot sur Render, l'URL publique HTTPS existe déjà :
`https://<votre-service>.onrender.com/webhook/tradingview`.

1. Pine Editor > coller `pine/tradingview_strategy.pine` > ajouter au graphique (ex.
   `BINANCE:BTCUSDT`, 4h). Paramètre « WEBHOOK_SECRET » = la valeur du bot.
2. Alerte > Condition : ce script > « alert() function calls only » > Webhook URL ci-dessus.
3. `SIGNAL_SOURCE=tradingview` ou `both`. En mode IA, chaque alerte est soumise à Claude,
   qui décide (réponse immédiate `queued`).

Les webhooks TradingView exigent un abonnement payant et la double authentification.
Exemple de message et codes de réponse : voir `app/api/webhook.py`.

---

## 7. Configuration (`.env`)

Toutes les variables sont listées et commentées dans `.env.example`. Principales :

| Variable | Défaut | Rôle |
|---|---|---|
| `DECISION_MODE` | ai | `ai` : Claude décide ; `rules` : scanner seul |
| `AI_DECISION_MODEL` / `AI_REVIEW_MODEL` / `AI_BRIEFING_MODEL` | Opus 5.5 / Sonnet 5 / Sonnet 5 | Modèles |
| `AI_MIN_CONFIDENCE` | 0.65 | Confiance minimale pour exécuter |
| `AI_DAILY_BUDGET_USD` | 5 | Plafond de dépense API par jour |
| `HARD_MAX_RISK_PER_TRADE` | 0.02 | Risque max par trade, quoi que demande Claude |
| `MAX_PORTFOLIO_RISK` / `MAX_CORRELATED_RISK` | 0.05 / 0.03 | Risque total / même sens |
| `MAX_DAILY_LOSS` / `MAX_WEEKLY_LOSS` / `MAX_DRAWDOWN` | 3 % / 6 % / 10 % | Limites de perte |
| `MAX_CONSECUTIVE_LOSSES` / `COOLDOWN_HOURS` | 4 / 6 | Pause après une série de pertes |
| `MIN_REWARD_RISK`, `MIN_STOP_ATR`, `MAX_STOP_ATR` | 1.5, 0.5, 6 | Validation des niveaux |
| `TRADE_WEEKENDS`, `NO_TRADE_HOURS_UTC` | true, vide | Fenêtres de trading |
| `SYMBOLS`, `TIMEFRAME` | BTC, ETH, SOL ; 4h | Marchés et timeframe de décision |
| `DATABASE_URL` | SQLite | Supabase en production |

---

## 8. Dashboard

Thème sombre par défaut (bouton ◐ pour le mode clair), mis à jour en temps réel : KPI du
compte, graphique en chandeliers avec EMA, entrées, sorties et niveaux (entrée, SL, TP, TP1),
courbe d'equity, positions avec P&L latent, **décisions de Claude en direct** (thèse,
confiance, statut, résultat en R), budget API, sessions de marché et briefing d'actualité,
risque et limites, historique (trades, signaux, décisions IA, journal). Boutons START / PAUSE
/ STOP, « Analyser maintenant » et « Rafraîchir le briefing ».

---

## 9. Passer un jour à l'argent réel

L'architecture le prévoit (interface `Broker`, `WalletBroker` pour MetaMask, connexion wallet
en lecture seule dans le dashboard), mais c'est **délibérément verrouillé** : aucun code
d'envoi d'ordre ou de signature n'existe, et plusieurs garde-fous l'interdisent (voir
`app/safety.py`, testés dans `tests/test_paper_only.py`). Avant d'y penser : plusieurs mois
de paper trading positifs et stables, une exécution auditée (ordres stop côté exchange,
gestion des échecs), et uniquement un capital que vous acceptez de perdre.

---

## 10. Limitations connues

- Performance non prouvée : voir §4.
- Le Pine Script n'a pas été compilé dans TradingView ; aucune alerte TradingView réelle n'a
  été reçue (webhook testé avec des requêtes identiques).
- Mode PostgreSQL : schéma créé dans Supabase, mais le bot n'y a pas encore été connecté
  (mot de passe requis). Déploiement Render et image Docker non testés ici.
- Simulation : pas d'exécution partielle ni de carnet réel, pas de frais de financement.
- Les demi-séances boursières (veille de Noël…) ne sont pas modélisées.
- Recherche web : Claude peut manquer ou mal dater une information ; les heures d'annonces
  proviennent de ses recherches.
- L'API publique Binance peut être inaccessible depuis certains pays.
