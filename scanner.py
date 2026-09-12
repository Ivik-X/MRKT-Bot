"""
MRKT Gift Scanner (multi-account + VLESS proxy)
================================================
Конфиг:
  tokens.txt   — по одному токену на строку (обязательно)
  proxies.txt  — по одному vless:// URL на строку (опционально)
  .env         — настройки

Запуск на сервере:
  nohup python scanner.py > /dev/null 2>&1 &
  # или через screen/tmux
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from statistics import median
from typing import Any

from curl_cffi import requests as cffi_requests
from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

from account_pool import (
    AccountPool,
    Slot,
    build_pool,
    build_pool_async,
    verify_token_async,
    buy_gift_async,
    verify_gift_in_vault_async,
)
from settings_manager import load_settings, save_settings

load_dotenv()

# ─────────────────────────────────────────────
#  Конфиг
# ─────────────────────────────────────────────

MARKET_API_URL        = "https://api.tgmrkt.io/api/v1"
SCAN_INTERVAL         = float(os.getenv("SCAN_INTERVAL", 0.8))
MIN_SCAN_INTERVAL     = float(os.getenv("MIN_SCAN_INTERVAL", 0.3))
MAX_SCAN_INTERVAL     = float(os.getenv("MAX_SCAN_INTERVAL", 3.0))
MIN_TON_DIFF          = float(os.getenv("MIN_TON_DIFF", 2.5))
CHEAP_PRICE_THRESHOLD = float(os.getenv("CHEAP_PRICE_THRESHOLD", 3.0))
MIN_TURNOVER_RATIO    = float(os.getenv("MIN_TURNOVER_RATIO", "0.0"))
LOW_ID_MAX_FLOOR_RATIO = float(os.getenv("LOW_ID_MAX_FLOOR_RATIO", "0.20"))
FILTER_BY_BALANCE     = os.getenv("FILTER_BY_BALANCE", "false").lower() in ("1", "true", "yes")
PRIMARY_TOKEN         = os.getenv("PRIMARY_TOKEN", "").strip()
FLOOR_REFRESH         = int(os.getenv("FLOOR_REFRESH", 300))
FLOOR_HISTORY_LEN     = int(os.getenv("FLOOR_HISTORY_LEN", 5))
FLOOR_ANOMALY_PCT     = float(os.getenv("FLOOR_ANOMALY_PCT", 50.0))
REQUEST_TIMEOUT       = max(2.5, float(os.getenv("REQUEST_TIMEOUT", 3.0)))
MAX_PING_SECONDS      = float(os.getenv("MAX_PING_SECONDS", 3.0))
MAX_RETRIES           = int(os.getenv("MAX_RETRIES", 3))
PENALTY_429           = float(os.getenv("PENALTY_429", 15.0))
LOG_DIR               = Path(os.getenv("LOG_DIR", "logs"))

RATE_ADAPT_WINDOW     = int(os.getenv("RATE_ADAPT_WINDOW", 30))
RATE_UP_THRESHOLD     = float(os.getenv("RATE_UP_THRESHOLD", 0.15))
RATE_DOWN_THRESHOLD   = float(os.getenv("RATE_DOWN_THRESHOLD", 0.04))

BLACK_BACKDROPS = {"Black"}

TG_BOT_TOKEN     = os.getenv("TG_BOT_TOKEN", "").strip()
TG_ADMIN_ID_RAW  = os.getenv("TG_ADMIN_ID", "").strip()
TG_ADMIN_IDS     = {int(x.strip()) for x in TG_ADMIN_ID_RAW.split(",") if x.strip().isdigit()}

from tg_bot import (
    ScannerState,
    run_telegram_bot,
    send_deal_notification,
    send_autobuy_success_report,
    send_autobuy_failed_report,
    send_autobuy_skipped_notification,
    _mask_token,
)

_global_scanner_state: Optional[ScannerState] = None



# ─────────────────────────────────────────────
#  Логирование
# ─────────────────────────────────────────────

def setup_logging() -> tuple[logging.Logger, logging.Logger]:
    LOG_DIR.mkdir(exist_ok=True)

    fmt_file    = logging.Formatter("%(asctime)s [%(levelname)-8s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fmt_console = logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")

    scanner_log = logging.getLogger("scanner")
    scanner_log.setLevel(logging.DEBUG)

    fh = TimedRotatingFileHandler(
        LOG_DIR / "scanner.log", when="midnight", interval=1, backupCount=30, encoding="utf-8",
    )
    fh.suffix = "%Y-%m-%d"
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt_file)
    scanner_log.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt_console)
    scanner_log.addHandler(ch)

    deals_log = logging.getLogger("deals")
    deals_log.setLevel(logging.INFO)
    deals_log.propagate = False

    dfh = logging.FileHandler(LOG_DIR / "deals.jsonl", mode="a", encoding="utf-8")
    dfh.setLevel(logging.INFO)
    dfh.setFormatter(logging.Formatter("%(message)s"))
    deals_log.addHandler(dfh)

    return scanner_log, deals_log


log, deals_log = setup_logging()


# ─────────────────────────────────────────────
#  Утилиты
# ─────────────────────────────────────────────

def tons(n: int | float) -> float:
    return n / 1_000_000_000

def tons_fmt(n: int | float) -> str:
    return f"{tons(n):.2f} TON"

def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")

_SLUG_RE = re.compile(r"[A-Za-z0-9]+")

def make_telegram_nft_url(collection_name: str, number: Any) -> str:
    if not collection_name or number is None:
        return "https://t.me/nft"
    cleaned = str(collection_name).replace("'", "")
    words = _SLUG_RE.findall(cleaned)
    slug = "".join(w.capitalize() for w in words)
    if not slug:
        return "https://t.me/nft"
    return f"https://t.me/nft/{slug}-{number}"

def gift_url(gift: dict) -> str:
    col = gift.get("collectionName", "")
    num = gift.get("number") if gift.get("number") is not None else gift.get("num")
    return make_telegram_nft_url(col, num)

SEPARATOR = "─" * 60

def log_deal_to_file(deal: dict) -> None:
    gift = deal["gift"]
    diff_ton = deal.get("diff_ton", tons(deal["floor"] - deal["price"]))
    row = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "type": deal["type"],
        "collection": gift.get("collectionName", "?"),
        "model": gift.get("modelName", "?"),
        "backdrop": gift.get("backdropName", "?"),
        "symbol": gift.get("symbolName", "?"),
        "number": gift.get("number"),
        "price_nanoton": deal["price"],
        "price_ton": round(tons(deal["price"]), 4),
        "floor_nanoton": deal["floor"],
        "floor_ton": round(tons(deal["floor"]), 4),
        "profit_ton": round(diff_ton, 4),
        "discount_pct": round(deal["pct"], 2),
        "floor_src": deal.get("floor_src", ""),
        "url": gift_url(gift),
        "gift_id": gift.get("id", ""),
    }
    deals_log.info(json.dumps(row, ensure_ascii=False))


def log_error_to_file(error_type: str, message: str, slot_label: str = "", endpoint: str = "") -> None:
    row = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "type": error_type,
        "endpoint": endpoint,
        "slot": slot_label,
        "message": str(message),
    }
    try:
        with open(LOG_DIR / "errors.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


class StatsTracker:
    def __init__(self):
        self.started_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        self.scans_completed = 0
        self.new_gifts_found = 0
        self.deals_found = 0
        self.errors_count = 0
        self.errors_by_type: dict[str, int] = {}
        self.collections_summary: dict[str, int] = {}
        self._last_save = 0.0

    def record_scan(self, new_gifts: list[dict], deals_count: int = 0) -> None:
        self.scans_completed += 1
        self.new_gifts_found += len(new_gifts)
        self.deals_found += deals_count
        for g in new_gifts:
            col = g.get("collectionName", "Unknown")
            self.collections_summary[col] = self.collections_summary.get(col, 0) + 1
        now = time.monotonic()
        if now - self._last_save >= 30.0:
            self.save()
            self._last_save = now

    def record_error(self, err_type: str, msg: str = "", slot_label: str = "", endpoint: str = "") -> None:
        self.errors_count += 1
        self.errors_by_type[err_type] = self.errors_by_type.get(err_type, 0) + 1
        log_error_to_file(err_type, msg, slot_label, endpoint)

    def save(self) -> None:
        try:
            data = {
                "started_at": self.started_at,
                "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "scans_completed": self.scans_completed,
                "new_gifts_found": self.new_gifts_found,
                "deals_found": self.deals_found,
                "errors_count": self.errors_count,
                "errors_by_type": self.errors_by_type,
                "top_collections": dict(sorted(self.collections_summary.items(), key=lambda x: x[1], reverse=True)[:30]),
            }
            with open(LOG_DIR / "night_stats.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


stats_tracker = StatsTracker()


class AuthTokenExpiredError(Exception):
    pass


# ─────────────────────────────────────────────
#  FloorTracker — история флора NFT-коллекций
# ─────────────────────────────────────────────

class FloorTracker:
    """
    Хранит историю последних FLOOR_HISTORY_LEN измерений флора каждой коллекции.
    Медиана — рабочий флор. Отклонение > FLOOR_ANOMALY_PCT% → перепроверка.
    """

    def __init__(self, history_len: int = FLOOR_HISTORY_LEN, anomaly_pct: float = FLOOR_ANOMALY_PCT):
        self._history: dict[str, deque] = {}
        self._stable: dict[str, int] = {}
        self._anomalous: set[str] = set()
        self._history_len = history_len
        self._anomaly_pct = anomaly_pct

    def update(self, col_name: str, new_floor: int) -> int:
        if col_name not in self._history:
            self._history[col_name] = deque(maxlen=self._history_len)
        hist = self._history[col_name]
        prev_stable = self._stable.get(col_name)
        hist.append(new_floor)
        stable = int(median(hist))
        self._stable[col_name] = stable
        if prev_stable and prev_stable > 0:
            change_pct = abs(new_floor - prev_stable) / prev_stable * 100
            if change_pct > self._anomaly_pct:
                self._anomalous.add(col_name)
                log.debug(
                    "FloorTracker: аномалия '%s': %s → %s (%.0f%%)",
                    col_name, tons_fmt(prev_stable), tons_fmt(new_floor), change_pct,
                )
        return stable

    def get(self, col_name: str) -> int | None:
        return self._stable.get(col_name)

    def get_all(self) -> dict[str, int]:
        return dict(self._stable)

    def pop_anomalous(self) -> set[str]:
        result = self._anomalous.copy()
        self._anomalous.clear()
        return result

    def count(self) -> int:
        return len(self._stable)


# ─────────────────────────────────────────────
#  RateAdaptor — авто-подстройка интервала
# ─────────────────────────────────────────────

class RateAdaptor:
    """
    Адаптивный регулятор интервала сканирования с защитой от каскадов 429.
    - Имеет динамический нижний порог effective_min, зависящий от количества слотов:
      max(0.40, 1.6 / max(1, num_slots)).
    - При получении 429 от ЛЮБОГО слота немедленно повышает интервал (+0.15с или +0.20с при штрафе >= 30с)
      и сбрасывает серию чистых сканов.
    - Снижает интервал только после серии из 50 успешных сканов подряд без единого 429
      (шаг -0.02с) до effective_min.
    """

    def __init__(
        self,
        initial_interval: float,
        min_interval: float = 0.40,
        max_interval: float = MAX_SCAN_INTERVAL,
        pool: Optional[AccountPool] = None,
    ):
        self._min_base = max(0.40, min_interval)
        self._max = max_interval
        self._pool = pool
        self._auto = True
        self._clean_streak = 0
        self.interval = max(self.effective_min, min(self._max, initial_interval))

    @property
    def effective_min(self) -> float:
        """
        Динамический пол интервала.
        tgmrkt держит ~1 req/s на IP. Чтобы не перегружать слоты,
        интервал не должен быть меньше 1.6s / num_slots (и не меньше 0.40s).
        """
        num_slots = len(self._pool.slots) if (self._pool and self._pool.slots) else 1
        return max(self._min_base, 1.6 / max(1, num_slots))

    def on_429(self, penalty_sec: float = 15.0) -> float:
        """
        Срочная реакция на 429 от любого слота.
        Немедленно повышает интервал и сбрасывает серию чистых сканов.
        """
        self._clean_streak = 0
        if not self._auto:
            return self.interval

        old = self.interval
        step = 0.20 if penalty_sec >= 30.0 else 0.15
        self.interval = min(self._max, max(self.effective_min, self.interval + step))
        if abs(self.interval - old) > 0.001:
            log.warning(
                "⚡ RateAdaptor [429]: экстренное увеличение интервала %.2fс → %.2fс (штраф %.0fс)",
                old, self.interval, penalty_sec,
            )
        return self.interval

    def record_ok(self) -> bool:
        """
        Вызывается после чистых сканов (где не было 429).
        Каждые 50 чистых сканов осторожно снижает интервал на 0.02с до effective_min.
        Возвращает True, если интервал изменился.
        """
        if not self._auto:
            return False

        self._clean_streak += 1
        if self._clean_streak >= 50:
            self._clean_streak = 0
            cur_min = self.effective_min
            if self.interval > cur_min + 0.005:
                old = self.interval
                self.interval = max(cur_min, self.interval - 0.02)
                log.info(
                    "⚡ RateAdaptor: серия 50 чистых сканов, интервал %.2fс → %.2fс (пол: %.2fс)",
                    old, self.interval, cur_min,
                )
                return True
        return False

    def set_manual(self, interval: float) -> None:
        self.interval = max(0.1, min(self._max, interval))
        self._auto = False

    def set_auto(self) -> None:
        self._auto = True
        self._clean_streak = 0
        self.interval = max(self.effective_min, self.interval)

    @property
    def is_auto(self) -> bool:
        return self._auto


# ─────────────────────────────────────────────
#  API с retry и логированием
# ─────────────────────────────────────────────

async def api_request_async(
    method: str,
    endpoint: str,
    pool: AccountPool,
    session: AsyncSession,
    json_data: dict | None = None,
) -> Any:
    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES):
        slot = await pool.next_async()
        t0 = time.monotonic()

        log.debug("API %s %s | слот: %s | попытка %d/%d", method.upper(), endpoint, slot.label, attempt + 1, MAX_RETRIES)

        try:
            if method.upper() == "GET":
                r = await session.get(
                    f"{MARKET_API_URL}{endpoint}",
                    headers=slot.headers,
                    proxies=slot.proxies,
                    timeout=REQUEST_TIMEOUT,
                )
            else:
                r = await session.post(
                    f"{MARKET_API_URL}{endpoint}",
                    headers=slot.headers,
                    json=json_data or {},
                    proxies=slot.proxies,
                    timeout=REQUEST_TIMEOUT,
                )
            elapsed = time.monotonic() - t0

            if elapsed > REQUEST_TIMEOUT:
                needs_replace = slot.record_timeout()
                if needs_replace:
                    new_prx = pool.replace_slot_proxy(slot)
                    if new_prx:
                        log.warning("⚠️ Слот [%s] ответил за %.2fс — заменён [%s]", slot.token[:8], elapsed, new_prx.cfg.name)
                    else:
                        log.warning("⚠️ Слот [%s] ответил за %.2fс — отключён", slot.label, elapsed)

            if r.status_code == 401:
                log.error("HTTP 401 | Токен просрочен (слот: %s)", slot.label)
                stats_tracker.record_error("HTTP 401", f"Токен просрочен (слот: {slot.label})", slot.label, endpoint)
                last_exc = AuthTokenExpiredError(f"HTTP 401: Токен просрочен (слот: {slot.label})")
                continue

            if r.status_code == 429:
                raw_retry = r.headers.get("Retry-After")
                try:
                    retry_val = float(raw_retry) if raw_retry else 0.0
                except (ValueError, TypeError):
                    retry_val = 0.0
                retry_after = retry_val if retry_val > 0 else PENALTY_429
                log.warning("429 | слот: %s | штраф: %.0fс", slot.label, retry_after)
                pool.penalize(slot, retry_after)
                stats_tracker.record_error("HTTP 429", f"penalty {retry_after}s", slot.label, endpoint)
                if _global_scanner_state is not None:
                    _global_scanner_state.record_429()
                    adaptor = getattr(_global_scanner_state, "rate_adaptor", None)
                    if adaptor is not None:
                        adaptor.on_429(retry_after)
                        _global_scanner_state.scan_interval = adaptor.interval
                        save_settings(_global_scanner_state)
                last_exc = Exception(f"HTTP 429 (слот: {slot.label})")
                await asyncio.sleep(0.5)
                continue

            r.raise_for_status()
            slot.record_success()
            log.debug("API ответ: %d | %.2fс | %s", r.status_code, elapsed, slot.label)
            return r.json()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            elapsed = time.monotonic() - t0
            err_name = type(e).__name__
            if "Timeout" in err_name or "timed out" in str(e).lower():
                needs_replace = slot.record_timeout()
                if needs_replace:
                    new_prx = pool.replace_slot_proxy(slot)
                    if new_prx:
                        log.warning("⚠️ Слот [%s] таймаут — заменён [%s]", slot.token[:8], new_prx.cfg.name)
                    else:
                        log.warning("⚠️ Слот [%s] таймаут — отключён", slot.label)
            if "429" in str(e):
                pool.penalize(slot, PENALTY_429)
                log.warning("429 (из исключения) | слот: %s | штраф: %.0fс", slot.label, PENALTY_429)
                stats_tracker.record_error("HTTP 429", str(e), slot.label, endpoint)
                if _global_scanner_state is not None:
                    _global_scanner_state.record_429()
                    adaptor = getattr(_global_scanner_state, "rate_adaptor", None)
                    if adaptor is not None:
                        adaptor.on_429(PENALTY_429)
                        _global_scanner_state.scan_interval = adaptor.interval
                        save_settings(_global_scanner_state)
            else:
                log.warning("Ошибка %s %s | %s | %.2fс | %s", method.upper(), endpoint, slot.label, elapsed, e)
                stats_tracker.record_error(err_name, str(e), slot.label, endpoint)
            last_exc = e
            # Нет sleep(0.3) — сразу берём следующий слот

    raise last_exc or RuntimeError(f"Все попытки исчерпаны: {method} {endpoint}")


async def api_post_async(endpoint: str, json_data: dict, pool: AccountPool, session: AsyncSession) -> dict:
    return await api_request_async("POST", endpoint, pool, session, json_data)


async def api_get_async(endpoint: str, pool: AccountPool, session: AsyncSession) -> Any:
    return await api_request_async("GET", endpoint, pool, session)


# ─────────────────────────────────────────────
#  Загрузка листингов (параллельный Async)
# ─────────────────────────────────────────────

async def fetch_page_async(cursor: str, pool: AccountPool, session: AsyncSession) -> dict:
    return await api_post_async("/gifts/saling", {
        "collectionNames": [],
        "modelNames": [],
        "backdropNames": [],
        "symbolNames": [],
        "ordering": "None",
        "lowToHigh": False,
        "maxPrice": None,
        "minPrice": None,
        "mintable": None,
        "number": None,
        "count": 20,
        "cursor": cursor,
        "query": None,
        "promotedFirst": False,
    }, pool, session)


async def fetch_new_listings_async(
    pool: AccountPool,
    seen_ids: Any,
    first_run: bool,
    session: AsyncSession,
) -> tuple[list[dict], list[dict]]:
    """
    Загрузка первой страницы листинга (20 подарков).
    Возвращает (new_gifts, page_gifts).
    """
    data = await fetch_page_async("", pool, session)
    gifts = data.get("gifts", [])
    log.debug("Страница 1: %d подарков", len(gifts))

    if not gifts:
        return [], []

    if first_run:
        return gifts, gifts

    new_gifts: list[dict] = []
    for gift in gifts:
        gid = gift.get("id")
        if gid in seen_ids:
            break
        new_gifts.append(gift)

    return new_gifts, gifts


# ─────────────────────────────────────────────
#  Загрузка флоров NFT-коллекций
# ─────────────────────────────────────────────

async def fetch_floors_async(
    pool: AccountPool,
    session: AsyncSession,
    floor_tracker: FloorTracker,
) -> tuple[int | None, dict[str, int], dict[str, int]]:
    """
    Получает флоры всех коллекций из /gifts/collections (один запрос).
    Также запрашивает флор чёрного фона из листинга Black подарков.
    Возвращает (black_floor_nano, collection_floors, collection_volumes).
    """
    log.debug("Обновляем флоры коллекций...")

    try:
        collections_data = await api_get_async("/gifts/collections", pool, session)
    except Exception as e:
        log.error("Не удалось получить /gifts/collections: %s", e)
        return None, floor_tracker.get_all(), {}

    if not isinstance(collections_data, list):
        log.error("Некорректный формат /gifts/collections: %s", type(collections_data))
        return None, floor_tracker.get_all(), {}

    collection_volumes: dict[str, int] = {}

    for c in collections_data:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        c_name = c["name"]
        vol = c.get("volume")
        if vol is not None:
            collection_volumes[c_name] = int(vol)
        raw_floor = c.get("floorPriceNanoTons")
        if raw_floor is not None and int(raw_floor) > 0:
            floor_tracker.update(c_name, int(raw_floor))

    # Отдельный запрос для точного флора чёрного фона
    black_floor = await _fetch_black_floor_from_listing(pool, session)

    collection_floors = floor_tracker.get_all()
    log.debug("Флоры обновлены: %d коллекций, чёрный: %s", len(collection_floors), tons_fmt(black_floor) if black_floor else "N/A")
    return black_floor, collection_floors, collection_volumes


async def _fetch_black_floor_from_listing(pool: AccountPool, session: AsyncSession) -> int | None:
    try:
        data = await api_post_async("/gifts/saling", {
            "collectionNames": [],
            "modelNames": [],
            "backdropNames": list(BLACK_BACKDROPS),
            "symbolNames": [],
            "ordering": "Price",
            "lowToHigh": True,
            "maxPrice": None,
            "minPrice": None,
            "mintable": None,
            "number": None,
            "count": 20,
            "cursor": "",
            "query": None,
            "promotedFirst": False,
        }, pool, session)
    except Exception as e:
        log.warning("Не удалось получить листинг Black: %s", e)
        return None

    prices = sorted(int(g["salePrice"]) for g in data.get("gifts", []) if g.get("salePrice"))
    floor = prices[1] if len(prices) >= 2 else (prices[0] if prices else None)
    log.debug("Флор чёрного: %s (%d позиций)", tons_fmt(floor) if floor else "N/A", len(prices))
    return floor


# ─────────────────────────────────────────────
#  Проверка сделок
# ─────────────────────────────────────────────

def check_gift(
    gift: dict,
    black_floor: int | None,
    collection_floors: dict[str, int],
    collection_volumes: dict[str, int] | None = None,
    min_ton_diff: float = MIN_TON_DIFF,
    cheap_threshold: float = CHEAP_PRICE_THRESHOLD,
    min_turnover_ratio: float = 0.0,
    max_price_nano: int | None = None,
) -> list[dict]:
    """
    Три условия покупки:
    1. Черный фон + цена ниже флора черного на min_ton_diff.
    2. Цена < cheap_threshold (абсолютно дёшево).
    3. Цена ниже флора коллекции (NFT floor) на min_ton_diff.

    Доп. фильтры: баланс, оборот.
    """
    deals: list[dict] = []
    price = gift.get("salePrice")
    if not price:
        return deals
    price = int(price)
    price_ton = tons(price)
    collection_name = gift.get("collectionName")
    model_name = gift.get("modelName")
    backdrop_name = gift.get("backdropName")

    if max_price_nano is not None and price > max_price_nano:
        return deals

    gift_num = gift.get("number") if gift.get("number") is not None else gift.get("num")
    is_low_id = False
    low_id_val = None
    try:
        if gift_num is not None:
            low_id_val = int(gift_num)
            is_low_id = (0 < low_id_val < 100)
    except (ValueError, TypeError):
        is_low_id = False

    col_vol = (collection_volumes or {}).get(collection_name, 0)
    turnover_ratio = (col_vol / price) if price > 0 else 0.0
    is_turnover_exempt = (backdrop_name in BLACK_BACKDROPS) or (price_ton < cheap_threshold) or is_low_id
    if not is_turnover_exempt and min_turnover_ratio > 0 and turnover_ratio < min_turnover_ratio:
        return deals

    # 1. Чёрный фон
    if backdrop_name in BLACK_BACKDROPS and black_floor:
        diff_ton = tons(black_floor - price)
        if diff_ton >= min_ton_diff:
            pct = (black_floor - price) / black_floor * 100
            deals.append({
                "type": "BLACK",
                "gift": gift,
                "price": price,
                "floor": black_floor,
                "pct": pct,
                "diff_ton": diff_ton,
                "floor_src": "черный фон",
                "turnover_ratio": turnover_ratio,
                "collection_volume": col_vol,
            })

    # 2. Абсолютно дёшево
    if price_ton < cheap_threshold:
        col_floor = collection_floors.get(collection_name, price)
        diff_ton = tons(col_floor - price) if col_floor > price else 0.0
        pct = (col_floor - price) / col_floor * 100 if col_floor > 0 else 0.0
        deals.append({
            "type": "CHEAP",
            "gift": gift,
            "price": price,
            "floor": col_floor,
            "pct": pct,
            "diff_ton": diff_ton,
            "floor_src": f"дешевле {cheap_threshold:.2f} TON",
            "turnover_ratio": turnover_ratio,
            "collection_volume": col_vol,
        })

    # 3. Флор коллекции (NFT) или редкий ID
    col_floor = collection_floors.get(collection_name)
    if col_floor:
        diff_ton = tons(col_floor - price)
        pct = (col_floor - price) / col_floor * 100 if col_floor > 0 else 0.0
        max_allowed_low_id = int(col_floor * LOW_ID_MAX_FLOOR_RATIO) if is_low_id else -1

        if is_low_id and price <= max_allowed_low_id:
            deals.append({
                "type": "LOW_ID",
                "gift": gift,
                "price": price,
                "floor": col_floor,
                "pct": pct,
                "diff_ton": diff_ton,
                "floor_src": f"редкий ID #{low_id_val} (<=  {LOW_ID_MAX_FLOOR_RATIO*100:.0f}% флора)",
                "turnover_ratio": turnover_ratio,
                "collection_volume": col_vol,
            })
        elif diff_ton >= min_ton_diff:
            deals.append({
                "type": "NFT",
                "gift": gift,
                "price": price,
                "floor": col_floor,
                "pct": pct,
                "diff_ton": diff_ton,
                "floor_src": f"флор коллекции {collection_name}",
                "turnover_ratio": turnover_ratio,
                "collection_volume": col_vol,
            })

    return deals


# ─────────────────────────────────────────────
#  Вывод в консоль + логирование
# ─────────────────────────────────────────────

def print_and_log_deal(deal: dict) -> None:
    gift = deal["gift"]
    _tags = {
        "BLACK":  "🖤  ЧЁРНЫЙ ФОН",
        "CHEAP":  f"💸  ДЁШЕВО (<{CHEAP_PRICE_THRESHOLD:.1f} TON)",
        "NFT":    "🎯  НИЖЕ ФЛОРА КОЛЛЕКЦИИ",
        "LOW_ID": "🏷️  РЕДКИЙ НОМЕР (<100)",
    }
    tag = _tags.get(deal["type"], "🔥  ВЫГОДНАЯ СДЕЛКА")
    diff_ton = deal.get("diff_ton", tons(deal["floor"] - deal["price"]))

    tr = deal.get("turnover_ratio")
    vol_nano = deal.get("collection_volume")
    tr_line = []
    if tr is not None and vol_nano is not None:
        tr_line.append(f"  📊  Оборот/цена: {tr:.1f}x  (объём: {vol_nano/1e9:,.0f} TON)")

    lines = [
        SEPARATOR,
        f"  {tag}  —  выгода {diff_ton:.2f} TON (скидка {deal['pct']:.1f}%)",
        f"  📦  {gift.get('collectionName', '?')}  |  модель: {gift.get('modelName', '?')}",
        f"  🎨  Фон: {gift.get('backdropName', '?')}  |  узор: {gift.get('symbolName', '?')}  |  #{gift.get('number', '?')}",
        f"  💰  Цена: {tons_fmt(deal['price'])}  (флор: {tons_fmt(deal['floor'])} [{deal['floor_src']}])",
        *tr_line,
        f"  🔗  {gift_url(gift)}",
        "",
    ]
    print("\n".join(lines))

    tr_info = f" | оборот: {tr:.1f}x" if tr is not None else ""
    log.info(
        "DEAL [%s] выгода %.2f TON (%.1f%%) | %s #%s | %.2f TON → флор %.2f TON%s | %s",
        deal["type"], diff_ton, deal["pct"],
        gift.get("collectionName"), gift.get("number"),
        tons(deal["price"]), tons(deal["floor"]),
        tr_info, gift_url(gift),
    )
    log_deal_to_file(deal)


# ─────────────────────────────────────────────
#  Детектирование выкупленных лотов
# ─────────────────────────────────────────────

async def _edit_sold_alert_async(
    bot_token: str,
    alert_info: dict,
    elapsed_sec: int,
) -> None:
    """Редактирует Telegram-сообщение о лоте, помечая его как выкупленный с временем."""
    if not bot_token or not alert_info:
        return
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    messages = alert_info.get("messages", {})
    if not messages:
        return

    orig_text = alert_info.get("text", "")
    nft_url = alert_info.get("nft_url", "")

    header_sold = f"☑️ <b>ЛОТ ВЫКУПЛЕН</b> (за {elapsed_sec} сек)"
    if orig_text:
        parts = orig_text.split("\n\n", 1)
        body = parts[1] if len(parts) > 1 else orig_text
        new_text = f"{header_sold}\n\n{body}"
    else:
        new_text = header_sold

    reply_markup = None
    if nft_url:
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🎁 Открыть NFT в Telegram", url=nft_url)]]
        )

    bot = Bot(token=bot_token)
    try:
        for admin_id, msg_id in messages.items():
            try:
                await bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=msg_id,
                    text=new_text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            except Exception as e:
                log.debug("Ошибка редактирования сообщения %s (sold): %s", msg_id, e)
    finally:
        await bot.session.close()


def check_sold_alerts(
    page_gifts: list[dict],
    scanner_state: ScannerState,
    bot_token: str,
) -> None:
    """
    Проверяет отправленные алерты на выкуп:
    1. Если подарок всё ещё на странице 1 — обновляем список ID, идущих после него.
    2. Если подарок исчез, но ХОТЯ БЫ ОДИН подарок, который был ПОСЛЕ него, всё ещё на странице — лот выкуплен!
    3. Если исчезли и подарок, и все последующие — они просто сместились на страницу 2 (НЕ помечаем как выкупленные).
    4. Старые алерты (> 15 минут) удаляются для очистки памяти.
    """
    if not scanner_state.sent_alerts:
        return

    now = time.time()
    current_page_ids = {g.get("id") for g in page_gifts if g.get("id")}
    sold_deals = []
    expired_ids = []

    for gid, info in list(scanner_state.sent_alerts.items()):
        # Очистка памяти: алерты старше 15 минут удаляем
        if now - info.get("sent_at", now) > 900:
            expired_ids.append(gid)
            continue

        # Подарок всё ещё виден на первой странице
        if gid in current_page_ids:
            found = False
            new_older = set()
            for g in page_gifts:
                if found:
                    oid = g.get("id")
                    if oid:
                        new_older.add(oid)
                elif g.get("id") == gid:
                    found = True
            if new_older:
                info["older_ids"] = new_older
            continue

        # Подарок исчез с первой страницы
        older_ids = info.get("older_ids", set())
        # Если хотя бы один лот, стоявший ПОСЛЕ него, всё ещё на странице —
        # значит страница не уехала вперёд, а лот был именно выкуплен/удалён!
        if older_ids and (older_ids & current_page_ids):
            elapsed_sec = max(1, int(now - info.get("sent_at", now)))
            sold_deals.append((gid, info, elapsed_sec))
        else:
            # Лот и все следующие за ним уехали вниз (смещение пагинации) — не помечаем выкупленным
            pass

    for gid in expired_ids:
        scanner_state.sent_alerts.pop(gid, None)

    for gid, info, elapsed_sec in sold_deals:
        scanner_state.sent_alerts.pop(gid, None)
        log.info("🎯 Лот %s выкуплен за %dс!", gid, elapsed_sec)
        if bot_token:
            asyncio.create_task(_edit_sold_alert_async(bot_token, info, elapsed_sec))


async def process_deal_async(
    deal: dict,
    older_ids: set[str],
    scanner_state: ScannerState,
    bot_token: str,
    admin_ids: set[int],
    pool: AccountPool,
) -> None:
    """
    Обрабатывает найденную сделку:
    - Если AutoBuy ВКЛ: мгновенно выкупает через POST /gifts/buy без лишних задержек,
      сверяя перед этим с последним известным балансом, затем проверяет Хранилище
      и отправляет подробный отчёт.
    - Если AutoBuy ВЫКЛ: отправляет стандартный алерт с кнопкой [💳 Купить за X.XX TON].
    """
    gift = deal.get("gift", {})
    deal_gid = gift.get("id", "")
    price_nano = deal.get("price", 0)
    price_ton = price_nano / 1e9

    if scanner_state.auto_buy:
        known_balance = scanner_state.primary_balance_nano

        # Сверка с последним известным балансом (БЕЗ доп сетевого запроса!)
        if known_balance is not None and price_nano > known_balance:
            bal_ton = known_balance / 1e9
            reason = f"Недостаточно средств (баланс: {bal_ton:.2f} TON, цена: {price_ton:.2f} TON)"
            log.warning("AutoBuy: %s для лота %s", reason, deal_gid)
            await send_autobuy_skipped_notification(bot_token, admin_ids, deal, reason)
            # Присылаем обычный алерт с кнопкой ручной покупки
            await send_deal_notification(
                bot_token,
                admin_ids,
                deal,
                scanner_state=scanner_state,
                older_ids=older_ids,
                with_buy_button=True,
            )
            return

        prim_slot = pool.get_primary_slot()
        if not prim_slot:
            log.error("AutoBuy: нет активного основного аккаунта в пуле!")
            await send_deal_notification(
                bot_token,
                admin_ids,
                deal,
                scanner_state=scanner_state,
                older_ids=older_ids,
                with_buy_button=True,
            )
            return

        log.info("⚡ [AutoBuy] Мгновенный выкуп лота %s за %.2f TON...", deal_gid, price_ton)
        t_buy = time.monotonic()
        # Оптимистичное списание баланса
        if scanner_state.primary_balance_nano is not None:
            scanner_state.primary_balance_nano = max(0, scanner_state.primary_balance_nano - price_nano)

        buy_ok, buy_msg, buy_data = await buy_gift_async(
            gift_id=deal_gid,
            price_nano=price_nano,
            token=prim_slot.token,
            proxies=prim_slot.proxies,
        )
        elapsed = time.monotonic() - t_buy

        # Фоновое обновление баланса
        async def _refresh_primary_balance():
            try:
                ok_b, _, bdata = await verify_token_async(prim_slot.token, proxy=prim_slot.proxy)
                if ok_b and "hard" in bdata:
                    scanner_state.primary_balance_nano = int(bdata["hard"])
            except Exception:
                pass

        asyncio.create_task(_refresh_primary_balance())

        if buy_ok:
            log.info("🎉 [AutoBuy] Лот %s успешно выкуплен за %.2f с!", deal_gid, elapsed)
            # Проверяем попадание в Хранилище (инвентарь)
            in_vault = await verify_gift_in_vault_async(deal_gid, prim_slot.token, prim_slot.proxies)
            await send_autobuy_success_report(
                bot_token=bot_token,
                admin_ids=admin_ids,
                deal=deal,
                buy_data=buy_data,
                in_vault=in_vault,
                buy_elapsed=elapsed,
                scanner_state=scanner_state,
            )
        else:
            log.warning("❌ [AutoBuy] Не удалось выкупить лот %s: %s (%.2f с)", deal_gid, buy_msg, elapsed)
            await send_autobuy_failed_report(
                bot_token=bot_token,
                admin_ids=admin_ids,
                deal=deal,
                reason=buy_msg,
                buy_elapsed=elapsed,
                scanner_state=scanner_state,
            )
    else:
        # AutoBuy выключен — присылаем обычный алерт с кнопкой ручной покупки
        await send_deal_notification(
            bot_token=bot_token,
            admin_ids=admin_ids,
            deal=deal,
            scanner_state=scanner_state,
            older_ids=older_ids,
            with_buy_button=True,
        )


# ─────────────────────────────────────────────
#  Основной цикл
# ─────────────────────────────────────────────


_shutdown = asyncio.Event()

def _handle_signal(sig, frame):
    log.info("Получен сигнал %s, завершаем...", signal.Signals(sig).name)
    _shutdown.set()

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


async def main() -> None:
    startup_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header_lines = [
        "=" * 60,
        f"  🚀  MRKT Gift Scanner (Async)  (старт: {startup_ts})",
        f"  Порог выгоды:  {MIN_TON_DIFF:.2f} TON  |  дёшево < {CHEAP_PRICE_THRESHOLD:.2f} TON",
        f"  Интервал:      {SCAN_INTERVAL:.2f} сек (авто-адаптация)",
        f"  Флоры:         раз в {FLOOR_REFRESH} сканов | история: {FLOOR_HISTORY_LEN} точек",
        f"  Штраф 429:     {PENALTY_429:.0f} сек",
        f"  Чёрные фоны:   {', '.join(BLACK_BACKDROPS)}",
        f"  Логи:          {LOG_DIR.resolve()}",
        "─" * 60,
        "  Загрузка аккаунтов и прокси...",
    ]
    for line in header_lines:
        print(line)
    log.info("=" * 50)
    log.info("Запуск MRKT Scanner (Async) | порог выгоды: %.2f TON", MIN_TON_DIFF)

    try:
        pool = await build_pool_async(max_ping_seconds=MAX_PING_SECONDS)
    except RuntimeError as e:
        log.critical("Ошибка инициализации пула: %s", e)
        print(f"\n❌  {e}")
        sys.exit(1)

    if PRIMARY_TOKEN and pool:
        pool.set_primary_token(PRIMARY_TOKEN)

    print(f"  {pool}")
    log.info("Пул: %s", pool)
    print("=" * 60)

    loop = asyncio.get_running_loop()
    def _sig_handler():
        _shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig_handler)
        except (NotImplementedError, RuntimeError):
            pass

    # Стартовый интервал и персистентные настройки
    saved_settings = load_settings()
    init_min_ton_diff = float(saved_settings.get("min_ton_diff", MIN_TON_DIFF))
    init_cheap_threshold = float(saved_settings.get("cheap_price_threshold", CHEAP_PRICE_THRESHOLD))
    init_min_turnover = float(saved_settings.get("min_turnover_ratio", MIN_TURNOVER_RATIO))
    init_filter_balance = bool(saved_settings.get("filter_by_balance", FILTER_BY_BALANCE))
    init_autobuy = bool(saved_settings.get("auto_buy", False))

    avg_ping = pool.avg_ping_ms() if hasattr(pool, "avg_ping_ms") else None
    if "scan_interval" in saved_settings:
        start_interval = float(saved_settings["scan_interval"])
        log.info("Интервал из сохранённых настроек: %.2f с", start_interval)
    elif avg_ping and avg_ping > 0:
        start_interval = max(MIN_SCAN_INTERVAL, min(avg_ping / 1000.0 * 1.5, SCAN_INTERVAL))
        log.info("Стартовый интервал из пинга %.0f мс: %.2f с", avg_ping, start_interval)
    else:
        start_interval = SCAN_INTERVAL

    adaptor = RateAdaptor(initial_interval=start_interval, pool=pool)

    scanner_state = ScannerState(
        pool=pool,
        is_paused=False,
        auto_buy=init_autobuy,
        min_ton_diff=init_min_ton_diff,
        cheap_price_threshold=init_cheap_threshold,
        min_turnover_ratio=init_min_turnover,
        filter_by_balance=init_filter_balance,
        scan_interval=adaptor.interval,
        scans_count=0,
        deals_count=0,
        start_time=time.monotonic(),
        black_floor_nano=None,
        collection_floors_count=0,
        rate_adaptor=adaptor,
    )
    if "notify_categories" in saved_settings and isinstance(saved_settings["notify_categories"], dict):
        scanner_state.notify_categories.update(saved_settings["notify_categories"])

    global _global_scanner_state
    _global_scanner_state = scanner_state
    save_settings(scanner_state)


    bot_task = None
    if TG_BOT_TOKEN and TG_ADMIN_IDS:
        log.info("Запуск Telegram бота (админы: %s)...", TG_ADMIN_IDS)
        print(f"  🤖 Telegram бот запущен для админов: {TG_ADMIN_IDS}")
        bot_task = asyncio.create_task(run_telegram_bot(TG_BOT_TOKEN, TG_ADMIN_IDS, scanner_state))
    else:
        print("  ℹ️  Telegram бот отключён (не задан TG_BOT_TOKEN или TG_ADMIN_ID в .env)")

    async def init_primary_account_async(pool_obj: AccountPool, state_obj: ScannerState, explicit_token: str = "") -> None:
        tokens = pool_obj.get_tokens()
        if not tokens:
            state_obj.primary_balance_nano = None
            return

        if explicit_token and explicit_token in tokens:
            pool_obj.set_primary_token(explicit_token)
            slot = next((s for s in pool_obj.slots if s.token == explicit_token), None)
            proxy = slot.proxy if slot else None
            ok, msg, bdata = await verify_token_async(explicit_token, proxy=proxy)
            if ok and "hard" in bdata:
                state_obj.primary_balance_nano = int(bdata["hard"])
                bal_text = f"{state_obj.primary_balance_nano / 1e9:.2f} TON"
            else:
                bal_text = f"ошибка ({msg})"
            print(f"  👑 Основной аккаунт (из .env): {_mask_token(explicit_token)} (баланс: {bal_text})")
            return

        print(f"  👑 Поиск аккаунта с наибольшим балансом среди {len(tokens)} токенов...")
        tasks = []
        for tok in tokens:
            slot = next((s for s in pool_obj.slots if s.token == tok), None)
            proxy = slot.proxy if slot else None
            tasks.append(verify_token_async(tok, proxy=proxy))

        results = await asyncio.gather(*tasks)
        best_token = tokens[0]
        best_balance = -1

        for tok, (ok, msg, bdata) in zip(tokens, results):
            if ok and "hard" in bdata:
                bal = int(bdata["hard"])
                print(f"     • {_mask_token(tok)}: {bal / 1e9:.2f} TON")
                if bal > best_balance:
                    best_balance = bal
                    best_token = tok
            else:
                print(f"     • {_mask_token(tok)}: не удалось получить баланс ({msg})")

        pool_obj.set_primary_token(best_token)
        state_obj.primary_balance_nano = max(0, best_balance) if best_balance >= 0 else None
        bal_text = f"{best_balance / 1e9:.2f} TON" if best_balance >= 0 else "0.00 TON"
        print(f"  👑 Автовыбор: {_mask_token(best_token)} (баланс: {bal_text})")

    async def update_primary_balance_async(pool_obj: AccountPool, state_obj: ScannerState) -> None:
        tok = pool_obj.primary_token
        if not tok:
            return
        try:
            slot = next((s for s in pool_obj.slots if s.token == tok), None)
            proxy = slot.proxy if slot else None
            ok, msg, bdata = await verify_token_async(tok, proxy=proxy)
            if ok and "hard" in bdata:
                state_obj.primary_balance_nano = int(bdata["hard"])
        except Exception as err:
            log.warning("Ошибка обновления баланса: %s", err)

    saved_primary = str(saved_settings.get("primary_token") or PRIMARY_TOKEN).strip()
    await init_primary_account_async(pool, scanner_state, explicit_token=saved_primary)
    save_settings(scanner_state)


    seen_ids: dict[str, None] = {}
    black_floor: int | None = None
    collection_floors: dict[str, int] = {}
    floor_tracker = FloorTracker()
    last_balance_refresh: float = time.monotonic()
    scan_count = 0
    total_deals = 0
    consecutive_401_errors = 0
    first_run = True

    try:
        async with AsyncSession() as session:
            while not _shutdown.is_set():
                if scanner_state.is_paused:
                    await asyncio.sleep(0.5)
                    continue

                if (time.monotonic() - last_balance_refresh >= 30.0) or (scanner_state.filter_by_balance and scanner_state.primary_balance_nano is None):
                    last_balance_refresh = time.monotonic()
                    asyncio.create_task(update_primary_balance_async(pool, scanner_state))

                # Синхронизация интервала с адаптором
                if scanner_state.scan_interval != adaptor.interval:
                    if adaptor.is_auto:
                        adaptor.set_manual(scanner_state.scan_interval)
                    else:
                        scanner_state.scan_interval = adaptor.interval

                # Принудительное обновление флоров
                if scanner_state.force_refresh_floors:
                    scanner_state.force_refresh_floors = False
                    scan_count = FLOOR_REFRESH - 1

                scan_count += 1
                scanner_state.scans_count = scan_count
                ts = now_str()

                # ── Обновление флоров ─────────────────────────────────────────
                if scan_count == 1 or scan_count % FLOOR_REFRESH == 0:
                    label = "первый запуск" if scan_count == 1 else f"скан #{scan_count}"
                    print(f"[{ts}] 🔄 Флоры ({label})...", end=" ", flush=True)
                    try:
                        bf, cf, cv = await fetch_floors_async(pool, session, floor_tracker)
                        if cf:
                            collection_floors = cf
                            scanner_state.collection_floors = cf
                            scanner_state.collection_floors_count = len(collection_floors)
                            scanner_state.collection_volumes = cv
                        if bf:
                            black_floor = bf
                            scanner_state.black_floor_nano = black_floor
                        print(f"{len(collection_floors)} коллекций | 🖤 {tons_fmt(black_floor) if black_floor else 'N/A'}")
                        log.info("Флоры: %d коллекций | чёрный: %s", len(collection_floors), tons_fmt(black_floor) if black_floor else "N/A")

                        anomalous = floor_tracker.pop_anomalous()
                        if anomalous:
                            log.info("Зафиксированы колебания флора (%d коллекций): %s", len(anomalous), ", ".join(list(anomalous)[:5]))

                    except Exception as e:
                        log.error("Не удалось обновить флоры: %s", e)
                        print(f"ошибка: {e}")

                # ── Скан ─────────────────────────────────────────────────────
                label = " (первый скан)" if first_run else ""
                log.debug("Скан #%d%s", scan_count, label)
                print(f"[{ts}] ⟳ Скан #{scan_count}{label}...", end=" ", flush=True)

                try:
                    scan_start_429 = scanner_state.get_429_count_last_hour()
                    t_scan = time.monotonic()
                    new_gifts, page_gifts = await fetch_new_listings_async(pool, seen_ids, first_run, session)
                    elapsed = time.monotonic() - t_scan
                    consecutive_401_errors = 0

                    if first_run:
                        for g in page_gifts:
                            gid = g.get("id")
                            if gid:
                                seen_ids[gid] = None
                    else:
                        for g in new_gifts:
                            gid = g.get("id")
                            if gid:
                                seen_ids[gid] = None

                    # Ограничение памяти seen_ids (макс 5000 элементов)
                    while len(seen_ids) > 5000:
                        seen_ids.pop(next(iter(seen_ids)))

                    scan_deals_count = 0
                    if not first_run and new_gifts:
                        scan_deals: list[dict] = []
                        max_price_nano = scanner_state.primary_balance_nano if scanner_state.filter_by_balance else None
                        for gift in new_gifts:
                            scan_deals.extend(
                                check_gift(
                                    gift,
                                    black_floor,
                                    collection_floors,
                                    collection_volumes=scanner_state.collection_volumes,
                                    min_ton_diff=scanner_state.min_ton_diff,
                                    cheap_threshold=scanner_state.cheap_price_threshold,
                                    min_turnover_ratio=scanner_state.min_turnover_ratio,
                                    max_price_nano=max_price_nano,
                                )
                            )

                        if scan_deals:
                            scan_deals_count = len(scan_deals)
                            total_deals += scan_deals_count
                            scanner_state.deals_count = total_deals
                            print(f"\n[{ts}]  ✅ {len(scan_deals)} предложений! (сессия: {total_deals})\n")
                            log.info("!!! Найдено %d сделок (сессия: %d)", len(scan_deals), total_deals)
                            for deal in scan_deals:
                                print_and_log_deal(deal)
                                if TG_BOT_TOKEN and TG_ADMIN_IDS:
                                    deal_type = deal.get("type", "NFT")
                                    if scanner_state.notify_categories.get(deal_type, True):
                                        deal_gid = deal.get("gift", {}).get("id")
                                        older_ids = set()
                                        found = False
                                        for pg in page_gifts:
                                            if found:
                                                ogid = pg.get("id")
                                                if ogid:
                                                    older_ids.add(ogid)
                                            elif pg.get("id") == deal_gid:
                                                found = True

                                        asyncio.create_task(
                                            process_deal_async(
                                                deal=deal,
                                                older_ids=older_ids,
                                                scanner_state=scanner_state,
                                                bot_token=TG_BOT_TOKEN,
                                                admin_ids=TG_ADMIN_IDS,
                                                pool=pool,
                                            )
                                        )

                                    else:
                                        scanner_state.vault.append(deal)
                                        log.info("Сделка [%s] #%s → Хранилище (%d)", deal_type, deal.get("gift", {}).get("number"), len(scanner_state.vault))

                    # Детектирование выкупленных лотов на основе текущей страницы
                    if not first_run and page_gifts:
                        check_sold_alerts(page_gifts, scanner_state, TG_BOT_TOKEN)

                    stats_tracker.record_scan(new_gifts, scan_deals_count)

                    print(f"+{len(new_gifts)} новых  |  в базе: {len(seen_ids)}  |  {elapsed:.1f}с")
                    log.info("Скан #%d: +%d новых | база: %d | %.1fс", scan_count, len(new_gifts), len(seen_ids), elapsed)

                    if scanner_state.get_429_count_last_hour() == scan_start_429:
                        if adaptor.record_ok():
                            scanner_state.scan_interval = adaptor.interval
                            save_settings(scanner_state)
                            log.info("⚡ Авто-интервал: %.2fс", adaptor.interval)

                except AuthTokenExpiredError as e:
                    consecutive_401_errors += 1
                    print(f"\n[{ts}] ❌ {e}")
                    if consecutive_401_errors >= 3:
                        log.critical("❌ Все токены просрочены (HTTP 401).")
                        print(f"\n🛑 Все токены просрочены. Ожидание обновления через Telegram бота...")
                        scanner_state.is_paused = True
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str:
                        print(f"\n[{ts}] ⏳ 429 — пауза 3с...")
                        log.warning("Скан #%d: все слоты на штрафе (429).", scan_count)
                        stats_tracker.record_error("HTTP 429", err_str)
                        scanner_state.record_429()
                        adaptor.on_429(30.0)
                        scanner_state.scan_interval = adaptor.interval
                        save_settings(scanner_state)
                        await asyncio.sleep(3.0)
                    else:
                        print(f"\n[{ts}] ❌ {e}")
                        log.error("Ошибка в скане #%d: %s", scan_count, e, exc_info=True)
                        stats_tracker.record_error(type(e).__name__, str(e))

                first_run = False

                current_interval = scanner_state.scan_interval
                try:
                    await asyncio.wait_for(
                        asyncio.shield(asyncio.ensure_future(_shutdown.wait())),
                        timeout=current_interval,
                    )
                    break
                except asyncio.TimeoutError:
                    pass

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n👋 Сканирование остановлено.")
    finally:
        stats_tracker.save()
        log.info("Остановка: сканов: %d, сделок: %d", scan_count, total_deals)
        print(f"\n👋 Остановлено. Сканов: {scan_count}, сделок: {total_deals}")
        if bot_task:
            bot_task.cancel()
        pool.stop_all_proxies()


if __name__ == "__main__":
    asyncio.run(main())
