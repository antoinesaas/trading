-- Multi-marchés (actions, ETF, forex) et auto-apprentissage de Claude.
-- Le bot ajoute aussi ces colonnes tout seul au démarrage s'il les trouve absentes.
alter table public.positions add column if not exists fx_rate double precision default 1.0;
alter table public.ai_decisions add column if not exists lesson jsonb;
