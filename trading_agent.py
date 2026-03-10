"""
trading_agent.py — главный цикл paper-trading агента для Pump.Studio

Запуск:  python trading_agent.py
Лог:     trades_log.csv
Позиции: positions.json
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import api_client as api
from score_engine import calculate_score, format_score_details, get_sol_amount

load_dotenv()

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
POLL_INTERVAL      = 3         # секунд между циклами
MAX_OPEN_POSITIONS = 5         # максимум одновременно открытых позиций
MIN_SCORE          = 3         # минимальный score для открытия позиции
TAKE_PROFIT_PCT    = 25.0
TRAILING_STOP_PCT  = 8.0
STOP_LOSS_PCT      = 12.0
TIMEOUT_MINUTES    = 20
TRADE_FEE_PCT      = 1.0      # % комиссии на каждую сделку (buy и sell)

TRADES_LOG_FILE  = Path("trades_log.csv")
POSITIONS_FILE   = Path("positions.json")

# ---------------------------------------------------------------------------
# CSV лог
# ---------------------------------------------------------------------------
CSV_HEADERS = [
    "timestamp", "mint", "gmgn_url", "score", "score_details",
    "sol_amount",
    "entry_price_usd", "exit_price_usd",
    "buy_usd", "sell_usd", "fee_usd", "pnl_usd",
    "pnl_pct", "exit_reason", "hold_seconds",
]


def ensure_csv() -> None:
    """Создаёт trades_log.csv с заголовками, если файл не существует."""
    if not TRADES_LOG_FILE.exists():
        with open(TRADES_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
        print(f"[INIT] Создан файл лога: {TRADES_LOG_FILE}")


def log_trade(record: dict) -> None:
    """Дозаписывает строку в trades_log.csv."""
    with open(TRADES_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        row = {k: record.get(k, "") for k in CSV_HEADERS}
        writer.writerow(row)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _to_float(val, default: float = 0.0) -> float:
    """Безопасно конвертирует любое значение в float."""
    if val is None or isinstance(val, (dict, list, bool)):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _get_sol_price(datapoint: dict) -> float:
    """Цена SOL в USD из datapoint, fallback 85."""
    return _to_float(datapoint.get("solPriceUsd"), 85.0)


def _get_token_price_usd(datapoint: dict) -> float:
    """
    Цена одного токена в USD.
    Приоритет: priceUsd → price → marketCap / totalSupply (1B для pump.fun).
    """
    price = _to_float(datapoint.get("priceUsd") or datapoint.get("price"))
    if price > 0:
        return price
    mc_usd = _to_float(datapoint.get("marketCap"))
    supply = _to_float(datapoint.get("totalSupply"), 1_000_000_000.0)
    if mc_usd > 0 and supply > 0:
        return mc_usd / supply
    return 0.0


def _calc_usd(sol_amount: float, sol_price: float, pnl_pct: float = 0.0) -> dict:
    """Рассчитывает USD-значения сделки с учётом комиссий."""
    buy_usd  = sol_amount * sol_price
    sell_usd = buy_usd * (1.0 + pnl_pct / 100.0)
    buy_fee  = buy_usd  * (TRADE_FEE_PCT / 100.0)
    sell_fee = sell_usd * (TRADE_FEE_PCT / 100.0)
    total_fee = buy_fee + sell_fee
    pnl_usd  = (sell_usd - sell_fee) - (buy_usd + buy_fee)
    return {
        "buy_usd":  round(buy_usd,   2),
        "sell_usd": round(sell_usd,  2),
        "fee_usd":  round(total_fee, 2),
        "pnl_usd":  round(pnl_usd,  2),
    }


# ---------------------------------------------------------------------------
# Хранилище позиций (in-memory + JSON)
# ---------------------------------------------------------------------------

class PositionStore:
    """
    Хранит открытые позиции в памяти и синхронизирует их с positions.json.

    Структура позиции:
    {
        "mint":             str,
        "position_id":      str,          # id из API ответа (пусто для локальных)
        "local":            bool,         # True = API недоступен, трекаем сами
        "score":            int,
        "sol_amount":       float,
        "sol_price_usd":    float,        # цена SOL в USD на момент покупки
        "entry_price_usd":  float,        # цена токена в USD на момент покупки
        "trailing_high_usd": float,       # максимальная цена для trailing stop
        "opened_at":        float,        # unix timestamp
    }
    """

    def __init__(self):
        self._positions: dict[str, dict] = {}  # mint → position
        self._load()

    def _load(self) -> None:
        if POSITIONS_FILE.exists():
            try:
                raw = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
                self._positions = {p["mint"]: p for p in raw}
                print(f"[INIT] Загружено {len(self._positions)} позиций из {POSITIONS_FILE}")
            except (json.JSONDecodeError, KeyError) as e:
                print(f"[WARN] Не удалось загрузить {POSITIONS_FILE}: {e} — начинаем с нуля")
                self._positions = {}

    def _save(self) -> None:
        data = list(self._positions.values())
        POSITIONS_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def add(self, position: dict) -> None:
        self._positions[position["mint"]] = position
        self._save()

    def remove(self, mint: str) -> dict | None:
        pos = self._positions.pop(mint, None)
        if pos is not None:
            self._save()
        return pos

    def has(self, mint: str) -> bool:
        return mint in self._positions

    def count(self) -> int:
        return len(self._positions)

    def all(self) -> list[dict]:
        return list(self._positions.values())

    def all_mints(self) -> set[str]:
        return set(self._positions.keys())


# ---------------------------------------------------------------------------
# Форматирование для терминала
# ---------------------------------------------------------------------------

def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def print_header(cycle: int) -> None:
    print(f"\n{'='*60}")
    print(f"  ЦИКЛ #{cycle} | {now_str()}")
    print(f"{'='*60}")


GMGN_URL = "https://gmgn.ai/sol/token/{mint}"


def print_buy(mint: str, score: int, sol_amount: float,
              sol_price: float, entry_price_usd: float, details: dict) -> None:
    gmgn = GMGN_URL.format(mint=mint)
    buy_usd = sol_amount * sol_price
    buy_fee = buy_usd * (TRADE_FEE_PCT / 100.0)
    print(f"\n  ✅ ПОКУПКА | {mint}")
    print(f"     Score: {score}/5 | Размер: {sol_amount} SOL")
    print(f"     Цена SOL: ${sol_price:.2f} | Цена токена: ${entry_price_usd:.8f}")
    print(f"     Стоимость: ${buy_usd:.2f} + ${buy_fee:.2f} комиссия = ${buy_usd + buy_fee:.2f} итого")
    print(f"     gmgn: {gmgn}")
    print(format_score_details(score, details, mint))


def print_skip(mint: str, score: int, reason: str, details: dict | None = None) -> None:
    print(f"  ⏭  ПРОПУСК | {mint[:12]}… | Score {score}/5 | {reason}")
    if details:
        print(format_score_details(score, details, mint))


def print_position_status(positions: list[dict]) -> None:
    if not positions:
        print("  📭 Открытых позиций нет")
        return
    print(f"  📊 Открытых позиций: {len(positions)}")
    for p in positions:
        age = int(time.time() - p.get("opened_at", time.time()))
        mode = "LOCAL" if p.get("local") else "API"
        print(
            f"     • {p['mint'][:12]}… | "
            f"Score {p['score']} | "
            f"{p['sol_amount']} SOL | "
            f"entry=${p.get('entry_price_usd', 0):.8f} | "
            f"Возраст: {age}s [{mode}]"
        )


def _print_close(pos: dict, exit_price_usd: float, pnl_pct: float,
                 reason: str, hold_s: int) -> None:
    sol_amount = pos["sol_amount"]
    sol_price  = pos.get("sol_price_usd", 85.0)
    usd = _calc_usd(sol_amount, sol_price, pnl_pct)
    emoji = "🟢" if pnl_pct >= 0 else "🔴"
    print(
        f"  {emoji} ЗАКРЫТА [{reason}] | {pos['mint'][:12]}… | "
        f"PnL={pnl_pct:+.1f}% (${usd['pnl_usd']:+.2f}) | "
        f"Купил ${usd['buy_usd']:.2f} → Продал ${usd['sell_usd']:.2f} | "
        f"Комиссия ${usd['fee_usd']:.2f} | держали {hold_s}s"
    )


def _log_close(pos: dict, exit_price_usd: float, pnl_pct: float,
               reason: str, hold_s: int) -> None:
    sol_amount = pos["sol_amount"]
    sol_price  = pos.get("sol_price_usd", 85.0)
    usd = _calc_usd(sol_amount, sol_price, pnl_pct)
    log_trade({
        "timestamp":       now_str(),
        "mint":            pos["mint"],
        "gmgn_url":        GMGN_URL.format(mint=pos["mint"]),
        "score":           pos["score"],
        "score_details":   json.dumps(pos.get("score_details", {}), ensure_ascii=False),
        "sol_amount":      sol_amount,
        "entry_price_usd": round(pos.get("entry_price_usd", 0), 8),
        "exit_price_usd":  round(exit_price_usd, 8),
        "buy_usd":         usd["buy_usd"],
        "sell_usd":        usd["sell_usd"],
        "fee_usd":         usd["fee_usd"],
        "pnl_usd":         usd["pnl_usd"],
        "pnl_pct":         round(pnl_pct, 2),
        "exit_reason":     reason,
        "hold_seconds":    hold_s,
    })


# ---------------------------------------------------------------------------
# Синхронизация позиций с API (закрытые сервером — записываем в лог)
# ---------------------------------------------------------------------------

def sync_portfolio(store: PositionStore) -> None:
    """
    Запрашивает текущий портфель через API.
    Если позиция пропала из API — значит она закрылась (TP/SL/timeout).
    Записываем в лог и удаляем из хранилища.
    Если API вернул ошибку (None) — пропускаем синхронизацию, не трогаем позиции.
    """
    api_positions = api.get_portfolio()
    if api_positions is None:
        print("  [WARN] Portfolio API недоступен — синхронизация пропущена")
        return
    api_ids   = {p.get("positionId", p.get("id", "")) for p in api_positions}
    api_mints = {p.get("mint", "") for p in api_positions}

    closed_mints = []
    for pos in store.all():
        if pos.get("local"):
            continue  # локальные позиции не синхронизируем через API
        mint   = pos["mint"]
        pos_id = pos.get("position_id", "")
        is_open = (pos_id and pos_id in api_ids) or (mint in api_mints)
        if not is_open:
            closed_mints.append(mint)

    for mint in closed_mints:
        pos = store.remove(mint)
        if pos:
            hold_s = int(time.time() - pos.get("opened_at", time.time()))
            api_pos = next(
                (p for p in api_positions if p.get("mint") == mint), {}
            )
            exit_price_raw = api_pos.get("exitPrice", api_pos.get("exit_price", 0))
            pnl_pct        = _to_float(api_pos.get("pnlPct", api_pos.get("pnl_pct", 0)))
            reason         = api_pos.get("exitReason", api_pos.get("exit_reason", "closed_by_server"))
            exit_price_usd = _to_float(exit_price_raw)

            _log_close(pos, exit_price_usd, pnl_pct, reason, hold_s)
            _print_close(pos, exit_price_usd, pnl_pct, reason, hold_s)


# ---------------------------------------------------------------------------
# Мониторинг локальных позиций (TP / SL / trailing / timeout)
# ---------------------------------------------------------------------------

def monitor_local_positions(store: PositionStore) -> None:
    """
    Для позиций с local=True запрашивает текущую цену токена и проверяет
    условия закрытия: TP, SL, trailing stop, timeout.
    """
    local_positions = [p for p in store.all() if p.get("local")]
    if not local_positions:
        return

    now = time.time()
    to_close: list[tuple] = []

    for pos in local_positions:
        mint = pos["mint"]
        datapoint = api.get_token_datapoint(mint)
        if datapoint is None:
            continue

        current_price_usd = _get_token_price_usd(datapoint)
        entry_price_usd   = pos.get("entry_price_usd", 0.0)

        if entry_price_usd <= 0 or current_price_usd <= 0:
            continue

        # Обновляем trailing high
        trailing_high = pos.get("trailing_high_usd", entry_price_usd)
        if current_price_usd > trailing_high:
            pos["trailing_high_usd"] = current_price_usd
            store.add(pos)  # сохраняем обновлённый trailing high
            trailing_high = current_price_usd

        pnl_pct       = (current_price_usd - entry_price_usd) / entry_price_usd * 100.0
        hold_s        = now - pos.get("opened_at", now)
        trail_drop    = (trailing_high - current_price_usd) / trailing_high * 100.0 \
                        if trailing_high > 0 else 0.0

        reason = None
        if pnl_pct >= TAKE_PROFIT_PCT:
            reason = "take_profit"
        elif pnl_pct <= -STOP_LOSS_PCT:
            reason = "stop_loss"
        elif trail_drop >= TRAILING_STOP_PCT and pnl_pct > 0:
            reason = "trailing_stop"
        elif hold_s >= TIMEOUT_MINUTES * 60:
            reason = "timeout"

        if reason:
            to_close.append((pos, current_price_usd, pnl_pct, reason))

    for pos, exit_price_usd, pnl_pct, reason in to_close:
        hold_s = int(now - pos.get("opened_at", now))
        store.remove(pos["mint"])
        _log_close(pos, exit_price_usd, pnl_pct, reason, hold_s)
        _print_close(pos, exit_price_usd, pnl_pct, reason, hold_s)


# ---------------------------------------------------------------------------
# Главный цикл
# ---------------------------------------------------------------------------

def main() -> None:
    ensure_csv()
    store = PositionStore()

    print("=" * 60)
    print("  🚀 PUMP.STUDIO PAPER TRADING AGENT")
    print(f"  Интервал: {POLL_INTERVAL}s | Макс позиций: {MAX_OPEN_POSITIONS}")
    print(f"  Мин score: {MIN_SCORE} | TP: {TAKE_PROFIT_PCT}% | SL: {STOP_LOSS_PCT}%")
    print(f"  Трейлинг: {TRAILING_STOP_PCT}% | Timeout: {TIMEOUT_MINUTES}m")
    print(f"  Комиссия: {TRADE_FEE_PCT}% на сделку (buy + sell)")
    print("=" * 60)

    cycle = 0
    seen_mints: set[str] = store.all_mints().copy()

    while True:
        cycle += 1
        print_header(cycle)

        # 1. Синхронизируем API-позиции (закрытые сервером → лог)
        print("\n[SYNC] Синхронизация портфеля...")
        sync_portfolio(store)

        # 2. Мониторим локальные позиции (TP/SL/trailing/timeout)
        monitor_local_positions(store)

        print_position_status(store.all())

        # 3. Получаем список новых токенов
        print("\n[SCAN] Получаем список токенов...")
        tokens = api.get_overview()
        if not tokens:
            print("[WARN] Список токенов пуст или ошибка API — пропускаем цикл")
        else:
            print(f"[SCAN] Получено {len(tokens)} токенов")

        # 4. Фильтруем и оцениваем токены
        new_tokens_found = 0
        for token in tokens:
            mint = token.get("mint", token.get("address", ""))
            if not mint:
                continue

            if mint in seen_mints:
                continue

            seen_mints.add(mint)
            new_tokens_found += 1

            if store.count() >= MAX_OPEN_POSITIONS:
                print_skip(mint, 0, f"достигнут лимит позиций ({MAX_OPEN_POSITIONS})")
                continue

            # Получаем детальные данные токена
            datapoint = api.get_token_datapoint(mint)
            if datapoint is None:
                datapoint = token
                print(f"  [WARN] Нет datapoint для {mint[:12]}… — используем overview данные")

            # Считаем score
            score, details = calculate_score(datapoint)

            if score < MIN_SCORE:
                print_skip(mint, score, f"score {score} < {MIN_SCORE}")
                continue

            # Цены для логирования
            sol_price       = _get_sol_price(datapoint)
            entry_price_usd = _get_token_price_usd(datapoint)
            sol_amount      = get_sol_amount(score)

            print_buy(mint, score, sol_amount, sol_price, entry_price_usd, details)

            result = api.open_paper_trade(
                mint=mint,
                sol_amount=sol_amount,
                take_profit_pct=TAKE_PROFIT_PCT,
                trailing_stop_pct=TRAILING_STOP_PCT,
                stop_loss_pct=STOP_LOSS_PCT,
                timeout_minutes=TIMEOUT_MINUTES,
            )

            if result is None:
                # API недоступен — открываем позицию локально
                if entry_price_usd > 0:
                    position = {
                        "mint":              mint,
                        "position_id":       "",
                        "local":             True,
                        "score":             score,
                        "score_details":     details,
                        "sol_amount":        sol_amount,
                        "sol_price_usd":     sol_price,
                        "entry_price_usd":   entry_price_usd,
                        "trailing_high_usd": entry_price_usd,
                        "opened_at":         time.time(),
                    }
                    store.add(position)
                    usd = _calc_usd(sol_amount, sol_price)
                    print(
                        f"  📌 [LOCAL] Позиция открыта локально | "
                        f"entry=${entry_price_usd:.8f} | "
                        f"Куплено за ${usd['buy_usd']:.2f} + ${usd['fee_usd']:.2f} fee"
                    )
                else:
                    print(f"  [ERROR] Не удалось открыть позицию для {mint} — нет цены, пропускаем")
                    log_trade({
                        "timestamp":   now_str(),
                        "mint":        mint,
                        "gmgn_url":    GMGN_URL.format(mint=mint),
                        "score":       score,
                        "score_details": json.dumps(details, ensure_ascii=False),
                        "sol_amount":  sol_amount,
                        "exit_reason": "api_error_no_price",
                        "hold_seconds": 0,
                    })
                continue

            # API успешно открыл позицию
            api_entry = _to_float(result.get("entryPrice", result.get("entry_price", 0)))
            if api_entry <= 0:
                api_entry = entry_price_usd  # fallback на локальную цену

            position = {
                "mint":              mint,
                "position_id":       result.get("positionId", result.get("id", "")),
                "local":             False,
                "score":             score,
                "score_details":     details,
                "sol_amount":        sol_amount,
                "sol_price_usd":     sol_price,
                "entry_price_usd":   api_entry,
                "trailing_high_usd": api_entry,
                "opened_at":         time.time(),
            }
            store.add(position)
            usd = _calc_usd(sol_amount, sol_price)
            print(
                f"  📌 Позиция открыта | id={position['position_id']} | "
                f"entry=${api_entry:.8f} | "
                f"Куплено за ${usd['buy_usd']:.2f} + ${usd['fee_usd']:.2f} fee"
            )

        if new_tokens_found == 0 and tokens:
            print("  ℹ  Новых токенов в этом цикле нет")

        # 5. Пауза до следующего цикла
        print(f"\n[SLEEP] Следующий цикл через {POLL_INTERVAL}s... (Ctrl+C для выхода)")
        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n\n[EXIT] Агент остановлен пользователем. До свидания!")
            print(f"  Открытых позиций при выходе: {store.count()}")
            print(f"  Лог сохранён в: {TRADES_LOG_FILE}")
            sys.exit(0)


if __name__ == "__main__":
    main()
