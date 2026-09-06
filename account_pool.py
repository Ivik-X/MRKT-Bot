"""
account_pool.py — пул (токен, прокси) слотов.

Стратегия: LRU (Least Recently Used) — берём слот который дольше всего
не использовался. Это гарантирует равномерное распределение и не долбит
один токен несколько раз подряд.

Штрафование (penalize): при 429 слот уходит в «кулдаун» на N секунд.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from xray_proxy import XrayProcess, load_proxies, filter_fast_proxies_async


# Минимальный интервал между запросами через один слот (секунды)
SLOT_COOLDOWN = float(os.getenv("SLOT_COOLDOWN", 0.5))
# Кулдаун при 429 (секунды)
PENALTY_SECONDS = float(os.getenv("PENALTY_SECONDS", 60.0))


# ─────────────────────────────────────────────
#  Slot
# ─────────────────────────────────────────────

@dataclass
class Slot:
    """Один (токен + прокси) слот для API запросов."""
    token: str
    proxy: Optional[XrayProcess] = None
    disabled: bool = False
    timeout_count: int = 0
    # Время когда слот снова доступен (monotonic)
    _available_at: float = field(default=0.0, init=False, repr=False)

    @property
    def headers(self) -> dict:
        return {
            "Authorization": self.token,
            "Cookie": f"access_token={self.token}",
            "Referer": "https://cdn.tgmrkt.io/",
            "Origin": "https://cdn.tgmrkt.io",
            "Content-Type": "application/json",
        }

    @property
    def proxies(self) -> Optional[dict]:
        if self.proxy and self.proxy.alive():
            url = self.proxy.socks_url
            return {"http": url, "https": url}
        return None

    @property
    def label(self) -> str:
        tok = self.token[:8] + "…"
        prx = self.proxy.cfg.name if self.proxy else "direct"
        status = " [отключён >1.5с]" if self.disabled else ""
        return f"{tok} @ {prx}{status}"

    def mark_used(self) -> None:
        """Помечаем слот использованным — следующий раз не раньше чем через SLOT_COOLDOWN."""
        self._available_at = time.monotonic() + SLOT_COOLDOWN

    def penalize(self, seconds: float = PENALTY_SECONDS) -> None:
        """429 — запрещаем слот на N секунд."""
        self._available_at = time.monotonic() + seconds

    def record_timeout(self) -> None:
        """Таймаут или ответ >1.5с — отключение слота."""
        self.timeout_count += 1
        if self.timeout_count >= 2:
            self.disabled = True

    def reset_stats(self) -> None:
        self.timeout_count = 0
        self.disabled = False

    @property
    def available_at(self) -> float:
        return self._available_at


# ─────────────────────────────────────────────
#  AccountPool
# ─────────────────────────────────────────────

class AccountPool:
    """
    LRU пул слотов. Thread-safe и Async-safe.
    Всегда выдаёт доступный активный слот (LRU).
    Пропускает disabled (тормозящие) слоты.
    """

    def __init__(self, slots: list[Slot]):
        if not slots:
            raise ValueError("AccountPool: список слотов пуст")
        self._slots = slots
        self._lock = threading.Lock()
        self._async_lock = asyncio.Lock()

    async def next_async(self) -> Slot:
        """
        Асинхронно выдаёт следующий доступный активный слот (LRU).
        Если слот на кулдауне — await asyncio.sleep() без блокировки потока.
        """
        async with self._async_lock:
            active = [s for s in self._slots if not s.disabled]
            if not active:
                for s in self._slots:
                    s.reset_stats()
                active = self._slots

            now = time.monotonic()
            best = min(active, key=lambda s: s.available_at)
            wait = best.available_at - now
            if wait > 0:
                await asyncio.sleep(wait)
            best.mark_used()
            return best

    def next(self) -> Slot:
        """Синхронный метод получения слота (LRU)."""
        with self._lock:
            active = [s for s in self._slots if not s.disabled]
            if not active:
                for s in self._slots:
                    s.reset_stats()
                active = self._slots

            now = time.monotonic()
            best = min(active, key=lambda s: s.available_at)
            wait = best.available_at - now
            if wait > 0:
                time.sleep(wait)
            best.mark_used()
            return best

    def penalize(self, slot: Slot, seconds: float = PENALTY_SECONDS) -> None:
        """Штрафуем слот за 429."""
        slot.penalize(seconds)

    def get_tokens(self) -> list[str]:
        """Возвращает список уникальных токенов в пуле."""
        with self._lock:
            seen = set()
            tokens = []
            for s in self._slots:
                if s.token not in seen:
                    seen.add(s.token)
                    tokens.append(s.token)
            return tokens

    def get_proxies(self) -> list[XrayProcess]:
        """Возвращает список уникальных активных прокси."""
        with self._lock:
            seen = set()
            proxies = []
            for s in self._slots:
                if s.proxy and id(s.proxy) not in seen:
                    seen.add(id(s.proxy))
                    proxies.append(s.proxy)
            return proxies

    def reload_tokens(self, new_tokens: list[str]) -> None:
        """
        Горячая перезагрузка списка токенов на лету.
        Пересобирает слоты с сохранением текущих активных прокси.
        """
        if not new_tokens:
            raise ValueError("Список токенов не может быть пустым")

        with self._lock:
            active_proxies = self.get_proxies()
            new_slots: list[Slot] = []

            if active_proxies:
                proxy_cycle = itertools.cycle(active_proxies)
                for tok in new_tokens:
                    new_slots.append(Slot(token=tok, proxy=next(proxy_cycle)))
            else:
                for tok in new_tokens:
                    new_slots.append(Slot(token=tok))

            self._slots = new_slots

    def stop_all_proxies(self) -> None:
        """Останавливает все xray процессы."""
        seen: set = set()
        for slot in self._slots:
            if slot.proxy and id(slot.proxy) not in seen:
                seen.add(id(slot.proxy))
                slot.proxy.stop()

    def __len__(self) -> int:
        return len(self._slots)

    def __repr__(self) -> str:
        active = sum(1 for s in self._slots if not s.disabled)
        direct = sum(1 for s in self._slots if not s.proxy)
        proxied = len(self._slots) - direct
        return f"AccountPool({len(self._slots)} слотов [{active} активных]: {direct} direct, {proxied} через прокси)"


# ─────────────────────────────────────────────
#  Инициализация пула
# ─────────────────────────────────────────────

def load_tokens(path: str = "tokens.txt") -> list[str]:
    """
    Читает токены из tokens.txt (один токен на строку).
    Если файл не найден — берёт MRKT_TOKEN из .env как fallback.
    """
    tokens: list[str] = []

    file_to_read = None
    if os.path.isfile(path):
        file_to_read = path
    elif os.path.isdir(path):
        for candidate in sorted(os.listdir(path)):
            candidate_path = os.path.join(path, candidate)
            if os.path.isfile(candidate_path):
                file_to_read = candidate_path
                break

    if file_to_read:
        with open(file_to_read, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    tokens.append(line)

    if not tokens:
        env_token = os.getenv("MRKT_TOKEN", "")
        if env_token and env_token != "your_token_here":
            tokens.append(env_token)

    return tokens


async def build_pool_async(
    tokens_file: str = "tokens.txt",
    proxies_file: str = "proxies.txt",
    max_ping_seconds: float = float(os.getenv("MAX_PING_SECONDS", 3.0)),
) -> AccountPool:
    """
    Асинхронно строит AccountPool из tokens.txt и proxies.txt.
    Запускает прокси, параллельно замеряет пинг каждого к MRKT API
    и отсеивает слишком медленные (> max_ping_seconds) или неработающие.
    """
    tokens = load_tokens(tokens_file)
    if not tokens:
        raise RuntimeError(
            "Нет токенов! Создай tokens.txt с токенами (по одному на строку)"
        )

    print(f"  🔑 Токенов: {len(tokens)}")

    raw_proxies = load_proxies(proxies_file)
    print(f"  🌐 Прокси запущено: {len(raw_proxies)}")

    if raw_proxies:
        proxies = await filter_fast_proxies_async(
            raw_proxies, max_ping_seconds=max_ping_seconds
        )
    else:
        proxies = []

    slots: list[Slot] = []
    if proxies:
        proxy_cycle = itertools.cycle(proxies)
        for token in tokens:
            slots.append(Slot(token=token, proxy=next(proxy_cycle)))
    else:
        for token in tokens:
            slots.append(Slot(token=token))

    return AccountPool(slots)


def build_pool(
    tokens_file: str = "tokens.txt",
    proxies_file: str = "proxies.txt",
    max_ping_seconds: float = float(os.getenv("MAX_PING_SECONDS", 3.0)),
) -> AccountPool:
    """
    Синхронная обёртка для build_pool_async.
    """
    return asyncio.run(
        build_pool_async(
            tokens_file=tokens_file,
            proxies_file=proxies_file,
            max_ping_seconds=max_ping_seconds,
        )
    )


def save_tokens(tokens: list[str], path: str = "tokens.txt") -> None:
    """Сохраняет токены в файл tokens.txt."""
    with open(path, "w", encoding="utf-8") as f:
        for t in tokens:
            t = t.strip()
            if t:
                f.write(f"{t}\n")


async def verify_token_async(
    token: str,
    proxy: Optional[XrayProcess] = None,
    timeout: float = 4.0,
) -> tuple[bool, str, dict]:
    """
    Проверяет валидность токена через GET /balance к MRKT API.
    Возвращает (is_valid: bool, status_msg: str, balance_data: dict).
    """
    from curl_cffi.requests import AsyncSession

    headers = {
        "Authorization": token,
        "Cookie": f"access_token={token}",
        "Origin": "https://cdn.tgmrkt.io",
        "Referer": "https://cdn.tgmrkt.io/",
        "Accept": "application/json, text/plain, */*",
    }
    proxies = None
    if proxy and proxy.alive():
        url = proxy.socks_url
        proxies = {"http": url, "https": url}

    try:
        async with AsyncSession(impersonate="chrome124", proxies=proxies) as session:
            resp = await session.get(
                "https://api.tgmrkt.io/api/v1/balance",
                headers=headers,
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                hard_nano = data.get("hard", 0)
                ton_bal = hard_nano / 1e9
                return True, f"{ton_bal:.2f} TON", data
            elif resp.status_code == 401:
                return False, "401 Unauthorized (токен протух)", {}
            elif resp.status_code == 429:
                return False, "429 Too Many Requests", {}
            else:
                return False, f"HTTP {resp.status_code}", {}
    except Exception as e:
        err_msg = str(e) or e.__class__.__name__
        if "timeout" in err_msg.lower():
            err_msg = "Таймаут соединения"
        return False, err_msg, {}


