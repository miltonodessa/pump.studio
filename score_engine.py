"""
score_engine.py — функция calculate_score(token_data) → (int, dict)

Поля взяты из реального ответа GET /api/v1/datapoint:
  _age          — возраст токена в секундах
  marketCap     — market cap в USD
  solPriceUsd   — цена SOL в USD (для перевода MC в SOL)
  market_cap    — MC в SOL (из overview, если datapoint недоступен)
  recentTrades  — список последних сделок [{sol, timestamp(ms), type, wallet}, ...]
  creator       — адрес создателя (для проверки dev sells)
"""

import time


# ---------------------------------------------------------------------------
# Вспомогательные вычисления по recentTrades
# ---------------------------------------------------------------------------

def _volume_windows(trades: list, now_ms: float) -> tuple[float, float]:
    """SOL-объём за последние 60s и предыдущие 60s."""
    cut1 = now_ms - 60_000
    cut2 = now_ms - 120_000
    vol_last = sum(t.get("sol", 0) for t in trades if t.get("timestamp", 0) >= cut1)
    vol_prev = sum(t.get("sol", 0) for t in trades if cut2 <= t.get("timestamp", 0) < cut1)
    return vol_last, vol_prev


def _unique_buyers_90s(trades: list, now_ms: float) -> int:
    """Уникальные покупатели за последние 90 секунд."""
    cut = now_ms - 90_000
    return len({
        t["wallet"] for t in trades
        if t.get("type") == "buy"
        and t.get("timestamp", 0) >= cut
        and "wallet" in t
    })


def _dev_sells_30m(trades: list, creator: str, now_ms: float) -> int:
    """Количество продаж от кошелька создателя за последние 30 минут."""
    if not creator:
        return 0
    cut = now_ms - 30 * 60_000
    return sum(
        1 for t in trades
        if t.get("type") == "sell"
        and t.get("wallet") == creator
        and t.get("timestamp", 0) >= cut
    )


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

def calculate_score(token_data: dict) -> tuple[int, dict]:
    """
    Считает score токена по 5 критериям (0–5).
    token_data — распакованный dict из GET /api/v1/datapoint (data-ключ уже снят).
    """
    details: dict = {}
    score: int = 0
    now_ms = time.time() * 1000
    trades: list = token_data.get("recentTrades", [])
    creator: str = token_data.get("creator", "")

    # ------------------------------------------------------------------
    # Критерий 1: Возраст 90–300 секунд
    # Datapoint: поле _age (секунды). Fallback: createdAt (ms timestamp).
    # ------------------------------------------------------------------
    age_s = token_data.get("_age")
    if age_s is None:
        created_ms = token_data.get("createdAt", token_data.get("created_timestamp", 0))
        age_s = (now_ms - created_ms) / 1000 if created_ms else -1

    try:
        age_s = float(age_s)
    except (TypeError, ValueError):
        age_s = -1.0

    c1 = 90 <= age_s <= 300
    details["age"] = {
        "value": f"{int(age_s)}s" if age_s >= 0 else "unknown",
        "pass": c1,
        "condition": "90s ≤ age ≤ 300s",
    }
    if c1:
        score += 1

    # ------------------------------------------------------------------
    # Критерий 2: Market Cap 50–300 SOL
    # Datapoint: marketCap (USD) / solPriceUsd → SOL
    # Overview fallback: market_cap (уже в SOL)
    # ------------------------------------------------------------------
    mc_usd = token_data.get("marketCap")          # datapoint → USD
    mc_sol_direct = token_data.get("market_cap")  # overview  → SOL

    if mc_usd is not None:
        sol_price = token_data.get("solPriceUsd", 0)
        mc_sol = float(mc_usd) / float(sol_price) if sol_price else 0.0
    elif mc_sol_direct is not None:
        mc_sol = float(mc_sol_direct)
    else:
        mc_sol = 0.0

    c2 = 50.0 <= mc_sol <= 300.0
    details["market_cap"] = {
        "value": f"{mc_sol:.1f} SOL",
        "pass": c2,
        "condition": "50 ≤ MC ≤ 300 SOL",
    }
    if c2:
        score += 1

    # ------------------------------------------------------------------
    # Критерий 3: Рост объёма — vol_last_60s > vol_prev_60s
    # Считается из recentTrades.
    # ------------------------------------------------------------------
    vol_last, vol_prev = _volume_windows(trades, now_ms)
    c3 = vol_last > vol_prev and vol_last > 0
    details["volume_trend"] = {
        "value": f"last={vol_last:.4f} SOL  prev={vol_prev:.4f} SOL",
        "pass": c3,
        "condition": "vol_last_60s > vol_prev_60s",
    }
    if c3:
        score += 1

    # ------------------------------------------------------------------
    # Критерий 4: unique_buyers_90s >= 5
    # ------------------------------------------------------------------
    buyers = _unique_buyers_90s(trades, now_ms)
    c4 = buyers >= 5
    details["unique_buyers"] = {
        "value": str(buyers),
        "pass": c4,
        "condition": "unique_buyers_90s ≥ 5",
    }
    if c4:
        score += 1

    # ------------------------------------------------------------------
    # Критерий 5: dev не продавал за 30 минут
    # Проверяем recentTrades по кошельку creator.
    # ------------------------------------------------------------------
    dev_sells = _dev_sells_30m(trades, creator, now_ms)
    c5 = dev_sells == 0
    details["dev_sells"] = {
        "value": f"{dev_sells} (creator={creator[:8]}…)" if creator else str(dev_sells),
        "pass": c5,
        "condition": "dev_sells_30m == 0",
    }
    if c5:
        score += 1

    return score, details


def get_sol_amount(score: int) -> float:
    """score 3→0.3 SOL, 4→0.7 SOL, 5→1.5 SOL."""
    return {3: 0.3, 4: 0.7, 5: 1.5}.get(score, 0.0)


def format_score_details(score: int, details: dict, mint: str = "") -> str:
    """Форматирует score для вывода в терминал."""
    lines = [f"  Score {score}/5 для {mint or 'токена'}:"]
    for criterion, info in details.items():
        icon = "✓" if info["pass"] else "✗"
        lines.append(f"    [{icon}] {criterion}: {info['value']}  ({info['condition']})")
    return "\n".join(lines)
