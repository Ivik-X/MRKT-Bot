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


@dataclass
class TrojanConfig:
    url: str
    password: str
    host: str
    port: int
    name: str
    sni: str = ""
    security: str = "tls"
    local_port: int = 0


def parse_trojan(url: str, local_port: int) -> TrojanConfig:
    """Парсит trojan://password@host:port?sni=...#name в TrojanConfig."""
    p = urlparse(url)
    q = parse_qs(p.query)
    host = p.hostname or ""
    return TrojanConfig(
        url=url,
        password=p.username or "",
        host=host,
        port=p.port or 443,
        name=unquote(p.fragment or f"{host}:{p.port or 443}"),
        sni=_q(q, "sni", host),
        security=_q(q, "security", "tls"),
        local_port=local_port,
    )


@dataclass
class ShadowsocksConfig:
    url: str
    method: str
    password: str
    host: str
    port: int
    name: str
    local_port: int = 0


def parse_ss(url: str, local_port: int) -> ShadowsocksConfig:
    """Парсит ss:// URL в ShadowsocksConfig."""
    import base64
    p = urlparse(url)
    name = unquote(p.fragment or f"{p.hostname or ''}:{p.port or 8388}")
    if "@" in p.netloc:
        userinfo, hostport = p.netloc.split("@", 1)
        pad = len(userinfo) % 4
        if pad:
            userinfo += "=" * (4 - pad)
        try:
            decoded = base64.urlsafe_b64decode(userinfo).decode("utf-8")
            method, password = decoded.split(":", 1)
        except Exception:
            method, password = "aes-256-gcm", userinfo
        hp = hostport.split(":")
        host = hp[0]
        port = int(hp[1]) if len(hp) > 1 and hp[1].isdigit() else 8388
    else:
        b64 = p.netloc
        pad = len(b64) % 4
        if pad:
            b64 += "=" * (4 - pad)
        try:
            decoded = base64.urlsafe_b64decode(b64).decode("utf-8")
            userpass, hostport = decoded.split("@", 1)
            method, password = userpass.split(":", 1)
            hp = hostport.split(":")
            host = hp[0]
            port = int(hp[1]) if len(hp) > 1 and hp[1].isdigit() else 8388
        except Exception:
            host = "127.0.0.1"
            port = 8388
            method = "aes-256-gcm"
            password = b64

    return ShadowsocksConfig(
        url=url,
        method=method,
        password=password,
        host=host,
        port=port,
        name=name,
        local_port=local_port,
    )


class SimpleProxy:
    """
    Прямой SOCKS5 / HTTP / HTTPS прокси.
    Работает нативно через curl_cffi без запуска сторонних Xray процессов.
    """
    def __init__(self, url: str):
        self.url = url
        p = urlparse(url)
        self.name = unquote(p.fragment) if p.fragment else f"{p.scheme}://{p.hostname}:{p.port}"
        clean = p._replace(fragment="")
        self.proxy_url = clean.geturl()
        self.ping_ms: float = 0.0
        from types import SimpleNamespace
        self.cfg = SimpleNamespace(name=self.name, local_port=p.port or 0)

    @property
    def socks_url(self) -> str:
        return self.proxy_url

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def alive(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"SimpleProxy({self.name!r}, {self.proxy_url})"


def _build_xray_config(cfg: Any) -> dict:
    """Генерирует JSON конфиг xray для VLESS, Trojan или Shadowsocks."""
    inbounds = [{
        "port": cfg.local_port,
        "listen": "127.0.0.1",
        "protocol": "socks",
        "settings": {"auth": "noauth", "udp": True},
    }]

    if isinstance(cfg, TrojanConfig):
        outbound = {
            "protocol": "trojan",
            "settings": {
                "servers": [{
                    "address": cfg.host,
                    "port": cfg.port,
                    "password": cfg.password,
                }]
            },
            "streamSettings": {
                "network": "tcp",
                "security": "tls",
                "tlsSettings": {
                    "serverName": cfg.sni or cfg.host,
                },
            },
        }
    elif isinstance(cfg, ShadowsocksConfig):
        outbound = {
            "protocol": "shadowsocks",
            "settings": {
                "servers": [{
                    "address": cfg.host,
                    "port": cfg.port,
                    "method": cfg.method,
                    "password": cfg.password,
                }]
            },
        }
    else:
        # VLESS
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

        outbound = {
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": cfg.host,
                    "port": cfg.port,
                    "users": [user],
                }]
            },
            "streamSettings": stream,
        }

    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": [
            outbound,
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
    """Один запущенный xray процесс = один VLESS/Trojan/Shadowsocks прокси."""

    def __init__(self, cfg: Any, xray_bin: str):
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

def load_proxies(path: str = "proxies.txt") -> list[Any]:
    """
    Читает proxies.txt:
      - vless://...       (VLESS Reality / TLS через Xray)
      - trojan://...      (Trojan через Xray)
      - ss://...          (Shadowsocks через Xray)
      - socks5://...      (Прямой SOCKS5 без Xray)
      - http://...        (Прямой HTTP прокси без Xray)
      - https://...       (Прямой HTTPS прокси без Xray)
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
            if not line or line.startswith("#"):
                continue
            if "=" in line and not line.startswith(("http", "socks", "vless", "trojan", "ss")):
                continue
            lines.append(line)

    if not lines:
        return []

    xray_bin = None
    processes: list[Any] = []
    for i, line in enumerate(lines):
        port = BASE_SOCKS_PORT + i
        if line.startswith(("http://", "https://", "socks5://", "socks5h://")):
            proc = SimpleProxy(line)
            print(f"  ✅ Прямой прокси [{proc.cfg.name}] → {proc.socks_url}")
            processes.append(proc)
        elif line.startswith("trojan://"):
            if not xray_bin:
                xray_bin = find_xray_binary()
            if not xray_bin:
                print(f"  ⚠️  xray не найден для Trojan [{line[:35]}...]")
                continue
            cfg = parse_trojan(line, port)
            proc = XrayProcess(cfg, xray_bin)
            try:
                proc.start()
                print(f"  ✅ Trojan [{cfg.name}] → socks5://127.0.0.1:{port}")
                processes.append(proc)
            except Exception as e:
                print(f"  ❌ Trojan [{cfg.name}] не запустился: {e}")
        elif line.startswith("ss://"):
            if not xray_bin:
                xray_bin = find_xray_binary()
            if not xray_bin:
                print(f"  ⚠️  xray не найден для Shadowsocks [{line[:35]}...]")
                continue
            cfg = parse_ss(line, port)
            proc = XrayProcess(cfg, xray_bin)
            try:
                proc.start()
                print(f"  ✅ Shadowsocks [{cfg.name}] → socks5://127.0.0.1:{port}")
                processes.append(proc)
            except Exception as e:
                print(f"  ❌ Shadowsocks [{cfg.name}] не запустился: {e}")
        elif line.startswith("vless://"):
            if not xray_bin:
                xray_bin = find_xray_binary()
            if not xray_bin:
                print(f"  ⚠️  xray не найден для VLESS [{line[:35]}...]")
                continue
            cfg = parse_vless(line, port)
            proc = XrayProcess(cfg, xray_bin)
            try:
                proc.start()
                print(f"  ✅ VLESS [{cfg.name}] → socks5://127.0.0.1:{port}")
                processes.append(proc)
            except Exception as e:
                print(f"  ❌ VLESS [{cfg.name}] не запустился: {e}")
        else:
            print(f"  ⚠️  Неподдерживаемый формат прокси: {line[:35]}...")

    return processes


# ─────────────────────────────────────────────
#  Проверка пинга и фильтрация быстрых прокси
# ─────────────────────────────────────────────

async def ping_proxy_async(
    proc: Any,
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
    processes: list[Any],
    max_ping_seconds: float = 3.0,
    test_url: str = "https://api.tgmrkt.io/api/v1/gifts/collections",
) -> list[Any]:
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

    fast_proxies: list[Any] = []
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

