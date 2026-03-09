"""
score_engine.py — функция calculate_score(token_data) → (int, dict)

Score система основана на анализе реального кошелька nya666 (34,419 сделок).
Максимальный score = 5, минимальный для входа = 3.
"""

import time


# ---------------------------------------------------------------------------
# Вспомогательные функции для безопасного извлечения полей
# ---------------------------------------------------------------------------

def _safe_float(data: dict, *keys, default: float = 0.0) -> float:
    """Ищет первый найденный ключ из keys в dict, возвращает float."""
    for key in keys:
        val = data.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return default


def _safe_int(data: dict, *keys, default: int = 0) -> int:
    """Ищет первый найденный ключ из keys в dict, возвращает int."""
    return int(_safe_float(data, *keys, default=float(default)))


def _safe_bool(data: dict, *keys, default: bool = False) -> bool:
    """Ищет первый найденный ключ, возвращает bool."""
    for key in keys:
        val = data.get(key)
        if val is not None:
            return bool(val)
    return default


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

def calculate_score(token_data: dict) -> tuple[int, dict]:
    """
    Считает score токена по 5 критериям.

    Args:
        token_data: dict с данными из GET /api/v1/datapoint (71 поле)
                    или из GET /api/v1/overview (краткие данные).

    Returns:
        (score: int, details: dict) — итоговый score и разбивка по критериям.
    """
    details = {}
    score = 0
    now = time.time()

    # ------------------------------------------------------------------
    # Критерий 1: Возраст токена 90–300 секунд
    # ------------------------------------------------------------------
    # Поля: created_at (unix), createdAt, creation_time, age_seconds
    created_ts = _safe_float(
        token_data,
        "created_at", "createdAt", "creation_time", "creationTime",
        default=0.0
    )
    if created_ts > 1_000_000_000:  # unix timestamp (не 0)
        age_seconds = now - created_ts
    else:
        # Некоторые API возвращают age напрямую
        age_seconds = _safe_float(
            token_data,
            "age_seconds", "ageSeconds", "age",
            default=-1.0
        )

    if age_seconds < 0:
        c1 = False
        age_label = "unknown"
    else:
        c1 = 90 <= age_seconds <= 300
        age_label = f"{int(age_seconds)}s"

    details["age"] = {
        "value": age_label,
        "pass": c1,
        "condition": "90s ≤ age ≤ 300s",
    }
    if c1:
        score += 1

    # ------------------------------------------------------------------
    # Критерий 2: Market Cap между 50 и 300 SOL
    # ------------------------------------------------------------------
    mc_sol = _safe_float(
        token_data,
        "market_cap_sol", "marketCapSol", "mc_sol", "marketCap",
        default=0.0
    )
    # Если MC в USD, попробуем перевести (грубо, SOL ≈ 140 USD)
    if mc_sol == 0.0:
        mc_usd = _safe_float(token_data, "market_cap", "marketCapUsd", default=0.0)
        if mc_usd > 0:
            sol_price = _safe_float(token_data, "sol_price", "solPrice", default=140.0)
            if sol_price <= 0:
                sol_price = 140.0
            mc_sol = mc_usd / sol_price

    c2 = 50.0 <= mc_sol <= 300.0
    details["market_cap"] = {
        "value": f"{mc_sol:.1f} SOL",
        "pass": c2,
        "condition": "50 ≤ MC ≤ 300 SOL",
    }
    if c2:
        score += 1

    # ------------------------------------------------------------------
    # Критерий 3: volume_last_60s > volume_prev_60s (рост объёма)
    # ------------------------------------------------------------------
    vol_last = _safe_float(
        token_data,
        "volume_last_60s", "volumeLast60s", "vol_60s", "volume60s",
        "volume_1m", "volume1m",
        default=0.0
    )
    vol_prev = _safe_float(
        token_data,
        "volume_prev_60s", "volumePrev60s", "vol_prev_60s",
        "volume_prev_1m", "volumePrev1m",
        default=0.0
    )
    c3 = vol_last > vol_prev and vol_last > 0
    details["volume_trend"] = {
        "value": f"last={vol_last:.2f} prev={vol_prev:.2f}",
        "pass": c3,
        "condition": "volume_last_60s > volume_prev_60s",
    }
    if c3:
        score += 1

    # ------------------------------------------------------------------
    # Критерий 4: unique_buyers_90s >= 5
    # ------------------------------------------------------------------
    buyers_90s = _safe_int(
        token_data,
        "unique_buyers_90s", "uniqueBuyers90s", "buyers_90s",
        "unique_buyers", "uniqueBuyers",
        default=0
    )
    c4 = buyers_90s >= 5
    details["unique_buyers"] = {
        "value": str(buyers_90s),
        "pass": c4,
        "condition": "unique_buyers_90s ≥ 5",
    }
    if c4:
        score += 1

    # ------------------------------------------------------------------
    # Критерий 5: dev_sells_30m == 0 (дев не продавал за 30 мин)
    # ------------------------------------------------------------------
    dev_sells = _safe_int(
        token_data,
        "dev_sells_30m", "devSells30m", "dev_sells",
        "devSells", "developer_sells",
        default=0
    )
    c5 = dev_sells == 0
    details["dev_sells"] = {
        "value": str(dev_sells),
        "pass": c5,
        "condition": "dev_sells_30m == 0",
    }
    if c5:
        score += 1

    return score, details


def get_sol_amount(score: int) -> float:
    """
    Возвращает размер позиции в SOL по confidence-based стратегии.

    score 3 → 0.3 SOL
    score 4 → 0.7 SOL
    score 5 → 1.5 SOL
    """
    mapping = {3: 0.3, 4: 0.7, 5: 1.5}
    return mapping.get(score, 0.0)


def format_score_details(score: int, details: dict, mint: str = "") -> str:
    """Форматирует score для вывода в терминал."""
    lines = [f"  Score {score}/5 для {mint or 'токена'}:"]
    icons = {True: "✓", False: "✗"}
    for criterion, info in details.items():
        icon = icons[info["pass"]]
        lines.append(
            f"    [{icon}] {criterion}: {info['value']}  ({info['condition']})"
        )
    return "\n".join(lines)
