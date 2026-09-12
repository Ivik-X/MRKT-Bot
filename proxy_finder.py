"""
proxy_finder.py — Автопоиск, замер пинга и авто-пополнение пула прокси из Proxifly.

Источники (обновляются каждые 5 минут):
  - https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.json
  - https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks5/data.txt
  - https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from curl_cffi.requests import AsyncSession

from xray_proxy import SimpleProxy

log = logging.getLogger("scanner")

PROXIFLY_JSON_URL = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.json"
PROXIFLY_SOCKS5_URL = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks5/data.txt"
PROXIFLY_HTTPS_URL = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt"
TEST_ENDPOINT = "https://api.tgmrkt.io/api/v1/gifts/collections"

_replenish_lock = asyncio.Lock()


async def fetch_candidates(limit: int = 600) -> list[tuple[str, str]]:
    """
    Загружает список кандидатов из Proxifly.
    Возвращает [(proxy_url, display_name), ...]
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    # 1. Пробуем получить полный JSON с метаданными стран и протоколов
    try:
        async with AsyncSession(impersonate="chrome124", verify=False) as s:
            resp = await s.get(PROXIFLY_JSON_URL, timeout=8.0)
            if resp.status_code == 200:
                data = resp.json()
                for item in data:
                    proto = item.get("protocol")
                    ip = item.get("ip")
                    port = item.get("port")
                    if not ip or not port:
                        continue
                    key = f"{ip}:{port}"
                    if key in seen:
                        continue
                    seen.add(key)
                    country = item.get("geolocation", {}).get("country", "XX")
                    if proto == "socks5":
                        candidates.append((f"socks5h://{ip}:{port}", f"SOCKS5-{country}:{port}"))
                    elif proto in ("http", "https"):
                        candidates.append((f"http://{ip}:{port}", f"HTTP-{country}:{port}"))
                    if len(candidates) >= limit:
                        break
                log.info("Загружено %d прокси-кандидатов из Proxifly JSON", len(candidates))
                return candidates
    except Exception as e:
        log.warning("Не удалось загрузить Proxifly JSON: %s, пробуем текстовые списки", e)

    # 2. Fallback: загрузка текстовых списков SOCKS5 и HTTPS
    try:
        async with AsyncSession(impersonate="chrome124", verify=False) as s:
            r_socks = await s.get(PROXIFLY_SOCKS5_URL, timeout=6.0)
            if r_socks.status_code == 200:
                for line in r_socks.text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    target = line.replace("socks5://", "socks5h://")
                    parts = target.split("://")[-1].split(":")
                    port = parts[-1] if len(parts) > 1 else ""
                    name = f"SOCKS5:{port}"
                    if target not in seen:
                        seen.add(target)
                        candidates.append((target, name))

            r_https = await s.get(PROXIFLY_HTTPS_URL, timeout=6.0)
            if r_https.status_code == 200:
                for line in r_https.text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    target = line if "://" in line else f"http://{line}"
                    parts = target.split("://")[-1].split(":")
                    port = parts[-1] if len(parts) > 1 else ""
                    name = f"HTTP:{port}"
                    if target not in seen:
                        seen.add(target)
                        candidates.append((target, name))
                    if len(candidates) >= limit:
                        break
    except Exception as e:
        log.error("Ошибка загрузки резервных текстовых списков прокси: %s", e)

    return candidates


async def ping_single_candidate(
    url: str,
    name: str,
    sem: asyncio.Semaphore,
    timeout: float = 2.5,
) -> Optional[tuple[str, str, float]]:
    """Проверяет один прокси к MRKT API. Возвращает (url, name, latency_ms) или None."""
    async with sem:
        t0 = time.monotonic()
        try:
            async with AsyncSession(
                impersonate="chrome124",
                proxies={"http": url, "https": url},
                verify=False,
            ) as s:
                res = await s.get(TEST_ENDPOINT, timeout=timeout)
                dt = (time.monotonic() - t0) * 1000.0
                if res.status_code in (200, 401, 403, 429):
                    return (url, name, dt)
        except Exception:
            pass
        return None


async def find_fastest_proxies(
    max_candidates: int = 500,
    timeout: float = 2.5,
    concurrency: int = 100,
    max_results: int = 50,
) -> list[SimpleProxy]:
    """
    Скачивает кандидатов, параллельно замеряет задержку к api.tgmrkt.io,
    фильтрует и возвращает до max_results объектов SimpleProxy,
    отсортированных по возрастанию задержки (самые быстрые первыми).
    """
    candidates = await fetch_candidates(limit=max_candidates)
    if not candidates:
        return []

    sem = asyncio.Semaphore(concurrency)
    tasks = [ping_single_candidate(u, n, sem, timeout=timeout) for u, n in candidates]
    results = await asyncio.gather(*tasks)

    working = [r for r in results if r is not None]
    working.sort(key=lambda x: x[2])  # сортировка по ms

    proxies: list[SimpleProxy] = []
    for url, name, lat in working[:max_results]:
        full_url = f"{url}#{name}"
        sp = SimpleProxy(full_url, name=name, ping_ms=lat)
        proxies.append(sp)

    log.info(
        "Автопоиск прокси: проверено %d, ответили %d, отобрано %d лучших (топ: %.0f мс)",
        len(candidates),
        len(working),
        len(proxies),
        proxies[0].ping_ms if proxies else 0,
    )
    return proxies


def save_proxies_to_file(
    proxies: list[SimpleProxy],
    use_direct: bool = False,
    file_path: str = "proxies.txt",
) -> None:
    """Сохраняет список найденных прокси в proxies.txt."""
    try:
        lines = [
            "# Автоматически найденные быстрые прокси (Proxifly)",
            f"# Обновлено: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if use_direct:
            lines.append("USE_DIRECT=true")
        lines.append("")
        for p in proxies:
            lines.append(f"{p.url}#{p.name}")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        log.info("Сохранено %d прокси в %s", len(proxies), file_path)
    except Exception as e:
        log.error("Ошибка сохранения прокси в %s: %s", file_path, e)


async def auto_replenish_background(pool: Any) -> int:
    """
    Фоновое пополнение горячего резерва прокси, если он истощился (< 3 штук).
    Запускается как background task без блокировки основного сканера.
    """
    if _replenish_lock.locked():
        return 0

    async with _replenish_lock:
        if pool and len(getattr(pool, "_reserve_proxies", [])) >= 5:
            return 0

        log.info("🔄 Фоновое пополнение резерва прокси...")
        try:
            fast = await find_fastest_proxies(
                max_candidates=300,
                timeout=2.5,
                concurrency=80,
                max_results=20,
            )
            if not fast or not pool:
                return 0

            added = pool.add_reserve_proxies(fast)
            log.info("✅ Горячий резерв пополнен: +%d прокси (всего в резерве: %d)", added, len(pool._reserve_proxies))
            return added
        except Exception as err:
            log.warning("Ошибка фонового пополнения резерва прокси: %s", err)
            return 0
