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

def get_overview() -> list[dict]:
    """
    GET /api/v1/overview
    Возвращает список токенов (trending/new).
    API может вернуть: список строк (mint-адреса), список dict, или {"tokens": [...]}.
    """
    data = _get("/api/v1/overview")
    if data is None:
        return []
    if isinstance(data, list):
        items = data
    else:
        items = data.get("tokens", data.get("data", []))
    # Нормализуем: строки → {"mint": str}
    result = []
    for item in items:
        if isinstance(item, str):
            result.append({"mint": item})
        elif isinstance(item, dict):
            result.append(item)
    return result


def get_token_datapoint(mint: str) -> dict | None:
    """
    GET /api/v1/datapoint?mint=MINT
    Возвращает 71-field данные токена.
    """
    return _get("/api/v1/datapoint", params={"mint": mint})


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
