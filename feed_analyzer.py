"""
feed_analyzer.py — Модуль глубокого анализа истории ленты MRKT (/feed).
========================================================================
Находит и фиксирует все выкупы подарков, совершенные в течение <= 2 секунд
после их выставления или изменения цены (быстрые выкупы снайперами/ботами).

Записывает:
  - Время выставления (listed_at)
  - Время выкупа (sold_at)
  - Скорость выкупа в миллисекундах (duration_ms) и секундах (duration_sec)
  - Цену в TON и нанотонах
  - Коллекцию, модель, фон, номер, ID
  - Прикладные рыночные данные (флор коллекции, флор черного фона, выгода/скидка в %, оборот, редкость)
  - Ссылку на NFT

Особенности:
  - Многоаккаунтный режим с распределением нагрузки по пулу слотов (LRU + прокси).
  - Защита от 429 с автоматическим пенальти и сменой слотов.
  - Потоковая запись в JSONL и CSV (данные сохраняются на лету).
  - Приостановка основного сканера на время анализа.
  - Поддержка отмены в любой момент с сохранением собранных результатов.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from curl_cffi.requests import AsyncSession

log = logging.getLogger("scanner")

FEED_API_URL = "https://api.tgmrkt.io/api/v1/feed"
DEFAULT_PAGE_SIZE = 20
PAGE_PACING_DELAY = 0.35  # Задержка между запросами страниц для безопасности аккаунтов


@dataclass
class FastBuyRecord:
    gift_id: str
    number: int
    collection: str
    model: str
    backdrop: str
    listed_at: str
    sold_at: str
    duration_ms: int
    duration_sec: float
    price_ton: float
    price_nano: int
    collection_floor_ton: Optional[float]
    diff_to_floor_ton: Optional[float]
    discount_pct: Optional[float]
    black_floor_ton: Optional[float]
    is_black: bool
    is_rare_number: bool
    collection_volume_ton: Optional[float]
    turnover_ratio: Optional[float]
    nft_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "gift_id": self.gift_id,
            "number": self.number,
            "collection": self.collection,
            "model": self.model,
            "backdrop": self.backdrop,
            "listed_at": self.listed_at,
            "sold_at": self.sold_at,
            "duration_ms": self.duration_ms,
            "duration_sec": self.duration_sec,
            "price_ton": self.price_ton,
            "price_nano": self.price_nano,
            "market_data": {
                "collection_floor_ton": self.collection_floor_ton,
                "diff_to_floor_ton": self.diff_to_floor_ton,
                "discount_pct": self.discount_pct,
                "black_floor_ton": self.black_floor_ton,
                "is_black": self.is_black,
                "is_rare_number": self.is_rare_number,
                "collection_volume_ton": self.collection_volume_ton,
                "turnover_ratio": self.turnover_ratio,
            },
            "nft_url": self.nft_url,
        }

    def to_csv_row(self) -> dict[str, Any]:
        return {
            "gift_id": self.gift_id,
            "number": self.number,
            "collection": self.collection,
            "model": self.model,
            "backdrop": self.backdrop,
            "listed_at": self.listed_at,
            "sold_at": self.sold_at,
            "duration_ms": self.duration_ms,
            "duration_sec": self.duration_sec,
            "price_ton": self.price_ton,
            "price_nano": self.price_nano,
            "collection_floor_ton": self.collection_floor_ton or "",
            "diff_to_floor_ton": self.diff_to_floor_ton or "",
            "discount_pct": self.discount_pct or "",
            "black_floor_ton": self.black_floor_ton or "",
            "is_black": self.is_black,
            "is_rare_number": self.is_rare_number,
            "collection_volume_ton": self.collection_volume_ton or "",
            "turnover_ratio": self.turnover_ratio or "",
            "nft_url": self.nft_url,
        }


CSV_FIELDNAMES = [
    "gift_id",
    "number",
    "collection",
    "model",
    "backdrop",
    "listed_at",
    "sold_at",
    "duration_ms",
    "duration_sec",
    "price_ton",
    "price_nano",
    "collection_floor_ton",
    "diff_to_floor_ton",
    "discount_pct",
    "black_floor_ton",
    "is_black",
    "is_rare_number",
    "collection_volume_ton",
    "turnover_ratio",
    "nft_url",
]


@dataclass
class AnalysisResult:
    total_pages: int
    total_events: int
    matched_buys: list[FastBuyRecord]
    json_path: Path
    jsonl_path: Path
    csv_path: Path
    elapsed_sec: float
    cancelled: bool = False
    error: Optional[str] = None


def make_nft_url(collection_name: str, number: Any) -> str:
    if not collection_name or not number:
        return "https://t.me/nft"
    slug = "".join(c for c in collection_name if c.isalnum())
    return f"https://t.me/nft/{slug}-{number}"


class FeedAnalyzer:
    """
    Класс для управления процессом извлечения и анализа быстрых выкупов из ленты MRKT.
    """

    def __init__(
        self,
        pool: Any,
        scanner_state: Any = None,
        log_dir: Path = Path("logs"),
    ):
        self.pool = pool
        self.scanner_state = scanner_state
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

    async def analyze_history(
        self,
        max_pages: int = 100,
        threshold_ms: int = 2000,
        on_progress: Optional[Callable[[int, int, int, int], None]] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AnalysisResult:
        """
        Запускает анализ указанного количества страниц истории ленты.
        Приостанавливает сканер на время работы.
        """
        start_time = time.monotonic()
        was_paused = getattr(self.scanner_state, "is_paused", False)

        if self.scanner_state:
            self.scanner_state.is_paused = True
            self.scanner_state.is_analyzing_feed = True

        log.info(
            "🚀 Старт анализа истории ленты (/feed): страниц=%d, порог=%.2fс",
            max_pages,
            threshold_ms / 1000,
        )

        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = self.log_dir / f"fast_buys_{timestamp_str}.json"
        jsonl_path = self.log_dir / f"fast_buys_{timestamp_str}.jsonl"
        csv_path = self.log_dir / f"fast_buys_{timestamp_str}.csv"
        cumulative_path = self.log_dir / "fast_buys_history.jsonl"

        # Инициализируем CSV файл с заголовками
        with open(csv_path, "w", encoding="utf-8", newline="") as cf:
            writer = csv.DictWriter(cf, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()

        cursor: Optional[str] = None
        total_events = 0
        pages_processed = 0
        matched_records: list[FastBuyRecord] = []
        recorded_keys: set[tuple[str, str]] = set()  # (gift_id, sale_id)
        events_by_gift: dict[str, list[dict[str, Any]]] = {}

        cancelled = False
        error_msg: Optional[str] = None

        # Кэш флоров и рынка из scanner_state
        col_floors = getattr(self.scanner_state, "collection_floors", {}) or {}
        black_floor_nano = getattr(self.scanner_state, "black_floor_nano", None)
        col_volumes = getattr(self.scanner_state, "collection_volumes", {}) or {}

        try:
            for page in range(1, max_pages + 1):
                if cancel_event and cancel_event.is_set():
                    log.info("Анализ ленты прерван пользователем на странице %d", page)
                    cancelled = True
                    break

                slot = await self.pool.next_async()
                payload: dict[str, Any] = {
                    "count": DEFAULT_PAGE_SIZE,
                    "type": ["sale", "listing", "change_price"],
                }
                if cursor:
                    payload["cursor"] = cursor

                page_items = []
                retry_attempts = 3
                for attempt in range(retry_attempts):
                    if cancel_event and cancel_event.is_set():
                        break
                    try:
                        proxies = slot.proxies
                        headers = slot.headers
                        async with AsyncSession(
                            impersonate="chrome124", proxies=proxies
                        ) as session:
                            r = await session.post(
                                FEED_API_URL,
                                headers=headers,
                                json=payload,
                                timeout=7.0,
                                discard_cookies=True,
                            )

                        if r.status_code == 200:
                            slot.record_success()
                            data = r.json()
                            if isinstance(data, dict):
                                page_items = data.get("items", [])
                                cursor = data.get("cursor")
                            break

                        if r.status_code == 429:
                            slot.penalize(15.0)
                            log.warning(
                                "FeedAnalyzer: 429 на слоте [%s], переключаем слот...",
                                slot.token[:8],
                            )
                            slot = await self.pool.next_async()
                            await asyncio.sleep(0.6)
                            continue

                        log.warning(
                            "FeedAnalyzer: статус %s на странице %d (попытка %d)",
                            r.status_code,
                            page,
                            attempt + 1,
                        )
                    except Exception as exc:
                        slot.record_timeout()
                        log.debug(
                            "FeedAnalyzer: сетевая ошибка на слоте [%s]: %s",
                            slot.token[:8],
                            exc,
                        )
                        slot = await self.pool.next_async()
                        await asyncio.sleep(0.5)

                if not page_items:
                    log.info(
                        "FeedAnalyzer: лента пуста или достигнут конец истории на странице %d",
                        page,
                    )
                    break

                pages_processed = page
                total_events += len(page_items)

                # Обработка событий страницы
                for item in page_items:
                    itype = item.get("type")
                    gift = item.get("gift") or {}
                    gid = gift.get("id")
                    if not gid or gift.get("luckyBuy") or itype not in ("sale", "listing", "change_price"):
                        continue

                    # Парсим дату
                    date_str = item.get("date")
                    if not date_str:
                        continue
                    try:
                        item_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        item["_dt"] = item_dt
                    except Exception:
                        continue

                    if gid not in events_by_gift:
                        events_by_gift[gid] = []
                    events_by_gift[gid].append(item)

                    # Проверяем, есть ли завершенная пара для этого подарка
                    gift_events = sorted(events_by_gift[gid], key=lambda x: x["_dt"])
                    sales = [ev for ev in gift_events if ev.get("type") == "sale"]
                    listings = [ev for ev in gift_events if ev.get("type") in ("listing", "change_price")]

                    for s in sales:
                        sale_id = s.get("id", "")
                        if (gid, sale_id) in recorded_keys:
                            continue

                        sale_dt: datetime = s["_dt"]
                        # Ищем листинг, который предшествовал этой продаже
                        prev_listings = [l for l in listings if l["_dt"] <= sale_dt]
                        if not prev_listings:
                            continue

                        best_listing = prev_listings[-1]
                        list_dt: datetime = best_listing["_dt"]
                        delta_ms = int((sale_dt - list_dt).total_seconds() * 1000)

                        if 0 <= delta_ms <= threshold_ms:
                            recorded_keys.add((gid, sale_id))

                            amount_nano = int(s.get("amount") or gift.get("salePrice") or 0)
                            price_ton = round(amount_nano / 1e9, 2)

                            col_name = (
                                gift.get("collectionName")
                                or gift.get("collectionTitle")
                                or gift.get("title")
                                or "NFT"
                            )
                            mod_name = gift.get("modelName") or gift.get("modelTitle") or ""
                            bd_name = gift.get("backdropName") or ""
                            num_val = int(gift.get("number") or 0)

                            # Рыночные данные
                            col_floor = col_floors.get(col_name)
                            col_floor_ton = round(col_floor / 1e9, 2) if col_floor else None
                            diff_to_floor_ton = (
                                round(col_floor_ton - price_ton, 2) if col_floor_ton else None
                            )
                            discount_pct = (
                                round(
                                    ((col_floor_ton - price_ton) / col_floor_ton) * 100, 1
                                )
                                if (col_floor_ton and col_floor_ton > 0)
                                else None
                            )

                            bf_ton = (
                                round(black_floor_nano / 1e9, 2) if black_floor_nano else None
                            )
                            is_black = bd_name.lower().strip() == "black"
                            is_rare_num = 1 <= num_val <= 100 or str(num_val) in (
                                "777",
                                "888",
                                "999",
                                "111",
                                "222",
                                "333",
                                "444",
                                "555",
                                "666",
                            )

                            vol_nano = col_volumes.get(col_name)
                            vol_ton = round(vol_nano / 1e9, 1) if vol_nano else None
                            turnover_ratio = (
                                round(vol_ton / price_ton, 1)
                                if (vol_ton and price_ton > 0)
                                else None
                            )

                            rec = FastBuyRecord(
                                gift_id=gid,
                                number=num_val,
                                collection=col_name,
                                model=mod_name,
                                backdrop=bd_name,
                                listed_at=best_listing.get("date", ""),
                                sold_at=s.get("date", ""),
                                duration_ms=delta_ms,
                                duration_sec=round(delta_ms / 1000, 2),
                                price_ton=price_ton,
                                price_nano=amount_nano,
                                collection_floor_ton=col_floor_ton,
                                diff_to_floor_ton=diff_to_floor_ton,
                                discount_pct=discount_pct,
                                black_floor_ton=bf_ton,
                                is_black=is_black,
                                is_rare_number=is_rare_num,
                                collection_volume_ton=vol_ton,
                                turnover_ratio=turnover_ratio,
                                nft_url=make_nft_url(col_name, num_val),
                            )

                            matched_records.append(rec)

                            # Потоковая запись в JSONL
                            json_line = json.dumps(rec.to_dict(), ensure_ascii=False)
                            with open(jsonl_path, "a", encoding="utf-8") as jf:
                                jf.write(json_line + "\n")
                            with open(cumulative_path, "a", encoding="utf-8") as cumf:
                                cumf.write(json_line + "\n")

                            # Потоковая запись в CSV
                            with open(csv_path, "a", encoding="utf-8", newline="") as cf:
                                writer = csv.DictWriter(cf, fieldnames=CSV_FIELDNAMES)
                                writer.writerow(rec.to_csv_row())

                            log.info(
                                "⚡ [FAST BUY] %s #%d | %.2f TON за %d мс (скидка: %s%%) | %s",
                                col_name,
                                num_val,
                                price_ton,
                                delta_ms,
                                f"{discount_pct:+.0f}" if discount_pct is not None else "N/A",
                                rec.nft_url,
                            )

                # Очистка памяти: держим не более 3000 активных подарков в словаре
                if len(events_by_gift) > 3000:
                    oldest_keys = list(events_by_gift.keys())[:1000]
                    for k in oldest_keys:
                        events_by_gift.pop(k, None)

                # Коллбэк прогресса
                if on_progress:
                    try:
                        on_progress(
                            pages_processed,
                            max_pages,
                            total_events,
                            len(matched_records),
                        )
                    except Exception:
                        pass

                if not cursor:
                    log.info("FeedAnalyzer: курсор закончился на странице %d", page)
                    break

                # Тактическая задержка между страницами
                await asyncio.sleep(PAGE_PACING_DELAY)

        except Exception as exc:
            error_msg = str(exc)
            log.error("FeedAnalyzer: критическая ошибка: %s", exc, exc_info=True)
        finally:
            elapsed_sec = round(time.monotonic() - start_time, 2)
            if self.scanner_state:
                self.scanner_state.is_analyzing_feed = False
                self.scanner_state.is_paused = was_paused

            # Сохраняем полный форматированный JSON со всеми данными
            try:
                full_json_data = {
                    "meta": {
                        "generated_at": datetime.utcnow().isoformat() + "Z",
                        "total_pages_scanned": pages_processed,
                        "total_events_scanned": total_events,
                        "fast_buys_count": len(matched_records),
                        "threshold_ms": threshold_ms,
                        "elapsed_seconds": elapsed_sec,
                        "cancelled_early": cancelled,
                    },
                    "items": [r.to_dict() for r in matched_records],
                }
                with open(json_path, "w", encoding="utf-8") as jf:
                    json.dump(full_json_data, jf, ensure_ascii=False, indent=2)
            except Exception as e:
                log.warning("Не удалось сохранить итоговый JSON файл: %s", e)

            log.info(
                "🏁 Анализ ленты завершён: страниц=%d, событий=%d, быстрых выкупов=%d (за %.1fс)",
                pages_processed,
                total_events,
                len(matched_records),
                elapsed_sec,
            )

        return AnalysisResult(
            total_pages=pages_processed,
            total_events=total_events,
            matched_buys=matched_records,
            json_path=json_path,
            jsonl_path=jsonl_path,
            csv_path=csv_path,
            elapsed_sec=elapsed_sec,
            cancelled=cancelled,
            error=error_msg,
        )


# ─────────────────────────────────────────────
#  CLI запуск
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    from account_pool import build_pool_async

    parser = argparse.ArgumentParser(description="Анализатор быстрых выкупов в ленте MRKT")
    parser.add_argument("--pages", type=int, default=100, help="Количество страниц для анализа (по 20 событий)")
    parser.add_argument("--threshold", type=int, default=2000, help="Порог скорости выкупа в мс (по умолчанию 2000 мс)")
    args = parser.parse_args()

    async def main_cli():
        print(f"🚀 Запуск анализатора ленты: {args.pages} страниц, порог {args.threshold} мс...")
        pool = await build_pool_async()
        analyzer = FeedAnalyzer(pool=pool)

        def progress(page, total_p, events, found):
            pct = int(page / total_p * 100) if total_p > 0 else 0
            print(f"\r[Прогресс: {pct}%] Стр: {page}/{total_p} | Событий: {events} | Быстрых выкупов: {found}", end="", flush=True)

        res = await analyzer.analyze_history(max_pages=args.pages, threshold_ms=args.threshold, on_progress=progress)
        print("\n\n" + "=" * 60)
        print(f"✅ Анализ завершён за {res.elapsed_sec:.1f} сек!")
        print(f"📄 Проверено страниц: {res.total_pages}")
        print(f"📦 Всего событий: {res.total_events}")
        print(f"⚡ Найдено выкупов <= {args.threshold} мс: {len(res.matched_buys)}")
        print(f"💾 JSONL: {res.jsonl_path}")
        print(f"📊 CSV:   {res.csv_path}")
        if res.matched_buys:
            print("\n🏆 Топ-5 самых быстрых выкупов:")
            for i, r in enumerate(sorted(res.matched_buys, key=lambda x: x.duration_ms)[:5], 1):
                print(f"  {i}. {r.collection} #{r.number} | {r.duration_ms} мс | {r.price_ton} TON | {r.nft_url}")
        print("=" * 60)

    asyncio.run(main_cli())
