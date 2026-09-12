"""
proxy_finder.py — Автопоиск, замер пинга и авто-пополнение пула прокси.

Источники:
  1. VLESS Reality подписки (быстрые европейские ноды):
     - Gamededio: https://gamededio.com/v2/5859454259555645444f40 (UA: Happ)
     - Ecobuy:    https://vpn.ecobuy.ltd/sub/... (UA: v2rayN)
  2. Databay free-proxy-list (обновляется каждые 5 минут):
     - https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt
     - https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt
     - https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks4.txt
  3. Proxifly free-proxy-list (обновляется каждые 5 минут):
     - https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.json
     - https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks5/data.txt
     - https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt

Особенности:
  - 100% защита от пересечения IP: ни один слот или резерв не разделяет один и тот же IP/хост.
  - Жёсткий фильтр скорости: пинг строго <= 800 мс (всё что выше 800 мс бракуется).
  - Персистентный пул пользовательских прокси (custom_proxies.txt) — сохраняется навсегда
    и имеет наивысший приоритет при автопоиске.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket
import ssl
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from curl_cffi.requests import AsyncSession

from xray_proxy import (
    BASE_SOCKS_PORT,
    SimpleProxy,
    XrayProcess,
    find_xray_binary,
    parse_ss,
    parse_trojan,
    parse_vless,
    ping_proxy_async,
)

log = logging.getLogger("scanner")

_allocated_ports: set[int] = set()


def allocate_local_port(start_port: int = 11000) -> int:
    """Выделяет гарантированно свободный локальный порт TCP для запуска Xray."""
    port = max(start_port, 11000)
    while port < 60000:
        if port not in _allocated_ports:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    _allocated_ports.add(port)
                    return port
                except OSError:
                    pass
        port += 1
    return 10999


def release_local_port(port: int) -> None:
    """Освобождает порт из реестра."""
    _allocated_ports.discard(port)

# ── Подписки VLESS ────────────────────────────────────────────────────────────
SUB_GAMEDEDIO_URL = "https://gamededio.com/v2/5859454259555645444f40"
SUB_ECOBUY_URL = (
    "https://vpn.ecobuy.ltd/sub/"
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpYXQiOjE3ODI1NzA1MTIsImV4cCI6MTc4NTE2MjUxMiwic3ViX2lkIjo5MjgzMDcsImNsaWVudCI6IjNmYzA1M2MyLTY2OTctNDlkYy1hMzU0LTg4ZTM5ZGJjZDQxOCIsInN1YiI6ImU0MGJkMWJhLThlYjgtNGQxMC1iZWIxLWNiNDMyNmRhOTRiZiIsInByb2plY3RfaWQiOjEsImNvdW50cnkiOiJhbGwifQ."
    "Wb44AxhqgXXKHHzig7TdzKqWWX_AJ6q2mXXBuG-9SVo"
)

# ── Источники Databay ─────────────────────────────────────────────────────────
DATABAY_SOCKS5_URL = "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt"
DATABAY_SOCKS5_MIRROR = "https://cdn.jsdelivr.net/gh/databay-labs/free-proxy-list@master/socks5.txt"
DATABAY_HTTP_URL = "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt"
DATABAY_HTTP_MIRROR = "https://cdn.jsdelivr.net/gh/databay-labs/free-proxy-list@master/http.txt"
DATABAY_SOCKS4_URL = "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks4.txt"
DATABAY_SOCKS4_MIRROR = "https://cdn.jsdelivr.net/gh/databay-labs/free-proxy-list@master/socks4.txt"

# ── Источники Proxifly ────────────────────────────────────────────────────────
PROXIFLY_JSON_URL = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.json"
PROXIFLY_SOCKS5_URL = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks5/data.txt"
PROXIFLY_HTTPS_URL = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt"

TEST_ENDPOINT = "https://api.tgmrkt.io/api/v1/gifts/collections"
MAX_PING_THRESHOLD_MS = 800.0  # Порог отбраковки: не более 800 мс

_replenish_lock = asyncio.Lock()


def extract_proxy_host(proxy_or_url: Any) -> str:
    """
    Извлекает чистый IP или hostname прокси для проверки на уникальность.
    Гарантирует, что разные порты одного и того же IP не будут считаться уникальными.
    """
    if hasattr(proxy_or_url, "cfg"):
        host = getattr(proxy_or_url.cfg, "host", None)
        if host:
            return str(host).lower().strip()

    url = getattr(proxy_or_url, "url", None) or str(proxy_or_url)
    p = urlparse(url)
    if p.hostname:
        return str(p.hostname).lower().strip()

    # fallback для raw ip:port или user:pass@ip:port
    clean = url.split("://")[-1].split("@")[-1].split("#")[0].split("?")[0]
    return clean.split(":")[0].lower().strip()


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
    seen_ips = {extract_proxy_host(l) for l in existing if extract_proxy_host(l)}
    added = 0
    updated = list(existing)

    for line in new_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" not in line:
            parts = line.split(":")
            if len(parts) >= 2:
                line = f"socks5h://{line}"

        host = extract_proxy_host(line)
        if host and host not in seen_ips:
            seen_ips.add(host)
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
#  Загрузка VLESS подписок (Gamededio + Ecobuy)
# ─────────────────────────────────────────────

def fetch_vless_subscriptions(exclude_ips: Optional[set[str]] = None) -> list[tuple[str, str, str]]:
    """
    Загружает и парсит VLESS Reality узлы из подписок Gamededio и Ecobuy.
    Фильтрует дубликаты IP и исключает уже занятые exclude_ips.
    Возвращает [(vless_url, ip_or_host, remarks), ...]
    """
    results: list[tuple[str, str, str]] = []
    local_seen: set[str] = set(exclude_ips or set())
    ctx = ssl._create_unverified_context()

    # 1. Gamededio (UA: Happ)
    try:
        req = urllib.request.Request(SUB_GAMEDEDIO_URL, headers={"User-Agent": "Happ"})
        with urllib.request.urlopen(req, timeout=12, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))

        for item in data:
            rem = item.get("remarks", "").strip()
            if any(k in rem for k in ("Автовыбор", "Лучший")):
                continue

            for ob in item.get("outbounds", []):
                if ob.get("protocol") != "vless":
                    continue
                vnext = ob.get("settings", {}).get("vnext", [])
                if not vnext:
                    continue
                node = vnext[0]
                users = node.get("users", [])
                if not users:
                    continue
                uuid = users[0].get("id")
                flow = users[0].get("flow", "")
                addr = str(node.get("address", "")).strip()
                port = node.get("port")
                if not addr or not port or addr.lower() in local_seen:
                    continue

                local_seen.add(addr.lower())
                stream = ob.get("streamSettings", {})
                net = stream.get("network", "tcp")
                sec = stream.get("security", "none")
                params = {"type": net}
                if flow:
                    params["flow"] = flow
                if sec in ("tls", "reality"):
                    params["security"] = sec
                    sk = "realitySettings" if sec == "reality" else "tlsSettings"
                    sdata = stream.get(sk, {})
                    if s := sdata.get("serverName"):
                        params["sni"] = s
                    if f := sdata.get("fingerprint"):
                        params["fp"] = f
                    if pb := sdata.get("publicKey"):
                        params["pbk"] = pb
                    if sid := sdata.get("shortId"):
                        params["sid"] = sid
                    if sp := sdata.get("spiderX"):
                        params["spx"] = sp
                query = urlencode(params)
                v_url = f"vless://{uuid}@{addr}:{port}?{query}#{quote(rem)}"
                results.append((v_url, addr, rem))
    except Exception as e:
        log.warning("Ошибка загрузки подписки Gamededio: %s", e)

    # 2. Ecobuy (UA: v2rayN/6.23)
    try:
        req = urllib.request.Request(SUB_ECOBUY_URL, headers={"User-Agent": "v2rayN/6.23"})
        with urllib.request.urlopen(req, timeout=12, context=ctx) as r:
            raw = r.read().decode("utf-8", errors="ignore").strip()
        pad = len(raw) % 4
        if pad:
            raw += "=" * (4 - pad)
        decoded = base64.b64decode(raw).decode("utf-8", errors="ignore")

        for line in decoded.splitlines():
            line = line.strip()
            if not line or not line.startswith("vless://"):
                continue
            p = urlparse(line)
            host = p.hostname or ""
            if not host or host.lower() in local_seen:
                continue
            local_seen.add(host.lower())
            rem = unquote(p.fragment) if p.fragment else host
            results.append((line, host, rem))
    except Exception as e:
        log.warning("Ошибка загрузки подписки Ecobuy: %s", e)

    log.info("Загружено %d уникальных VLESS нод из подписок", len(results))
    return results


# ─────────────────────────────────────────────
#  Загрузка кандидатов из Databay и Proxifly
# ─────────────────────────────────────────────

async def fetch_all_public_candidates(exclude_ips: Optional[set[str]] = None, limit: int = 1000) -> list[tuple[str, str, str]]:
    """
    Загружает кандидатов из Databay и Proxifly.
    Строго отбрасывает дубликаты IP, уже присутствующие в exclude_ips.
    Возвращает [(proxy_url, host_ip, display_name), ...]
    """
    candidates: list[tuple[str, str, str]] = []
    local_seen: set[str] = set(exclude_ips or set())

    async with AsyncSession(impersonate="chrome124", verify=False) as s:
        # 1. Proxifly JSON
        try:
            r = await s.get(PROXIFLY_JSON_URL, timeout=6.0)
            if r.status_code == 200:
                for item in r.json():
                    proto = item.get("protocol")
                    ip = item.get("ip")
                    port = item.get("port")
                    if not ip or not port:
                        continue
                    ip_clean = str(ip).lower().strip()
                    if ip_clean in local_seen:
                        continue
                    local_seen.add(ip_clean)
                    country = item.get("geolocation", {}).get("country", "XX")
                    if proto == "socks5":
                        candidates.append((f"socks5h://{ip}:{port}", ip_clean, f"SOCKS5-{country}:{port}"))
                    elif proto in ("http", "https"):
                        candidates.append((f"http://{ip}:{port}", ip_clean, f"HTTP-{country}:{port}"))
        except Exception as e:
            log.warning("Ошибка загрузки Proxifly JSON: %s", e)

        # 2. Databay SOCKS5
        for url in (DATABAY_SOCKS5_URL, DATABAY_SOCKS5_MIRROR):
            try:
                r = await s.get(url, timeout=5.0)
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        line = line.strip()
                        if not line or ":" not in line or line.startswith("#"):
                            continue
                        ip_clean = line.split(":")[0].lower().strip()
                        if ip_clean in local_seen:
                            continue
                        local_seen.add(ip_clean)
                        port = line.split(":")[-1]
                        candidates.append((f"socks5h://{line}", ip_clean, f"DATABAY-SOCKS5:{port}"))
                    break
            except Exception:
                pass

        # 3. Databay HTTP
        for url in (DATABAY_HTTP_URL, DATABAY_HTTP_MIRROR):
            try:
                r = await s.get(url, timeout=5.0)
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        line = line.strip()
                        if not line or ":" not in line or line.startswith("#"):
                            continue
                        ip_clean = line.split(":")[0].lower().strip()
                        if ip_clean in local_seen:
                            continue
                        local_seen.add(ip_clean)
                        port = line.split(":")[-1]
                        candidates.append((f"http://{line}", ip_clean, f"DATABAY-HTTP:{port}"))
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
                        ip_clean = line.split(":")[0].lower().strip()
                        if ip_clean in local_seen:
                            continue
                        local_seen.add(ip_clean)
                        port = line.split(":")[-1]
                        candidates.append((f"socks4://{line}", ip_clean, f"DATABAY-SOCKS4:{port}"))
                    break
            except Exception:
                pass

    log.info("Собрано %d уникальных публичных прокси из Databay и Proxifly", len(candidates))
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
    timeout_sec = max(1.5, (max_ping_ms / 1000.0) + 0.2)
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


async def ping_vless_node(
    url: str,
    name: str,
    port: int,
    xbin: str,
    sem: asyncio.Semaphore,
    max_ping_ms: float = MAX_PING_THRESHOLD_MS,
) -> Optional[tuple[Any, float]]:
    """Пингует VLESS ноду через Xray. Возвращает (XrayProcess, latency_ms) или None."""
    async with sem:
        cfg = parse_vless(url, port)
        proc = XrayProcess(cfg, xbin)
        timeout_sec = max(2.5, (max_ping_ms / 1000.0) + 0.5)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, proc.start)
            ok, lat, _ = await ping_proxy_async(proc, timeout=timeout_sec)
            if ok and lat <= max_ping_ms:
                proc.ping_ms = lat
                return (proc, lat)
            else:
                try:
                    proc.stop()
                except Exception:
                    pass
                release_local_port(port)
                return None
        except Exception:
            try:
                proc.stop()
            except Exception:
                pass
            release_local_port(port)
            return None


async def ping_custom_proxy(
    line: str,
    index: int,
    sem: asyncio.Semaphore,
    max_ping_ms: float = MAX_PING_THRESHOLD_MS,
) -> Optional[tuple[Any, float]]:
    """Пингует пользовательский прокси. Возвращает (proxy_obj, latency_ms) или None."""
    async with sem:
        port = allocate_local_port()
        p_obj = create_proxy_object(line, port_offset=port)
        if not p_obj:
            release_local_port(port)
            return None

        timeout_sec = max(2.5, (max_ping_ms / 1000.0) + 0.5)
        ok, latency, _ = await ping_proxy_async(p_obj, timeout=timeout_sec)
        if ok and latency <= max_ping_ms:
            p_obj.ping_ms = latency
            return (p_obj, latency)
        else:
            if hasattr(p_obj, "stop"):
                try:
                    p_obj.stop()
                except Exception:
                    pass
            release_local_port(port)
            return None


# ─────────────────────────────────────────────
#  Комплексный автопоиск с защитой от пересечения IP
# ─────────────────────────────────────────────

async def find_fastest_proxies(
    max_candidates: int = 1200,
    max_ping_ms: float = MAX_PING_THRESHOLD_MS,
    concurrency: int = 120,
    max_results: int = 40,
    include_custom: bool = True,
    exclude_ips: Optional[set[str]] = None,
) -> tuple[list[Any], list[Any], int]:
    """
    Выполняет поиск прокси по всем источникам с гарантией уникальности IP:
      1. Проверяет пользовательские прокси (custom_proxies.txt) — высший приоритет.
      2. Проверяет VLESS Reality ноды из подписок Gamededio и Ecobuy.
      3. Скачивает и параллельно пингует кандидатов из Databay + Proxifly.
      4. Все серверы строго <= 800 мс и без пересечения IP-адресов.
    """
    sem = asyncio.Semaphore(concurrency)
    assigned_ips: set[str] = set(exclude_ips or set())

    custom_working: list[Any] = []
    vless_working: list[Any] = []
    public_working: list[SimpleProxy] = []

    # 1. Проверяем сохранённые пользовательские прокси
    if include_custom:
        custom_lines = load_custom_proxies()
        if custom_lines:
            c_tasks = [ping_custom_proxy(l, idx, sem, max_ping_ms) for idx, l in enumerate(custom_lines)]
            c_results = await asyncio.gather(*c_tasks)
            for res in c_results:
                if res is not None:
                    p_obj, lat = res
                    host = extract_proxy_host(p_obj)
                    if host and host not in assigned_ips:
                        assigned_ips.add(host)
                        if hasattr(p_obj, "cfg") and not p_obj.cfg.name.startswith("⭐"):
                            p_obj.cfg.name = f"⭐ {p_obj.cfg.name}"
                        custom_working.append(p_obj)
                    else:
                        if hasattr(p_obj, "stop"):
                            try:
                                p_obj.stop()
                            except Exception:
                                pass
            custom_working.sort(key=lambda p: getattr(p, "ping_ms", 9999))

    # 2. Проверяем VLESS ноды из подписок Gamededio и Ecobuy
    xbin = find_xray_binary()
    vless_candidates = fetch_vless_subscriptions(exclude_ips=assigned_ips)
    if xbin and vless_candidates:
        v_tasks = [
            ping_vless_node(u, rem, allocate_local_port(), xbin, sem, max_ping_ms)
            for u, host, rem in vless_candidates[:25]
        ]
        v_results = await asyncio.gather(*v_tasks)
        for res in v_results:
            if res is not None:
                proc, lat = res
                host = extract_proxy_host(proc)
                if host and host not in assigned_ips:
                    assigned_ips.add(host)
                    vless_working.append(proc)
                else:
                    try:
                        proc.stop()
                    except Exception:
                        pass
                    if hasattr(proc, "cfg") and hasattr(proc.cfg, "local_port"):
                        release_local_port(proc.cfg.local_port)
        vless_working.sort(key=lambda p: p.ping_ms)
        log.info("VLESS нод подошло (<= %.0f мс): %d", max_ping_ms, len(vless_working))

    # 3. Скачиваем и пингуем кандидатов из Databay и Proxifly
    public_candidates = await fetch_all_public_candidates(exclude_ips=assigned_ips, limit=max_candidates)
    total_candidates = len(vless_candidates) + len(public_candidates)

    if public_candidates:
        tasks = [ping_candidate_under_threshold(u, n, sem, max_ping_ms) for u, host, n in public_candidates]
        results = await asyncio.gather(*tasks)
        working_tuples = [r for r in results if r is not None]
        working_tuples.sort(key=lambda x: x[2])

        for url, name, lat in working_tuples:
            host = extract_proxy_host(url)
            if host and host not in assigned_ips:
                assigned_ips.add(host)
                sp = SimpleProxy(f"{url}#{name}", name=name, ping_ms=lat)
                public_working.append(sp)
                if len(public_working) >= max_results:
                    break

        log.info(
            "Публичных прокси подошло (<= %.0f мс): %d (топ: %.0f мс)",
            max_ping_ms,
            len(public_working),
            public_working[0].ping_ms if public_working else 0,
        )

    # 4. Объединяем: Пользовательские -> VLESS подписки -> Databay/Proxifly
    return custom_working, vless_working + public_working, total_candidates


def save_proxies_to_file(
    active_proxies: list[Any],
    reserve_proxies: list[Any],
    use_direct: bool = False,
    file_path: str = "proxies.txt",
) -> None:
    """
    Сохраняет активные и резервные прокси в proxies.txt с дедупликацией IP.
    """
    try:
        lines = [
            "# MRKT Scanner Proxy Pool (Gamededio + Ecobuy + Databay + Proxifly + Custom)",
            f"# Обновлено: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if use_direct:
            lines.append("USE_DIRECT=true")
        lines.append("")

        seen_hosts = set()

        # Активные
        lines.append("# ── Активные прокси ──")
        for p in active_proxies:
            url = getattr(p, "url", None)
            if not url and hasattr(p, "cfg"):
                url = getattr(p.cfg, "url", None)
            host = extract_proxy_host(p)
            if url and host not in seen_hosts:
                seen_hosts.add(host)
                name = getattr(p.cfg, "name", "")
                suffix = f"#{name}" if name and "#" not in url else ""
                lines.append(f"{url}{suffix}")

        lines.append("")
        # Резервные
        lines.append("# ── Горячий резерв ──")
        for p in reserve_proxies:
            url = getattr(p, "url", None)
            if not url and hasattr(p, "cfg"):
                url = getattr(p.cfg, "url", None)
            host = extract_proxy_host(p)
            if url and host not in seen_hosts:
                seen_hosts.add(host)
                name = getattr(p.cfg, "name", "")
                suffix = f"#{name}" if name and "#" not in url else ""
                lines.append(f"{url}{suffix}")

        target = Path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        log.info("Сохранено %d активных и %d резервных прокси в %s (уникальных IP: %d)", len(active_proxies), len(reserve_proxies), file_path, len(seen_hosts))
    except Exception as e:
        log.error("Ошибка записи в %s: %s", file_path, e)


async def auto_replenish_background(pool: Any) -> int:
    """
    Фоновое пополнение горячего резерва (<= 800 мс) с проверкой уникальности IP.
    """
    if _replenish_lock.locked():
        return 0

    async with _replenish_lock:
        if pool and len(getattr(pool, "_reserve_proxies", [])) >= 5:
            return 0

        log.info("🔄 Фоновое пополнение резерва прокси (<= 800 мс, уникальные IP)...")
        try:
            current_ips: set[str] = set()
            if pool:
                current_ips.update(pool._extract_host(s.proxy) for s in pool._slots if s.proxy)
                current_ips.update(pool._extract_host(r) for r in pool._reserve_proxies)
                current_ips.discard(None)

            _, fast_new, _ = await find_fastest_proxies(
                max_candidates=400,
                max_ping_ms=MAX_PING_THRESHOLD_MS,
                concurrency=90,
                max_results=20,
                include_custom=False,
                exclude_ips=current_ips,
            )
            if not fast_new or not pool:
                return 0

            added = pool.add_reserve_proxies(fast_new)
            save_proxies_to_file(
                pool.get_proxies(),
                getattr(pool, "_reserve_proxies", []),
                use_direct=getattr(pool, "use_direct", False),
            )
            log.info("✅ Горячий резерв пополнен: +%d прокси (всего в резерве: %d)", added, len(pool._reserve_proxies))
            return added
        except Exception as err:
            log.warning("Ошибка фонового пополнения резерва: %s", err)
            return 0
