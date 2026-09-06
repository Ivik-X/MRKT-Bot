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
import signal
import sys
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from curl_cffi import requests as cffi_requests
from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

from account_pool import AccountPool, Slot, build_pool

load_dotenv()

# ─────────────────────────────────────────────
#  Конфиг
# ─────────────────────────────────────────────

MARKET_API_URL       = "https://api.tgmrkt.io/api/v1"
SCAN_INTERVAL        = float(os.getenv("SCAN_INTERVAL", 0.5))
MIN_TON_DIFF         = float(os.getenv("MIN_TON_DIFF", 2.5))
CHEAP_PRICE_THRESHOLD = float(os.getenv("CHEAP_PRICE_THRESHOLD", 3.0))  # абсолютный порог: < N TON → всегда сделка
BLACK_FLOOR_REFRESH  = int(os.getenv("BLACK_FLOOR_REFRESH", 40))
MODEL_FLOOR_REFRESH_HOURS = float(os.getenv("MODEL_FLOOR_REFRESH_HOURS", 12.0))
MODEL_FLOOR_REFRESH_SECS = MODEL_FLOOR_REFRESH_HOURS * 3600.0
REQUEST_TIMEOUT      = float(os.getenv("REQUEST_TIMEOUT", 1.5))       # Таймаут: >1.5с отключает медленный прокси
MAX_RETRIES          = int(os.getenv("MAX_RETRIES", 3))
PENALTY_429          = float(os.getenv("PENALTY_429", 60.0))
LOG_DIR              = Path(os.getenv("LOG_DIR", "logs"))

BLACK_BACKDROPS = {"Black"}


# ─────────────────────────────────────────────
#  Логирование
# ─────────────────────────────────────────────

def setup_logging() -> tuple[logging.Logger, logging.Logger]:
    """
    Настраивает два логгера:
      scanner — подробный лог сканера (файл + консоль)
      deals   — только найденные сделки в JSONL формате

    Файлы:
      logs/scanner_YYYY-MM-DD.log  — ротация раз в сутки, хранить 30 дней
      logs/deals.jsonl             — все найденные сделки, один JSON на строку
    """
    LOG_DIR.mkdir(exist_ok=True)

    # ── Формат ───────────────────────────────────────────────────────────
    fmt_file    = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fmt_console = logging.Formatter(
        "%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── Scanner logger ────────────────────────────────────────────────────
    scanner_log = logging.getLogger("scanner")
    scanner_log.setLevel(logging.DEBUG)

    # В файл (ротация раз в день, 30 файлов)
    fh = TimedRotatingFileHandler(
        LOG_DIR / "scanner.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    fh.suffix = "%Y-%m-%d"
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt_file)
    scanner_log.addHandler(fh)

    # В консоль (только INFO+)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt_console)
    scanner_log.addHandler(ch)

    # ── Deals logger ──────────────────────────────────────────────────────
    # Пишет только в файл, сырые JSONL строки
    deals_log = logging.getLogger("deals")
    deals_log.setLevel(logging.INFO)
    deals_log.propagate = False  # не тянуть в корневой логгер

    dfh = logging.FileHandler(
        LOG_DIR / "deals.jsonl",
        mode="a",
        encoding="utf-8",
    )
    dfh.setLevel(logging.INFO)
    dfh.setFormatter(logging.Formatter("%(message)s"))  # только само сообщение
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

def gift_url(gift: dict) -> str:
    gid = gift.get("id", "")
    return f"https://t.me/mrkt?startapp=gift_{gid}" if gid else "https://t.me/mrkt"

SEPARATOR = "─" * 60

def log_deal_to_file(deal: dict) -> None:
    """Записывает сделку в deals.jsonl (один JSON объект на строку)."""
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
    """Записывает событие ошибки в logs/errors.jsonl."""
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

    def record_scan(self, new_gifts: list[dict], deals_count: int = 0) -> None:
        self.scans_completed += 1
        self.new_gifts_found += len(new_gifts)
        self.deals_found += deals_count
        for g in new_gifts:
            col = g.get("collectionName", "Unknown")
            self.collections_summary[col] = self.collections_summary.get(col, 0) + 1
        self.save()

    def record_error(self, err_type: str, msg: str = "", slot_label: str = "", endpoint: str = "") -> None:
        self.errors_count += 1
        self.errors_by_type[err_type] = self.errors_by_type.get(err_type, 0) + 1
        log_error_to_file(err_type, msg, slot_label, endpoint)
        self.save()

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
    """Исключение: просроченный или невалидный токен (HTTP 401)."""
    pass


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
    """
    Асинхронный HTTP запрос (GET/POST) через AsyncSession с обработкой 429 и отключением медленных прокси (>1.5с).
    """
    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES):
        slot = await pool.next_async()
        t0 = time.monotonic()

        log.debug(
            "API %s %s | слот: %s | попытка %d/%d",
            method.upper(), endpoint, slot.label, attempt + 1, MAX_RETRIES,
        )

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
                slot.record_timeout()
                if slot.disabled:
                    log.warning("⚠️ Слот [%s] ответил за %.2fс (>%.1fс) — прокси отключён как медленный", slot.label, elapsed, REQUEST_TIMEOUT)
                    print(f"\n⚠️  Прокси [{slot.label}] отключён (>1.5с, {elapsed:.1f}с)")

            if r.status_code == 401:
                log.error("HTTP 401 Unauthorized | Токен просрочен (слот: %s)", slot.label)
                stats_tracker.record_error("HTTP 401", f"Токен просрочен (слот: {slot.label})", slot.label, endpoint)
                last_exc = AuthTokenExpiredError(f"HTTP 401: Токен просрочен (слот: {slot.label})")
                continue

            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", PENALTY_429))
                log.warning(
                    "429 Too Many Requests | слот: %s | штраф: %.0fс | elapsed: %.2fс",
                    slot.label, retry_after, elapsed,
                )
                pool.penalize(slot, retry_after)
                stats_tracker.record_error("HTTP 429", f"Too Many Requests (penalty {retry_after}s)", slot.label, endpoint)
                last_exc = Exception(f"HTTP 429 (слот: {slot.label})")
                continue

            r.raise_for_status()
            log.debug(
                "API ответ: %d | elapsed: %.2fс | слот: %s",
                r.status_code, elapsed, slot.label,
            )
            return r.json()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            elapsed = time.monotonic() - t0
            err_name = type(e).__name__
            if "Timeout" in err_name or "timed out" in str(e).lower():
                slot.record_timeout()
                if slot.disabled:
                    log.warning("⚠️ Слот [%s] превысил таймаут %.1fс — прокси временно отключён", slot.label, REQUEST_TIMEOUT)
                    print(f"\n⚠️  Прокси [{slot.label}] отключён за таймаут (>1.5с)")
            if "429" in str(e):
                pool.penalize(slot, PENALTY_429)
                log.warning("429 (из исключения) | слот: %s | штраф: %.0fс", slot.label, PENALTY_429)
                stats_tracker.record_error("HTTP 429", str(e), slot.label, endpoint)
            else:
                log.warning(
                    "Ошибка запроса %s %s | слот: %s | elapsed: %.2fс | %s",
                    method.upper(), endpoint, slot.label, elapsed, e,
                )
                stats_tracker.record_error(err_name, str(e), slot.label, endpoint)
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(0.3)

    raise last_exc or RuntimeError(f"Все попытки исчерпаны: {method} {endpoint}")


async def api_post_async(endpoint: str, json_data: dict, pool: AccountPool, session: AsyncSession) -> dict:
    return await api_request_async("POST", endpoint, pool, session, json_data)


async def api_get_async(endpoint: str, pool: AccountPool, session: AsyncSession) -> Any:
    return await api_request_async("GET", endpoint, pool, session)


# ─────────────────────────────────────────────
#  Загрузка листингов (Async)
# ─────────────────────────────────────────────

async def fetch_page_async(cursor: str, pool: AccountPool, session: AsyncSession) -> dict:
    return await api_post_async("/gifts/saling", {
        "collectionNames": [],
        "modelNames": [],
        "backdropNames": [],
        "symbolNames": [],
        "ordering": "None",    # по времени, новые первые
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


async def fetch_new_listings_async(pool: AccountPool, seen_ids: set, first_run: bool, session: AsyncSession) -> list[dict]:
    new_gifts: list[dict] = []
    cursor = ""
    max_pages = 1 if first_run else 3

    for page_n in range(1, max_pages + 1):
        data = await fetch_page_async(cursor, pool, session)
        gifts = data.get("gifts", [])
        log.debug("Страница %d: получено %d подарков", page_n, len(gifts))

        if not gifts:
            break

        found_old = False
        for gift in gifts:
            gid = gift.get("id")
            if gid in seen_ids:
                found_old = True
                break
            new_gifts.append(gift)

        if found_old:
            log.debug("Страница %d: встретили виденный ID, останавливаемся", page_n)
            break

        cursor = data.get("cursor")
        if not cursor:
            break

    return new_gifts


async def fetch_black_floor_async(pool: AccountPool, session: AsyncSession) -> int | None:
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

    prices = sorted(
        int(g["salePrice"])
        for g in data.get("gifts", [])
        if g.get("salePrice")
    )
    floor = prices[1] if len(prices) >= 2 else (prices[0] if prices else None)
    log.debug("Флор чёрного фона: %s (из %d позиций)", tons_fmt(floor) if floor else "N/A", len(prices))
    return floor


async def fetch_all_model_floors_async(pool: AccountPool, session: AsyncSession) -> dict[str, int]:
    """
    1. GET /gifts/collections -> список всех доступных коллекций.
    2. Батчами (по <=10 коллекций): POST /gifts/models {"collections": [...]}.
    3. Возвращает карту {"CollectionName:ModelName": floorPriceNanoTons}.
    """
    model_floors: dict[str, int] = {}
    log.info("Загрузка списка коллекций для обновления флора моделей...")
    try:
        collections_data = await api_get_async("/gifts/collections", pool, session)
    except Exception as e:
        log.error("Не удалось получить список коллекций: %s", e)
        return model_floors

    if not isinstance(collections_data, list):
        log.error("Некорректный формат ответа /gifts/collections: %s", type(collections_data))
        return model_floors

    collection_names = [
        c.get("name")
        for c in collections_data
        if isinstance(c, dict) and c.get("name")
    ]
    log.info("Получено коллекций: %d. Запрашиваем флор моделей пачками по 10...", len(collection_names))

    # Лимит API: за раз можно указать не более 10 коллекций
    for i in range(0, len(collection_names), 10):
        batch = collection_names[i:i + 10]
        try:
            models_data = await api_post_async("/gifts/models", {"collections": batch}, pool, session)
            if isinstance(models_data, list):
                for item in models_data:
                    c_name = item.get("collectionName")
                    m_name = item.get("modelName")
                    fp = item.get("floorPriceNanoTons")
                    if c_name and m_name and fp is not None:
                        key = f"{c_name}:{m_name}"
                        model_floors[key] = int(fp)
            await asyncio.sleep(0.2)
        except Exception as e:
            log.warning("Ошибка при загрузке флоров моделей для батча %s: %s", batch, e)

    log.info("Флоры моделей обновлены: загружено %d моделей", len(model_floors))
    return model_floors


# ─────────────────────────────────────────────
#  Проверка сделок
# ─────────────────────────────────────────────

def check_gift(gift: dict, black_floor: int | None, model_floors: dict[str, int]) -> list[dict]:
    """
    Подарок считается ликвидным (покупаем) в трёх случаях:
    1. У него черный фон и он стоит на MIN_TON_DIFF меньше флора черного фона.
    2. Он стоит меньше CHEAP_PRICE_THRESHOLD (абсолютный дешевый порог).
    3. Он стоит на MIN_TON_DIFF дешевле флора этой конкретной модели.
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
    model_key = f"{collection_name}:{model_name}"

    # ── 1. Флор чёрного фона ───────────────────────────────────────────────
    if backdrop_name in BLACK_BACKDROPS and black_floor:
        diff_ton = tons(black_floor - price)
        if diff_ton >= MIN_TON_DIFF:
            pct = (black_floor - price) / black_floor * 100
            deals.append({
                "type": "BLACK",
                "gift": gift,
                "price": price,
                "floor": black_floor,
                "pct": pct,
                "diff_ton": diff_ton,
                "floor_src": "черный фон",
            })
            log.debug(
                "BLACK deal: %s %s #%s | %.2f TON < флор черного %.2f TON (выгода %.2f TON, %.1f%% скидка)",
                collection_name, model_name, gift.get("number"), price_ton, tons(black_floor), diff_ton, pct,
            )

    # ── 2. Абсолютный дешевый порог (< CHEAP_PRICE_THRESHOLD TON) ───────────
    if price_ton < CHEAP_PRICE_THRESHOLD:
        model_floor_val = model_floors.get(model_key, price)
        diff_ton = tons(model_floor_val - price) if model_floor_val > price else 0.0
        pct = (model_floor_val - price) / model_floor_val * 100 if model_floor_val > 0 else 0.0
        deals.append({
            "type": "CHEAP",
            "gift": gift,
            "price": price,
            "floor": model_floor_val,
            "pct": pct,
            "diff_ton": diff_ton,
            "floor_src": f"дешевле {CHEAP_PRICE_THRESHOLD:.2f} TON",
        })
        log.debug(
            "CHEAP deal: %s %s #%s | %.4f TON < %.2f TON порог",
            collection_name, model_name, gift.get("number"), price_ton, CHEAP_PRICE_THRESHOLD,
        )

    # ── 3. Флор конкретной модели ──────────────────────────────────────────
    model_floor = model_floors.get(model_key)
    if model_floor:
        diff_ton = tons(model_floor - price)
        if diff_ton >= MIN_TON_DIFF:
            pct = (model_floor - price) / model_floor * 100
            deals.append({
                "type": "MODEL",
                "gift": gift,
                "price": price,
                "floor": model_floor,
                "pct": pct,
                "diff_ton": diff_ton,
                "floor_src": f"модель {model_name}",
            })
            log.debug(
                "MODEL deal: %s %s #%s | %.2f TON < флор модели %.2f TON (выгода %.2f TON, %.1f%% скидка)",
                collection_name, model_name, gift.get("number"), price_ton, tons(model_floor), diff_ton, pct,
            )

    return deals


# ─────────────────────────────────────────────
#  Вывод в консоль + логирование
# ─────────────────────────────────────────────

def print_and_log_deal(deal: dict) -> None:
    gift = deal["gift"]
    _tags = {"BLACK": "🖤  ЧЁРНЫЙ ФОН", "CHEAP": f"💸  ДЁШЕВО (<{CHEAP_PRICE_THRESHOLD:.1f} TON)", "MODEL": "🎯  НИЖЕ ФЛОРА МОДЕЛИ"}
    tag = _tags.get(deal["type"], "🔥  ВЫГОДНАЯ СДЕЛКА")
    diff_ton = deal.get("diff_ton", tons(deal["floor"] - deal["price"]))

    lines = [
        SEPARATOR,
        f"  {tag}  —  выгода {diff_ton:.2f} TON (скидка {deal['pct']:.1f}%)",
        f"  📦  {gift.get('collectionName', '?')}  |  модель: {gift.get('modelName', '?')}",
        f"  🎨  Фон: {gift.get('backdropName', '?')}  |  узор: {gift.get('symbolName', '?')}  |  #{gift.get('number', '?')}",
        f"  💰  Цена: {tons_fmt(deal['price'])}  (флор: {tons_fmt(deal['floor'])} [{deal['floor_src']}])",
        f"  🔗  {gift_url(gift)}",
        "",
    ]
    output = "\n".join(lines)
    print(output)

    # Логируем в scanner.log
    log.info(
        "DEAL [%s] выгода %.2f TON (скидка %.1f%%) | %s %s #%s | %.2f TON → флор %.2f TON | %s",
        deal["type"], diff_ton, deal["pct"],
        gift.get("collectionName"), gift.get("modelName"), gift.get("number"),
        tons(deal["price"]), tons(deal["floor"]),
        gift_url(gift),
    )

    # Записываем в deals.jsonl
    log_deal_to_file(deal)


# ─────────────────────────────────────────────
#  Основной цикл
# ─────────────────────────────────────────────

_shutdown = asyncio.Event()

def _handle_signal(sig, frame):
    log.info("Получен сигнал %s, завершаем...", signal.Signals(sig).name)
    _shutdown.set()

# Graceful shutdown на Linux (SIGTERM от systemd/kill)
signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


async def main() -> None:
    startup_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header_lines = [
        "=" * 60,
        f"  🚀  MRKT Gift Scanner (Async)  (старт: {startup_ts})",
        f"  Порог выгоды:  {MIN_TON_DIFF:.2f} TON  |  дёшево < {CHEAP_PRICE_THRESHOLD:.2f} TON",
        f"  Интервал:      {SCAN_INTERVAL:.2f} сек  |  таймаут прокси: {REQUEST_TIMEOUT:.1f} сек",
        f"  Флор моделей:  обновление каждые {MODEL_FLOOR_REFRESH_HOURS:.1f} ч",
        f"  Штраф 429:     {PENALTY_429:.0f} сек",
        f"  Чёрные фоны:   {', '.join(BLACK_BACKDROPS)}",
        f"  Логи:          {LOG_DIR.resolve()}",
        "─" * 60,
        "  Загрузка аккаунтов и прокси...",
    ]
    for line in header_lines:
        print(line)
    log.info("="*50)
    log.info("Запуск MRKT Scanner (Async) | порог выгоды: %.2f TON | интервал: %.2fs", MIN_TON_DIFF, SCAN_INTERVAL)

    try:
        pool = build_pool()
    except RuntimeError as e:
        log.critical("Ошибка инициализации пула: %s", e)
        print(f"\n❌  {e}")
        sys.exit(1)

    print(f"  {pool}")
    log.info("Пул: %s", pool)
    print("=" * 60)

    # Настройка моментального завершения по Ctrl+C на уровне asyncio
    loop = asyncio.get_running_loop()
    def _sig_handler():
        log.info("Получен сигнал завершения, останавливаем...")
        _shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig_handler)
        except (NotImplementedError, RuntimeError):
            pass

    seen_ids: set = set()
    black_floor: int | None = None
    model_floors: dict[str, int] = {}
    last_model_refresh: float = 0.0
    scan_count = 0
    total_deals = 0
    consecutive_401_errors = 0
    first_run = True

    try:
        async with AsyncSession() as session:
            while not _shutdown.is_set():
                scan_count += 1
                ts = now_str()

                # ── Флор моделей (каждые 12 часов) ──────────────────────────────
                if not model_floors or (time.monotonic() - last_model_refresh >= MODEL_FLOOR_REFRESH_SECS):
                    log.info("Обновляем базу флоров моделей...")
                    print(f"[{ts}] 🔄 Обновление флоров моделей (раз в {MODEL_FLOOR_REFRESH_HOURS:.0f}ч)...", end=" ", flush=True)
                    try:
                        mf = await fetch_all_model_floors_async(pool, session)
                        if mf:
                            model_floors = mf
                            last_model_refresh = time.monotonic()
                            print(f"загружено {len(model_floors)} моделей")
                        else:
                            print("не удалось обновить (используем кэш)")
                    except Exception as e:
                        log.error("Не удалось обновить флор моделей: %s", e)
                        print(f"ошибка: {e}")

                # ── Флор чёрного фона ────────────────────────────────────────────
                if black_floor is None or scan_count % BLACK_FLOOR_REFRESH == 0:
                    log.debug("Обновляем флор чёрного фона...")
                    try:
                        bf = await fetch_black_floor_async(pool, session)
                        if bf:
                            black_floor = bf
                            log.info("Флор чёрного фона: %s", tons_fmt(black_floor))
                            print(f"[{ts}] 🖤 Флор чёрного фона: {tons_fmt(black_floor)}")
                        else:
                            log.warning("Флор чёрного фона не получен (нет Black листингов?)")
                    except Exception as e:
                        log.error("Не удалось получить флор чёрного фона: %s", e)
                        print(f"[{ts}] ⚠️  Флор чёрного: {e}")

                # ── Скан ─────────────────────────────────────────────────────────
                label = " (первый скан)" if first_run else ""
                log.debug("Скан #%d начат%s", scan_count, label)
                print(f"[{ts}] ⟳ Скан #{scan_count}{label}...", end=" ", flush=True)

                try:
                    t_scan = time.monotonic()
                    new_gifts = await fetch_new_listings_async(pool, seen_ids, first_run, session)
                    elapsed = time.monotonic() - t_scan
                    consecutive_401_errors = 0  # Скан успешен — сбрасываем счётчик ошибки 401

                    for g in new_gifts:
                        seen_ids.add(g.get("id"))

                    scan_deals_count = 0
                    if not first_run and new_gifts:
                        scan_deals: list[dict] = []
                        for gift in new_gifts:
                            scan_deals.extend(check_gift(gift, black_floor, model_floors))

                        if scan_deals:
                            scan_deals_count = len(scan_deals)
                            total_deals += scan_deals_count
                            print(f"\n[{ts}]  ✅ {len(scan_deals)} предложений! (сессия: {total_deals})\n")
                            log.info("!!! Найдено %d сделок (сессия: %d)", len(scan_deals), total_deals)
                            for deal in scan_deals:
                                print_and_log_deal(deal)

                    stats_tracker.record_scan(new_gifts, scan_deals_count)

                    print(f"+{len(new_gifts)} новых  |  в базе: {len(seen_ids)}  |  {elapsed:.1f}с")
                    log.info(
                        "Скан #%d: +%d новых | всего в базе: %d | %.1fс",
                        scan_count, len(new_gifts), len(seen_ids), elapsed,
                    )

                except AuthTokenExpiredError as e:
                    consecutive_401_errors += 1
                    print(f"\n[{ts}] ❌ {e}")
                    log.error("Просроченный токен (скан #%d) [%d/3 попыток]", scan_count, consecutive_401_errors)
                    if consecutive_401_errors >= 3:
                        log.critical("❌ Все токены в пуле просрочены (HTTP 401). Завершение работы сканера во избежание нагрузки.")
                        print(f"\n🛑 ОСТАНОВКА: Все токены просрочены (HTTP 401). Сканер автоматически завершил работу во избежание бессмысленных запросов.")
                        break
                except Exception as e:
                    print(f"\n[{ts}] ❌ {e}")
                    log.error("Ошибка в скане #%d: %s", scan_count, e, exc_info=True)
                    stats_tracker.record_error(type(e).__name__, str(e))

                first_run = False

                # Ждём следующего скана (с поддержкой shutdown)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(asyncio.ensure_future(_shutdown.wait())),
                        timeout=SCAN_INTERVAL,
                    )
                    break  # shutdown получен
                except asyncio.TimeoutError:
                    pass  # нормально, просто истёк интервал

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n👋 Сканирование остановлено пользователем (Ctrl+C).")
        log.info("Остановка пользователем (Ctrl+C).")
    finally:
        log.info("Остановка: сканов: %d, сделок: %d", scan_count, total_deals)
        print(f"\n👋 Остановлено. Сканов: {scan_count}, сделок: {total_deals}")
        pool.stop_all_proxies()


if __name__ == "__main__":
    asyncio.run(main())
