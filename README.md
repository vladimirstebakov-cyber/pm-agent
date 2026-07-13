# pm-agent — Prediction Market Data + Replay Engine (Phase A)

AI-агент для рынков предсказаний. **Phase A: data + replay, без trading logic.**

## Принцип

LLM не торгует. Этот слой — честный point-in-time датасет и replay engine, на котором
проверяются 4 паттерна edge без leakage. Decision gate (paper→live) требует
положительный net EV в `tape_confirmed` И `conservative` режимах, fill rate >70%.

## Быстрый старт

```bash
# 1. Поднять TimescaleDB
docker compose up -d timescaledb

# 2. Установить зависимости
pip install -e .

# 3. Скопировать .env
cp .env.example .env

# 4. Применить схему
pm-agent init-db

# 5. Собрать рынки (Polymarket + Kalshi)
pm-agent collect-markets --pages 10

# 6. Запустить сбор стаканов (continuous)
pm-agent collect-orderbooks --interval 15

# 7. Проверить статус
pm-agent show-status
```

## Структура

```
src/pm_agent/
  config.py              # настройки из .env
  cli.py                 # CLI: init-db, collect-markets, collect-orderbooks, show-status
  clients/
    rate_limit.py        # token-bucket + retry + 429 backoff
    schemas.py           # NormalisedMarket/Outcome/Orderbook/Trade
    polymarket_gamma.py  # market discovery (read-only, no auth)
    polymarket_clob.py   # orderbook/price (read-only)
    kalshi_rest.py       # markets/orderbook/trades (read-only)
  db/
    schema.sql           # TimescaleDB hypertables + replay tables (no-leakage)
    repo.py              # append-only writes + point-in-time reads
    queries.py           # watchlist + matched pairs (stub)
  collectors/
    market_discovery.py   # Polymarket Gamma + Kalshi markets
    orderbook_collector.py # tiered snapshots
  replay/
    fill_models.py       # 4 fill modes: naive/latency/tape_confirmed/conservative
    engine.py            # replay runs + decision gate
  scanners/
    interfaces.py        # Scanner ABC + Signal
    stubs/               # 4 паттерна (Phase B logic)
systemd/                # юниты для discovery/orderbook/arb
```

## No-leakage инварианты

1. Replay использует ТОЛЬКО данные с `ts_collected <= decision_time`.
2. `resolutions.resolved_at` не виден до `resolution_known_at`.
3. `market_rules` версия WHERE `observed_at <= decision_time`.
4. Snapshots append-only; никогда не UPDATE in place.
5. Хранится raw payload + hash для воспроизводимости.

## Fill modes

| Mode | Что моделирует | Decision gate? |
|---|---|---|
| naive | fantasy P&L по displayed bid/ask | нет (baseline) |
| latency_adjusted | цена после N сек задержки | нет |
| **tape_confirmed** | fill только если реальный trade print прошёл через цену | **да** |
| **conservative** | losers fully, winners haircut 50% (adverse selection) | **да** |

## Что НЕ в Phase A

- Scanner logic (4 паттерна — stubs, Phase B)
- Contract matching NLP (Phase B)
- ML probability models (Phase B)
- Live execution / order placement (Phase B, требует API keys + RSA)
- Telegram alerts

## Источники

- [Polymarket docs](https://docs.polymarket.com)
- [Kalshi docs](https://docs.kalshi.com) (llms.txt index)
- Phase A спроектирован на основе `agent_architecture_design.md`
