"""
api_client.py — все вызовы к Pump.Studio API
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("PUMP_STUDIO_API_KEY", "ps_rbn2extmxsb58hngrd43tbcndq2b06xpaiqf7jmirxn1ojtn")
BASE_URL = os.getenv("PUMP_STUDIO_BASE_URL", "https://api.pump.studio")

SESSION = requests.Session()
SESSION.headers.update({
    "x-api-key": API_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
})

REQUEST_TIMEOUT = 15  # секунд


def _get(endpoint: str, params: dict = None, retries: int = 2) -> dict | None:
    """Выполняет GET запрос с retry логикой."""
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(retries + 1):
        try:
            resp = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 404:
                return None  # тихо, 404 ожидаем на некоторых эндпоинтах
            print(f"[API][ERROR] GET {endpoint} → HTTP {resp.status_code}: {e}")
            return None
        except requests.exceptions.Timeout:
            print(f"[API][WARN] GET {endpoint} → timeout (attempt {attempt+1}/{retries+1})")
            if attempt < retries:
                time.sleep(2 ** attempt)
        except requests.exceptions.RequestException as e:
            print(f"[API][ERROR] GET {endpoint} → {e}")
            return None
    return None


def _post(endpoint: str, payload: dict, retries: int = 2) -> dict | None:
    """Выполняет POST запрос с retry логикой."""
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(retries + 1):
        try:
            resp = SESSION.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            body = ""
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:200]
            print(f"[API][ERROR] POST {endpoint} → HTTP {resp.status_code}: {e} | body={body}")
            return None
        except requests.exceptions.Timeout:
            print(f"[API][WARN] POST {endpoint} → timeout (attempt {attempt+1}/{retries+1})")
            if attempt < retries:
                time.sleep(2 ** attempt)
        except requests.exceptions.RequestException as e:
            print(f"[API][ERROR] POST {endpoint} → {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# Публичные функции
# ---------------------------------------------------------------------------

def _is_valid_mint(s: str) -> bool:
    """
    Solana mint-адрес — base58, 32–44 символа.
    Отфильтровываем служебные ключи типа 'newCoins', 'trending' и т.д.
    """
    BASE58_CHARS = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return 32 <= len(s) <= 44 and all(c in BASE58_CHARS for c in s)


def _unwrap(data) -> any:
    """Разворачивает {"ok": true, "data": ...} → data."""
    if isinstance(data, dict) and "ok" in data and "data" in data:
        return data["data"]
    return data


def get_overview() -> list[dict]:
    """
    GET /api/v1/overview → {"ok": true, "data": {"newCoins": [...], "trending": [...], ...}}
    Итерирует секции в порядке приоритета и возвращает дедуплицированный список токенов.
    """
    raw = _get("/api/v1/overview")
    if raw is None:
        return []
    data = _unwrap(raw)

    raw_items: list = []
    if isinstance(data, dict):
        SECTION_ORDER = ("newCoins", "liveSpotlight", "trending", "gainers1h", "volLeaders", "losers1h")
        other = sorted(set(data.keys()) - set(SECTION_ORDER))
        for key in list(SECTION_ORDER) + other:
            section = data.get(key)
            if isinstance(section, list):
                raw_items.extend(section)
            elif isinstance(section, dict):
                raw_items.append(section)
    elif isinstance(data, list):
        raw_items = data
    else:
        return []

    result: list[dict] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, str):
            if _is_valid_mint(item) and item not in seen:
                seen.add(item)
                result.append({"mint": item})
        elif isinstance(item, dict):
            mint = item.get("mint", item.get("address", ""))
            if mint and mint not in seen:
                seen.add(mint)
                result.append(item)
    return result


def get_token_datapoint(mint: str) -> dict | None:
    """
    GET /api/v1/datapoint?mint=MINT → {"ok": true, "data": {...71 fields...}}
    Возвращает распакованные данные токена.
    """
    raw = _get("/api/v1/datapoint", params={"mint": mint})
    if raw is None:
        return None
    return _unwrap(raw)


def open_paper_trade(
    mint: str,
    sol_amount: float,
    take_profit_pct: float = 25.0,
    trailing_stop_pct: float = 8.0,
    stop_loss_pct: float = 12.0,
    timeout_minutes: int = 20,
) -> dict | None:
    """
    POST /api/v1/paper/trade
    Открывает paper trade позицию.
    """
    payload = {
        "ownerId": API_KEY,
        "mint": mint,
        "solAmount": sol_amount,
        "strategy": "custom",
        "takeProfitPct": take_profit_pct,
        "trailingStopPct": trailing_stop_pct,
        "stopLossPct": stop_loss_pct,
        "timeoutMinutes": timeout_minutes,
    }
    return _post("/api/v1/paper/trade", payload)


def close_paper_trade(position_id: str) -> dict | None:
    """
    POST /api/v1/paper/close
    Закрывает paper позицию вручную.
    """
    payload = {
        "ownerId": API_KEY,
        "positionId": position_id,
    }
    return _post("/api/v1/paper/close", payload)


def get_portfolio() -> list[dict] | None:
    """
    GET /api/v1/paper/portfolio?ownerId=API_KEY
    Возвращает текущие открытые позиции, или None при ошибке API.
    Отличаем None (ошибка) от [] (реально пустой портфель),
    чтобы не закрыть все локальные позиции при недоступном эндпоинте.
    """
    data = _get("/api/v1/paper/portfolio", params={"ownerId": API_KEY})
    if data is None:
        return None  # ошибка API (404, сеть и т.д.)
    if isinstance(data, list):
        return data
    return data.get("positions", data.get("data", []))
