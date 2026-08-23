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
from dotenv import load_dotenv

from account_pool import AccountPool, Slot, build_pool

load_dotenv()

# ─────────────────────────────────────────────
#  Конфиг
# ─────────────────────────────────────────────

MARKET_API_URL      = "https://api.tgmrkt.io/api/v1"
SCAN_INTERVAL       = int(os.getenv("SCAN_INTERVAL", 30))
DISCOUNT_THRESHOLD  = float(os.getenv("DISCOUNT_THRESHOLD", 20)) / 100
BLACK_FLOOR_REFRESH = int(os.getenv("BLACK_FLOOR_REFRESH", 10))
MAX_RETRIES         = int(os.getenv("MAX_RETRIES", 3))
PENALTY_429         = float(os.getenv("PENALTY_429", 60.0))
LOG_DIR             = Path(os.getenv("LOG_DIR", "logs"))

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
        "discount_pct": round(deal["pct"], 2),
        "floor_src": deal.get("floor_src", ""),
        "url": gift_url(gift),
        "gift_id": gift.get("id", ""),
    }
    deals_log.info(json.dumps(row, ensure_ascii=False))


# ─────────────────────────────────────────────
#  API с retry и логированием
# ─────────────────────────────────────────────

def api_post(endpoint: str, json_data: dict, pool: AccountPool) -> dict:
    """
    POST запрос с автоматическим retry и обработкой 429.
    При 429: штрафуем текущий слот и пробуем следующий.
    """
    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES):
        slot = pool.next()
        t0 = time.monotonic()

        log.debug(
            "API POST %s | слот: %s | попытка %d/%d",
            endpoint, slot.label, attempt + 1, MAX_RETRIES,
        )

        try:
            r = cffi_requests.post(
                f"{MARKET_API_URL}{endpoint}",
                headers=slot.headers,
                json=json_data,
                proxies=slot.proxies,
                timeout=15,
            )
            elapsed = time.monotonic() - t0

            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", PENALTY_429))
                log.warning(
                    "429 Too Many Requests | слот: %s | штраф: %.0fс | elapsed: %.2fс",
                    slot.label, retry_after, elapsed,
                )
                pool.penalize(slot, retry_after)
                last_exc = Exception(f"HTTP 429 (слот: {slot.label})")
                continue

            r.raise_for_status()
            log.debug(
                "API ответ: %d | elapsed: %.2fс | слот: %s",
                r.status_code, elapsed, slot.label,
            )
            return r.json()

        except Exception as e:
            elapsed = time.monotonic() - t0
            if "429" in str(e):
                pool.penalize(slot, PENALTY_429)
                log.warning("429 (из исключения) | слот: %s | штраф: %.0fс", slot.label, PENALTY_429)
            else:
                log.warning(
                    "Ошибка запроса %s | слот: %s | elapsed: %.2fс | %s",
                    endpoint, slot.label, elapsed, e,
                )
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(1)

    raise last_exc or RuntimeError("Все попытки исчерпаны")


# ─────────────────────────────────────────────
#  Загрузка листингов
# ─────────────────────────────────────────────

def fetch_page(cursor: str, pool: AccountPool) -> dict:
    return api_post("/gifts/saling", {
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
    }, pool)


def fetch_new_listings(pool: AccountPool, seen_ids: set, first_run: bool) -> list[dict]:
    """
    Загружает новые листинги.
    Первый запуск: 1 страница, только помечаем (не проверяем).
    Последующие: 1-3 страницы, стоп при первом виденном ID.
    """
    new_gifts: list[dict] = []
    cursor = ""
    max_pages = 1 if first_run else 3

    for page_n in range(1, max_pages + 1):
        data = fetch_page(cursor, pool)
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


def fetch_black_floor(pool: AccountPool) -> int | None:
    """Флор чёрного фона (2-я по цене позиция среди Black листингов)."""
    data = api_post("/gifts/saling", {
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
    }, pool)

    prices = sorted(
        int(g["salePrice"])
        for g in data.get("gifts", [])
        if g.get("salePrice")
    )
    floor = prices[1] if len(prices) >= 2 else (prices[0] if prices else None)
    log.debug("Флор чёрного фона: %s (из %d позиций)", tons_fmt(floor) if floor else "N/A", len(prices))
    return floor


# ─────────────────────────────────────────────
#  Проверка сделок
# ─────────────────────────────────────────────

def check_gift(gift: dict, black_floor: int | None) -> list[dict]:
    deals: list[dict] = []
    price = gift.get("salePrice")
    if not price:
        return deals
    price = int(price)

    # ── Флор модели ───────────────────────────────────────────────────────
    model_floor = (
        gift.get("floorPriceNanoTONsByBackdropModel")
        or gift.get("floorPriceNanoTONsByCollection")
    )
    if model_floor:
        model_floor = int(model_floor)
        if price < model_floor * (1 - DISCOUNT_THRESHOLD):
            pct = (model_floor - price) / model_floor * 100
            src = "backdrop+model" if gift.get("floorPriceNanoTONsByBackdropModel") else "collection"
            deals.append({
                "type": "MODEL",
                "gift": gift,
                "price": price,
                "floor": model_floor,
                "pct": pct,
                "floor_src": src,
            })
            log.debug(
                "MODEL deal: %s %s #%s | %.2f TON < флор %.2f TON (%.1f%% скидка)",
                gift.get("collectionName"), gift.get("modelName"),
                gift.get("number"), tons(price), tons(model_floor), pct,
            )

    # ── Флор чёрного фона ─────────────────────────────────────────────────
    if gift.get("backdropName") in BLACK_BACKDROPS and black_floor:
        if price < black_floor * (1 - DISCOUNT_THRESHOLD):
            pct = (black_floor - price) / black_floor * 100
            deals.append({
                "type": "BLACK",
                "gift": gift,
                "price": price,
                "floor": black_floor,
                "pct": pct,
                "floor_src": "black_market",
            })
            log.debug(
                "BLACK deal: %s %s #%s | %.2f TON < флор %.2f TON (%.1f%% скидка)",
                gift.get("collectionName"), gift.get("modelName"),
                gift.get("number"), tons(price), tons(black_floor), pct,
            )

    return deals


# ─────────────────────────────────────────────
#  Вывод в консоль + логирование
# ─────────────────────────────────────────────

def print_and_log_deal(deal: dict) -> None:
    gift = deal["gift"]
    tag = "🖤  ЧЁРНЫЙ ФОН" if deal["type"] == "BLACK" else "🔥  ДЕШЁВАЯ МОДЕЛЬ"

    lines = [
        SEPARATOR,
        f"  {tag}  —  скидка {deal['pct']:.1f}%",
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
        "DEAL [%s] скидка %.1f%% | %s %s #%s | %.2f TON → флор %.2f TON | %s",
        deal["type"], deal["pct"],
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
        f"  🚀  MRKT Gift Scanner  (старт: {startup_ts})",
        f"  Порог скидки:  {DISCOUNT_THRESHOLD * 100:.0f}%",
        f"  Интервал:      {SCAN_INTERVAL} сек",
        f"  Штраф 429:     {PENALTY_429:.0f} сек",
        f"  Чёрные фоны:   {', '.join(BLACK_BACKDROPS)}",
        f"  Логи:          {LOG_DIR.resolve()}",
        "─" * 60,
        "  Загрузка аккаунтов и прокси...",
    ]
    for line in header_lines:
        print(line)
    log.info("="*50)
    log.info("Запуск MRKT Scanner | порог: %.0f%% | интервал: %ds", DISCOUNT_THRESHOLD * 100, SCAN_INTERVAL)

    try:
        pool = build_pool()
    except RuntimeError as e:
        log.critical("Ошибка инициализации пула: %s", e)
        print(f"\n❌  {e}")
        sys.exit(1)

    print(f"  {pool}")
    log.info("Пул: %s", pool)
    print("=" * 60)

    seen_ids: set = set()
    black_floor: int | None = None
    scan_count = 0
    total_deals = 0
    first_run = True

    try:
        while not _shutdown.is_set():
            scan_count += 1
            ts = now_str()

            # ── Флор чёрного фона ────────────────────────────────────────────
            if black_floor is None or scan_count % BLACK_FLOOR_REFRESH == 0:
                log.debug("Обновляем флор чёрного фона...")
                try:
                    bf = fetch_black_floor(pool)
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
                new_gifts = fetch_new_listings(pool, seen_ids, first_run)
                elapsed = time.monotonic() - t_scan

                for g in new_gifts:
                    seen_ids.add(g.get("id"))

                print(f"+{len(new_gifts)} новых  |  в базе: {len(seen_ids)}  |  {elapsed:.1f}с")
                log.info(
                    "Скан #%d: +%d новых | всего в базе: %d | %.1fс",
                    scan_count, len(new_gifts), len(seen_ids), elapsed,
                )

                if not first_run and new_gifts:
                    scan_deals: list[dict] = []
                    for gift in new_gifts:
                        scan_deals.extend(check_gift(gift, black_floor))

                    if scan_deals:
                        total_deals += len(scan_deals)
                        print(f"\n[{ts}]  ✅ {len(scan_deals)} предложений! (сессия: {total_deals})\n")
                        log.info("!!! Найдено %d сделок (сессия: %d)", len(scan_deals), total_deals)
                        for deal in scan_deals:
                            print_and_log_deal(deal)

            except Exception as e:
                print(f"\n[{ts}] ❌ {e}")
                log.error("Ошибка в скане #%d: %s", scan_count, e, exc_info=True)

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

    finally:
        log.info("Остановка: сканов: %d, сделок: %d", scan_count, total_deals)
        print(f"\n👋 Остановлено. Сканов: {scan_count}, сделок: {total_deals}")
        pool.stop_all_proxies()


if __name__ == "__main__":
    asyncio.run(main())
