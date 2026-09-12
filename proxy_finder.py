"""
proxy_finder.py — Автопоиск, замер пинга и авто-пополнение пула прокси из Proxifly и Databay.

Источники:
  1. Databay free-proxy-list (обновляется каждые 5 минут):
     - https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt
     - https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt
     - https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks4.txt
  2. Proxifly free-proxy-list (обновляется каждые 5 минут):
     - https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.json
     - https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks5/data.txt
     - https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt

Особенности:
  - Жёсткий фильтр скорости: пинг строго <= 800 мс (всё что выше 800 мс бракуется).
  - Персистентный пул пользовательских прокси (custom_proxies.txt) — сохраняется навсегда
    и имеет наивысший приоритет над публичными прокси при автопоиске.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from curl_cffi.requests import AsyncSession

from xray_proxy import SimpleProxy, parse_vless, parse_trojan, parse_ss, XrayProcess, find_xray_binary

log = logging.getLogger("scanner")

# ── Источники Proxifly ────────────────────────────────────────────────────────
PROXIFLY_JSON_URL = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.json"
PROXIFLY_SOCKS5_URL = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks5/data.txt"
PROXIFLY_HTTPS_URL = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt"

# ── Источники Databay ─────────────────────────────────────────────────────────
DATABAY_SOCKS5_URL = "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt"
DATABAY_SOCKS5_MIRROR = "https://cdn.jsdelivr.net/gh/databay-labs/free-proxy-list@master/socks5.txt"
DATABAY_HTTP_URL = "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt"
DATABAY_HTTP_MIRROR = "https://cdn.jsdelivr.net/gh/databay-labs/free-proxy-list@master/http.txt"
DATABAY_SOCKS4_URL = "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks4.txt"
DATABAY_SOCKS4_MIRROR = "https://cdn.jsdelivr.net/gh/databay-labs/free-proxy-list@master/socks4.txt"

TEST_ENDPOINT = "https://api.tgmrkt.io/api/v1/gifts/collections"
MAX_PING_THRESHOLD_MS = 800.0  # Порог отбраковки: не более 800 мс

_replenish_lock = asyncio.Lock()


# ─────────────────────────────────────────────
#  Управление пользовательскими прокси
# ─────────────────────────────────────────────

def get_custom_proxies_file() -> Path:
    """Возвращает путь к custom_proxies.txt (приоритет папке data/)."""
    p_data = Path("data")
    if p_data.is_dir():
        return p_data / "custom_proxies.txt"
    return Path("custom_proxies.txt")


def load_custom_proxies() -> list[str]:
    """Загружает список сохранённых пользовательских прокси."""
    fpath = get_custom_proxies_file()
    if not fpath.is_file():
        # Проверяем также корень если искали в data
        if Path("custom_proxies.txt").is_file():
            fpath = Path("custom_proxies.txt")
        else:
            return []

    lines = []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    lines.append(line)
    except Exception as e:
        log.warning("Ошибка чтения %s: %s", fpath, e)
    return lines


def save_custom_proxies(proxies: list[str]) -> None:
    """Перезаписывает custom_proxies.txt."""
    fpath = get_custom_proxies_file()
    try:
        fpath.parent.mkdir(parents=True, exist_ok=True)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("# Пользовательские прокси (сохраняются навсегда)\n")
            f.write(f"# Обновлено: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            for p in proxies:
                p = p.strip()
                if p:
                    f.write(p + "\n")
    except Exception as e:
        log.error("Ошибка сохранения %s: %s", fpath, e)


def add_custom_proxies(new_lines: list[str]) -> tuple[int, list[str]]:
    """
    Добавляет новые строки в custom_proxies.txt без дубликатов.
    Возвращает (количество добавленных, итоговый список).
    """
    existing = load_custom_proxies()
    seen = set(existing)
    added = 0
    updated = list(existing)

    for line in new_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Если прислали просто ip:port без схемы -> делаем socks5h://
        if "://" not in line:
            parts = line.split(":")
            if len(parts) == 2 and parts[1].isdigit():
                line = f"socks5h://{line}"
            elif len(parts) >= 3:
                line = f"socks5h://{line}"

        if line not in seen:
            seen.add(line)
            updated.append(line)
            added += 1

    if added > 0:
        save_custom_proxies(updated)
    return added, updated


def create_proxy_object(line: str, port_offset: int = 10900) -> Optional[Any]:
    """Создаёт объект прокси (SimpleProxy или XrayProcess) из строки конфига."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    if line.startswith(("http://", "https://", "socks5://", "socks5h://")):
        return SimpleProxy(line)
    elif line.startswith("trojan://"):
        xbin = find_xray_binary()
        if not xbin:
            return None
        cfg = parse_trojan(line, port_offset)
        proc = XrayProcess(cfg, xbin)
        try:
            proc.start()
            return proc
        except Exception:
            return None
    elif line.startswith("ss://"):
        xbin = find_xray_binary()
        if not xbin:
            return None
        cfg = parse_ss(line, port_offset)
        proc = XrayProcess(cfg, xbin)
        try:
            proc.start()
            return proc
        except Exception:
            return None
    elif line.startswith("vless://"):
        xbin = find_xray_binary()
        if not xbin:
            return None
        cfg = parse_vless(line, port_offset)
        proc = XrayProcess(cfg, xbin)
        try:
            proc.start()
            return proc
        except Exception:
            return None
    elif "://" not in line and ":" in line:
        return SimpleProxy(f"socks5h://{line}")
    return None


# ─────────────────────────────────────────────
#  Загрузка кандидатов из Databay и Proxifly
# ─────────────────────────────────────────────

async def fetch_all_candidates(limit: int = 1200) -> list[tuple[str, str]]:
    """
    Загружает и объединяет кандидатов из Databay и Proxifly.
    Возвращает [(proxy_url, display_name), ...], очищенных от дубликатов.
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    async with AsyncSession(impersonate="chrome124", verify=False) as s:
        # 1. Proxifly JSON (с метаданными стран и протоколов)
        try:
            r = await s.get(PROXIFLY_JSON_URL, timeout=7.0)
            if r.status_code == 200:
                for item in r.json():
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
        except Exception as e:
            log.warning("Не удалось загрузить Proxifly JSON: %s", e)

        # 2. Databay SOCKS5
        for url in (DATABAY_SOCKS5_URL, DATABAY_SOCKS5_MIRROR):
            try:
                r = await s.get(url, timeout=6.0)
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        line = line.strip()
                        if not line or ":" not in line or line.startswith("#"):
                            continue
                        if line in seen:
                            continue
                        seen.add(line)
                        port = line.split(":")[-1]
                        candidates.append((f"socks5h://{line}", f"DATABAY-SOCKS5:{port}"))
                    break
            except Exception:
                pass

        # 3. Databay HTTP
        for url in (DATABAY_HTTP_URL, DATABAY_HTTP_MIRROR):
            try:
                r = await s.get(url, timeout=6.0)
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        line = line.strip()
                        if not line or ":" not in line or line.startswith("#"):
                            continue
                        if line in seen:
                            continue
                        seen.add(line)
                        port = line.split(":")[-1]
                        candidates.append((f"http://{line}", f"DATABAY-HTTP:{port}"))
                    break
            except Exception:
                pass

        # 4. Databay SOCKS4
        for url in (DATABAY_SOCKS4_URL, DATABAY_SOCKS4_MIRROR):
            try:
                r = await s.get(url, timeout=5.0)
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        line = line.strip()
                        if not line or ":" not in line or line.startswith("#"):
                            continue
                        if line in seen:
                            continue
                        seen.add(line)
                        port = line.split(":")[-1]
                        candidates.append((f"socks4://{line}", f"DATABAY-SOCKS4:{port}"))
                    break
            except Exception:
                pass

        # 5. Резервные Proxifly текстовые списки
        if len(candidates) < 100:
            for purl, proto, prefix in [
                (PROXIFLY_SOCKS5_URL, "socks5h", "SOCKS5"),
                (PROXIFLY_HTTPS_URL, "http", "HTTPS"),
            ]:
                try:
                    r = await s.get(purl, timeout=5.0)
                    if r.status_code == 200:
                        for line in r.text.splitlines():
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            raw = line.split("://")[-1]
                            if raw in seen:
                                continue
                            seen.add(raw)
                            port = raw.split(":")[-1]
                            target = f"{proto}://{raw}"
                            candidates.append((target, f"{prefix}:{port}"))
                except Exception:
                    pass

    log.info("Собрано %d уникальных прокси-кандидатов из Databay и Proxifly", len(candidates))
    return candidates[:limit]


# ─────────────────────────────────────────────
#  Замер пинга с фильтром <= 800 мс
# ─────────────────────────────────────────────

async def ping_candidate_under_threshold(
    url: str,
    name: str,
    sem: asyncio.Semaphore,
    max_ping_ms: float = MAX_PING_THRESHOLD_MS,
) -> Optional[tuple[str, str, float]]:
    """
    Проверяет прокси к api.tgmrkt.io.
    Строго бракует любые серверы с задержкой > max_ping_ms (800 мс).
    """
    timeout_sec = (max_ping_ms / 1000.0) + 0.15  # Обрываем сразу при превышении 800мс
    async with sem:
        t0 = time.monotonic()
        try:
            async with AsyncSession(
                impersonate="chrome124",
                proxies={"http": url, "https": url},
                verify=False,
            ) as s:
                res = await s.get(TEST_ENDPOINT, timeout=timeout_sec)
                dt = (time.monotonic() - t0) * 1000.0
                if res.status_code in (200, 401, 403, 429) and dt <= max_ping_ms:
                    return (url, name, dt)
        except Exception:
            pass
        return None


async def ping_custom_proxy(
    line: str,
    index: int,
    sem: asyncio.Semaphore,
    max_ping_ms: float = MAX_PING_THRESHOLD_MS,
) -> Optional[tuple[Any, float]]:
    """Пингует пользовательский прокси. Возвращает (proxy_obj, latency_ms) или None."""
    async with sem:
        p_obj = create_proxy_object(line, port_offset=10950 + index)
        if not p_obj:
            return None

        # Проверяем пинг
        from xray_proxy import ping_proxy_async
        timeout_sec = (max_ping_ms / 1000.0) + 0.2
        ok, latency, _ = await ping_proxy_async(p_obj, timeout=timeout_sec)
        if ok and latency <= max_ping_ms:
            p_obj.ping_ms = latency
            return (p_obj, latency)
        else:
            # Если отбракован или ошибка, останавливаем процесс если это Xray
            if hasattr(p_obj, "stop"):
                try:
                    p_obj.stop()
                except Exception:
                    pass
            return None


# ─────────────────────────────────────────────
#  Поиск быстрейших прокси с интеграцией Custom
# ─────────────────────────────────────────────

async def find_fastest_proxies(
    max_candidates: int = 1200,
    max_ping_ms: float = MAX_PING_THRESHOLD_MS,
    concurrency: int = 120,
    max_results: int = 40,
    include_custom: bool = True,
) -> tuple[list[Any], list[Any], int]:
    """
    Выполняет комплексный поиск:
      1. Загружает и пингует пользовательские прокси (они имеют наивысший приоритет).
      2. Скачивает и параллельно пингует кандидатов из Databay + Proxifly (строго <= 800 мс).
      3. Возвращает (active_proxies, reserve_proxies, total_tested_candidates).
    """
    sem = asyncio.Semaphore(concurrency)
    custom_working: list[Any] = []

    # 1. Проверяем сохранённые пользовательские прокси
    if include_custom:
        custom_lines = load_custom_proxies()
        if custom_lines:
            log.info("Проверка %d пользовательских прокси...", len(custom_lines))
            c_tasks = [ping_custom_proxy(l, idx, sem, max_ping_ms) for idx, l in enumerate(custom_lines)]
            c_results = await asyncio.gather(*c_tasks)
            for res in c_results:
                if res is not None:
                    p_obj, lat = res
                    # Помечаем имя как [Пользовательский]
                    if hasattr(p_obj, "cfg") and not p_obj.cfg.name.startswith("⭐"):
                        p_obj.cfg.name = f"⭐ {p_obj.cfg.name}"
                    custom_working.append(p_obj)
            custom_working.sort(key=lambda p: getattr(p, "ping_ms", 9999))
            log.info("Пользовательских прокси подошло: %d из %d", len(custom_working), len(custom_lines))

    # 2. Скачиваем кандидатов из Databay и Proxifly
    candidates = await fetch_all_candidates(limit=max_candidates)
    total_candidates = len(candidates)

    public_working: list[SimpleProxy] = []
    if candidates:
        tasks = [ping_candidate_under_threshold(u, n, sem, max_ping_ms) for u, n in candidates]
        results = await asyncio.gather(*tasks)
        working_tuples = [r for r in results if r is not None]
        working_tuples.sort(key=lambda x: x[2])  # сортировка по ms

        for url, name, lat in working_tuples[:max_results]:
            sp = SimpleProxy(f"{url}#{name}", name=name, ping_ms=lat)
            public_working.append(sp)

        log.info(
            "Публичных прокси подошло (<= %.0f мс): %d из %d (топ: %.0f мс)",
            max_ping_ms,
            len(public_working),
            len(candidates),
            public_working[0].ping_ms if public_working else 0,
        )

    # 3. Объединяем: custom_working идут в самом начале!
    combined_proxies = custom_working + public_working
    return custom_working, public_working, total_candidates


def save_proxies_to_file(
    active_proxies: list[Any],
    reserve_proxies: list[Any],
    use_direct: bool = False,
    file_path: str = "proxies.txt",
) -> None:
    """
    Сохраняет активные и резервные прокси в proxies.txt, сохраняя
    пользовательские конфигурации и разделы.
    """
    try:
        lines = [
            "# MRKT Scanner Proxy Pool (Databay + Proxifly + Custom)",
            f"# Обновлено: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if use_direct:
            lines.append("USE_DIRECT=true")
        lines.append("")

        seen_urls = set()

        # Записываем активные прокси
        lines.append("# ── Активные прокси ──")
        for p in active_proxies:
            url = getattr(p, "url", None)
            if not url and hasattr(p, "cfg"):
                url = getattr(p.cfg, "url", None)
            if url and url not in seen_urls:
                seen_urls.add(url)
                name = getattr(p.cfg, "name", "")
                suffix = f"#{name}" if name and "#" not in url else ""
                lines.append(f"{url}{suffix}")

        lines.append("")
        lines.append("# ── Горячий резерв ──")
        for p in reserve_proxies:
            url = getattr(p, "url", None)
            if not url and hasattr(p, "cfg"):
                url = getattr(p.cfg, "url", None)
            if url and url not in seen_urls:
                seen_urls.add(url)
                name = getattr(p.cfg, "name", "")
                suffix = f"#{name}" if name and "#" not in url else ""
                lines.append(f"{url}{suffix}")

        target = Path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        log.info("Сохранено %d активных и %d резервных прокси в %s", len(active_proxies), len(reserve_proxies), file_path)
    except Exception as e:
        log.error("Ошибка записи в %s: %s", file_path, e)


async def auto_replenish_background(pool: Any) -> int:
    """
    Фоновое пополнение горячего резерва прокси (<= 800 мс), если он истощился.
    Запускается как background task без блокировки сканера.
    """
    if _replenish_lock.locked():
        return 0

    async with _replenish_lock:
        if pool and len(getattr(pool, "_reserve_proxies", [])) >= 5:
            return 0

        log.info("🔄 Фоновое пополнение резерва прокси (Databay + Proxifly, <= 800 мс)...")
        try:
            _, public_fast, _ = await find_fastest_proxies(
                max_candidates=400,
                max_ping_ms=MAX_PING_THRESHOLD_MS,
                concurrency=90,
                max_results=20,
                include_custom=False,
            )
            if not public_fast or not pool:
                return 0

            added = pool.add_reserve_proxies(public_fast)
            log.info("✅ Горячий резерв пополнен: +%d прокси (всего в резерве: %d)", added, len(pool._reserve_proxies))
            return added
        except Exception as err:
            log.warning("Ошибка фонового пополнения резерва прокси: %s", err)
            return 0
