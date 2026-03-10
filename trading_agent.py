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

TRADES_LOG_FILE  = Path("trades_log.csv")
POSITIONS_FILE   = Path("positions.json")

# ---------------------------------------------------------------------------
# CSV лог
# ---------------------------------------------------------------------------
CSV_HEADERS = [
    "timestamp", "mint", "gmgn_url", "score", "score_details",
    "sol_amount", "entry_price", "exit_price",
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
# Хранилище позиций (in-memory + JSON)
# ---------------------------------------------------------------------------

class PositionStore:
    """
    Хранит открытые позиции в памяти и синхронизирует их с positions.json.

    Структура позиции:
    {
        "mint": str,
        "position_id": str,        # id из API ответа
        "score": int,
        "sol_amount": float,
        "entry_price": float,
        "opened_at": float,        # unix timestamp
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


def print_buy(mint: str, score: int, sol_amount: float, details: dict) -> None:
    gmgn = GMGN_URL.format(mint=mint)
    print(f"\n  ✅ ПОКУПКА | {mint}")
    print(f"     Score: {score}/5 | Размер: {sol_amount} SOL")
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
        print(
            f"     • {p['mint'][:12]}… | "
            f"Score {p['score']} | "
            f"{p['sol_amount']} SOL | "
            f"Возраст: {age}s"
        )


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
    api_ids = {p.get("positionId", p.get("id", "")) for p in api_positions}
    api_mints = {p.get("mint", "") for p in api_positions}

    closed_mints = []
    for pos in store.all():
        mint = pos["mint"]
        pos_id = pos.get("position_id", "")
        # Считаем закрытой, если не нашли ни по positionId, ни по mint
        is_open = (pos_id and pos_id in api_ids) or (mint in api_mints)
        if not is_open:
            closed_mints.append(mint)

    for mint in closed_mints:
        pos = store.remove(mint)
        if pos:
            hold = int(time.time() - pos.get("opened_at", time.time()))
            # Ищем финальные данные в api_positions (если API их вернул)
            api_pos = next(
                (p for p in api_positions if p.get("mint") == mint),
                {}
            )
            exit_price = api_pos.get("exitPrice", api_pos.get("exit_price", ""))
            pnl_pct    = api_pos.get("pnlPct",   api_pos.get("pnl_pct", ""))
            exit_reason = api_pos.get("exitReason", api_pos.get("exit_reason", "closed_by_server"))

            log_trade({
                "timestamp":    now_str(),
                "mint":         mint,
                "gmgn_url":     GMGN_URL.format(mint=mint),
                "score":        pos["score"],
                "score_details": json.dumps(pos.get("score_details", {}), ensure_ascii=False),
                "sol_amount":   pos["sol_amount"],
                "entry_price":  pos.get("entry_price", ""),
                "exit_price":   exit_price,
                "pnl_pct":      pnl_pct,
                "exit_reason":  exit_reason,
                "hold_seconds": hold,
            })
            print(
                f"  🏁 ЗАКРЫТА | {mint[:12]}… | "
                f"PnL={pnl_pct}% | причина={exit_reason} | "
                f"держали {hold}s"
            )


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
    print("=" * 60)

    cycle = 0
    seen_mints: set[str] = store.all_mints().copy()  # токены которые мы уже видели/купили

    while True:
        cycle += 1
        print_header(cycle)

        # 1. Синхронизируем портфель (закрытые позиции → лог)
        print("\n[SYNC] Синхронизация портфеля...")
        sync_portfolio(store)
        print_position_status(store.all())

        # 2. Получаем список новых токенов
        print("\n[SCAN] Получаем список токенов...")
        tokens = api.get_overview()
        if not tokens:
            print("[WARN] Список токенов пуст или ошибка API — пропускаем цикл")
        else:
            print(f"[SCAN] Получено {len(tokens)} токенов")

        # 3. Фильтруем и оцениваем токены
        new_tokens_found = 0
        for token in tokens:
            mint = token.get("mint", token.get("address", ""))
            if not mint:
                continue

            # Пропускаем уже виденные (куплены или оценены в этом/предыдущем цикле)
            if mint in seen_mints:
                continue

            seen_mints.add(mint)
            new_tokens_found += 1

            # Проверка лимита позиций
            if store.count() >= MAX_OPEN_POSITIONS:
                print_skip(mint, 0, f"достигнут лимит позиций ({MAX_OPEN_POSITIONS})")
                continue

            # Получаем детальные данные токена
            datapoint = api.get_token_datapoint(mint)
            if datapoint is None:
                # Fallback: используем данные из overview
                datapoint = token
                print(f"  [WARN] Не удалось получить datapoint для {mint[:12]}… — используем overview данные")

            # Считаем score
            score, details = calculate_score(datapoint)

            if score < MIN_SCORE:
                print_skip(mint, score, f"score {score} < {MIN_SCORE}")
                continue

            # Score достаточный — открываем позицию
            sol_amount = get_sol_amount(score)
            print_buy(mint, score, sol_amount, details)

            result = api.open_paper_trade(
                mint=mint,
                sol_amount=sol_amount,
                take_profit_pct=TAKE_PROFIT_PCT,
                trailing_stop_pct=TRAILING_STOP_PCT,
                stop_loss_pct=STOP_LOSS_PCT,
                timeout_minutes=TIMEOUT_MINUTES,
            )

            if result is None:
                print(f"  [ERROR] Не удалось открыть позицию для {mint} — пропускаем")
                log_trade({
                    "timestamp":    now_str(),
                    "mint":         mint,
                    "gmgn_url":     GMGN_URL.format(mint=mint),
                    "score":        score,
                    "score_details": json.dumps(details, ensure_ascii=False),
                    "sol_amount":   sol_amount,
                    "entry_price":  "",
                    "exit_price":   "",
                    "pnl_pct":      "",
                    "exit_reason":  "api_error_on_open",
                    "hold_seconds": 0,
                })
                continue

            # Сохраняем позицию
            position = {
                "mint":         mint,
                "position_id":  result.get("positionId", result.get("id", "")),
                "score":        score,
                "score_details": details,
                "sol_amount":   sol_amount,
                "entry_price":  result.get("entryPrice", result.get("entry_price", "")),
                "opened_at":    time.time(),
            }
            store.add(position)
            print(
                f"  📌 Позиция открыта | id={position['position_id']} | "
                f"entry={position['entry_price']}"
            )

        if new_tokens_found == 0 and tokens:
            print("  ℹ  Новых токенов в этом цикле нет")

        # 4. Пауза до следующего цикла
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
