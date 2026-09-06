"""
xray_proxy.py — парсинг VLESS URL и управление xray процессами.

Требования:
  - xray binary установлен (brew install xray  ИЛИ  скачай с github.com/XTLS/Xray-core/releases)
  - Путь к бинарнику: $XRAY_BIN или авто-поиск в стандартных местах
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse


BASE_SOCKS_PORT = int(os.getenv("XRAY_BASE_PORT", 10800))


# ─────────────────────────────────────────────
#  VLESS URL → Config
# ─────────────────────────────────────────────

@dataclass
class VlessConfig:
    url: str
    uuid: str
    host: str
    port: int
    name: str
    network: str = "tcp"
    security: str = "none"
    sni: str = ""
    fp: str = ""
    path: str = "/"
    ws_host: str = ""
    flow: str = ""
    pbk: str = ""    # Reality public key
    sid: str = ""    # Reality short ID
    spx: str = ""    # Reality spider X
    local_port: int = 0


def _q(params: dict, key: str, default: str = "") -> str:
    vals = params.get(key, [default])
    return vals[0] if vals else default


def parse_vless(url: str, local_port: int) -> VlessConfig:
    """Парсит vless:// URL в VlessConfig."""
    p = urlparse(url)
    q = parse_qs(p.query)
    return VlessConfig(
        url=url,
        uuid=p.username or "",
        host=p.hostname or "",
        port=p.port or 443,
        name=unquote(p.fragment or f"{p.hostname}:{p.port}"),
        network=_q(q, "type", "tcp"),
        security=_q(q, "security", "none"),
        sni=_q(q, "sni"),
        fp=_q(q, "fp", "chrome"),
        path=_q(q, "path", "/"),
        ws_host=_q(q, "host"),
        flow=_q(q, "flow"),
        pbk=_q(q, "pbk"),
        sid=_q(q, "sid"),
        spx=_q(q, "spx"),
        local_port=local_port,
    )


def _build_xray_config(cfg: VlessConfig) -> dict:
    """Генерирует JSON конфиг xray для данного VLESS."""
    stream: dict = {"network": cfg.network}

    # ── TLS ──────────────────────────────────────────────────────────────
    if cfg.security == "tls":
        tls: dict = {}
        if cfg.sni:
            tls["serverName"] = cfg.sni
        if cfg.fp:
            tls["fingerprint"] = cfg.fp
        stream["security"] = "tls"
        stream["tlsSettings"] = tls

    # ── Reality ──────────────────────────────────────────────────────────
    elif cfg.security == "reality":
        rl: dict = {"publicKey": cfg.pbk, "shortId": cfg.sid}
        if cfg.sni:
            rl["serverName"] = cfg.sni
        if cfg.fp:
            rl["fingerprint"] = cfg.fp
        if cfg.spx:
            rl["spiderX"] = cfg.spx
        stream["security"] = "reality"
        stream["realitySettings"] = rl

    else:
        stream["security"] = "none"

    # ── Network-specific settings ─────────────────────────────────────────
    if cfg.network == "ws":
        stream["wsSettings"] = {
            "path": cfg.path,
            "headers": {"Host": cfg.ws_host or cfg.sni or cfg.host},
        }
    elif cfg.network == "grpc":
        stream["grpcSettings"] = {"serviceName": cfg.path.lstrip("/")}
    elif cfg.network in ("http", "h2"):
        stream["httpSettings"] = {
            "path": cfg.path,
            "host": [cfg.ws_host or cfg.sni or cfg.host],
        }

    # ── User ──────────────────────────────────────────────────────────────
    user: dict = {"id": cfg.uuid, "encryption": "none"}
    if cfg.flow:
        user["flow"] = cfg.flow

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "port": cfg.local_port,
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": True},
        }],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [{
                        "address": cfg.host,
                        "port": cfg.port,
                        "users": [user],
                    }]
                },
                "streamSettings": stream,
            },
            {"protocol": "freedom", "tag": "direct"},
        ],
    }


# ─────────────────────────────────────────────
#  XrayProcess
# ─────────────────────────────────────────────

def find_xray_binary() -> Optional[str]:
    """Ищет xray бинарник в стандартных местах."""
    candidates = [
        os.getenv("XRAY_BIN", ""),
        "xray",
        "./xray",
        "/usr/local/bin/xray",
        "/opt/homebrew/bin/xray",
        "/opt/xray/xray",
        os.path.expanduser("~/.local/bin/xray"),
    ]
    for c in candidates:
        if not c:
            continue
        found = shutil.which(c) or (c if os.path.isfile(c) else None)
        if found:
            return found
    return None


class XrayProcess:
    """Один запущенный xray процесс = один VLESS прокси."""

    def __init__(self, cfg: VlessConfig, xray_bin: str):
        self.cfg = cfg
        self.xray_bin = xray_bin
        self.ping_ms: float = 0.0
        self._proc: Optional[subprocess.Popen] = None
        self._cfg_path: Optional[str] = None

    @property
    def socks_url(self) -> str:
        return f"socks5h://127.0.0.1:{self.cfg.local_port}"

    def start(self) -> None:
        xray_cfg = _build_xray_config(self.cfg)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, prefix="xray_mrkt_"
        )
        json.dump(xray_cfg, tmp)
        tmp.close()
        self._cfg_path = tmp.name

        self._proc = subprocess.Popen(
            [self.xray_bin, "run", "-config", self._cfg_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        time.sleep(1.0)  # даём xray время подняться
        if self._proc.poll() is not None:
            err = (self._proc.stderr.read(500) if self._proc.stderr else b"").decode(errors="replace")
            raise RuntimeError(f"xray [{self.cfg.name}] не запустился: {err.strip()}")

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._cfg_path and os.path.exists(self._cfg_path):
            os.unlink(self._cfg_path)
            self._cfg_path = None

    def alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    def __repr__(self) -> str:
        status = "alive" if self.alive() else "dead"
        return f"XrayProcess({self.cfg.name!r}, :{self.cfg.local_port}, {status})"


# ─────────────────────────────────────────────
#  Загрузка прокси из proxies.txt
# ─────────────────────────────────────────────

def load_proxies(path: str = "proxies.txt") -> list[XrayProcess]:
    """
    Читает proxies.txt (одна vless:// строка на строку).
    Запускает xray процесс для каждого прокси.
    Возвращает список XrayProcess.
    """
    file_to_read = None
    if os.path.isfile(path):
        file_to_read = path
    elif os.path.isdir(path):
        for candidate in sorted(os.listdir(path)):
            candidate_path = os.path.join(path, candidate)
            if os.path.isfile(candidate_path):
                file_to_read = candidate_path
                break

    if not file_to_read:
        return []

    lines = []
    with open(file_to_read, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and line.startswith("vless://"):
                lines.append(line)

    if not lines:
        return []

    xray_bin = find_xray_binary()
    if not xray_bin:
        print("⚠️  proxies.txt найден, но xray не обнаружен!")
        print("   Установи: brew install xray")
        print("   Или скачай: https://github.com/XTLS/Xray-core/releases")
        print("   Или укажи путь: XRAY_BIN=/path/to/xray")
        print("   Продолжаем без прокси...\n")
        return []

    processes = []
    for i, url in enumerate(lines):
        port = BASE_SOCKS_PORT + i
        cfg = parse_vless(url, port)
        proc = XrayProcess(cfg, xray_bin)
        try:
            proc.start()
            print(f"  ✅ Прокси [{cfg.name}] → socks5://127.0.0.1:{port}")
            processes.append(proc)
        except Exception as e:
            print(f"  ❌ Прокси [{cfg.name}] не запустился: {e}")

    return processes


# ─────────────────────────────────────────────
#  Проверка пинга и фильтрация быстрых прокси
# ─────────────────────────────────────────────

async def ping_proxy_async(
    proc: XrayProcess,
    test_url: str = "https://api.tgmrkt.io/api/v1/gifts/collections",
    timeout: float = 4.0,
) -> tuple[bool, float, str]:
    """
    Проверяет доступность и пинг одного прокси через cffi AsyncSession.
    Возвращает (success: bool, latency_ms: float, error_msg: str).
    """
    from curl_cffi.requests import AsyncSession

    socks_url = proc.socks_url
    proxies = {"http": socks_url, "https": socks_url}
    t0 = time.monotonic()
    try:
        async with AsyncSession(impersonate="chrome124", proxies=proxies) as session:
            resp = await session.get(test_url, timeout=timeout)
            latency_ms = (time.monotonic() - t0) * 1000.0
            # Любой статус-код от сервера подтверждает что прокси и сеть работают
            if resp.status_code in (200, 401, 403, 429):
                return True, latency_ms, ""
            return False, latency_ms, f"HTTP {resp.status_code}"
    except Exception as e:
        latency_ms = (time.monotonic() - t0) * 1000.0
        err_name = e.__class__.__name__
        err_msg = str(e) or err_name
        if "timeout" in err_msg.lower():
            err_msg = "Таймаут"
        return False, latency_ms, err_msg


async def filter_fast_proxies_async(
    processes: list[XrayProcess],
    max_ping_seconds: float = 3.0,
    test_url: str = "https://api.tgmrkt.io/api/v1/gifts/collections",
) -> list[XrayProcess]:
    """
    Параллельно пингует все запущенные прокси.
    Отсеивает те, у которых пинг > max_ping_seconds или ошибка соединения.
    Останавливает процессы отклонённых прокси.
    """
    if not processes:
        return []

    print(f"  🔍 Проверка пинга {len(processes)} прокси (порог: {max_ping_seconds:.1f}с)...")

    timeout_val = max(4.0, max_ping_seconds + 1.0)
    tasks = [ping_proxy_async(p, test_url=test_url, timeout=timeout_val) for p in processes]
    results = await asyncio.gather(*tasks)

    fast_proxies: list[XrayProcess] = []
    max_ping_ms = max_ping_seconds * 1000.0

    for proc, (ok, latency_ms, err) in zip(processes, results):
        if ok and latency_ms <= max_ping_ms:
            proc.ping_ms = latency_ms
            fast_proxies.append(proc)
        else:
            reason = f"пинг {latency_ms:.0f} мс (> {max_ping_ms:.0f} мс)" if ok else f"{err} ({latency_ms:.0f} мс)"
            print(f"  ❌ Прокси [{proc.cfg.name}] отклонён: {reason}")
            proc.stop()

    # Сортируем от самых быстрых к более медленным
    fast_proxies.sort(key=lambda p: p.ping_ms)

    if not fast_proxies:
        print("  ⚠️  Ни один прокси не прошёл проверку скорости! Работаем напрямую (direct).")
    else:
        print(f"  🎯 Отобрано быстрых прокси: {len(fast_proxies)} из {len(processes)} (ранжированы по скорости):")
        for rank, p in enumerate(fast_proxies, 1):
            print(f"     #{rank} [{p.cfg.name}]: {p.ping_ms:.0f} мс")

    return fast_proxies

