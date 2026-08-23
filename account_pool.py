"""
account_pool.py — пул (токен, прокси) слотов.

Стратегия: LRU (Least Recently Used) — берём слот который дольше всего
не использовался. Это гарантирует равномерное распределение и не долбит
один токен несколько раз подряд.

Штрафование (penalize): при 429 слот уходит в «кулдаун» на N секунд.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from xray_proxy import XrayProcess, load_proxies


# Минимальный интервал между запросами через один слот (секунды)
SLOT_COOLDOWN = float(os.getenv("SLOT_COOLDOWN", 5.0))
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
        return f"{tok} @ {prx}"

    def mark_used(self) -> None:
        """Помечаем слот использованным — следующий раз не раньше чем через SLOT_COOLDOWN."""
        self._available_at = time.monotonic() + SLOT_COOLDOWN

    def penalize(self, seconds: float = PENALTY_SECONDS) -> None:
        """429 — запрещаем слот на N секунд."""
        self._available_at = time.monotonic() + seconds

    @property
    def available_at(self) -> float:
        return self._available_at


# ─────────────────────────────────────────────
#  AccountPool
# ─────────────────────────────────────────────

class AccountPool:
    """
    LRU пул слотов. Thread-safe.
    Всегда выдаёт слот с самым ранним временем доступности.
    Если все слоты на кулдауне — ждёт самый быстрый.
    """

    def __init__(self, slots: list[Slot]):
        if not slots:
            raise ValueError("AccountPool: список слотов пуст")
        self._slots = slots
        self._lock = threading.Lock()

    def next(self) -> Slot:
        """
        Возвращает следующий доступный слот (LRU).
        Если все на кулдауне — ждёт самый «быстрый».
        """
        with self._lock:
            now = time.monotonic()
            # Сортируем по времени доступности
            best = min(self._slots, key=lambda s: s.available_at)
            wait = best.available_at - now
            if wait > 0:
                time.sleep(wait)
            best.mark_used()
            return best

    def penalize(self, slot: Slot, seconds: float = PENALTY_SECONDS) -> None:
        """Штрафуем слот за 429."""
        slot.penalize(seconds)

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
        direct = sum(1 for s in self._slots if not s.proxy)
        proxied = len(self._slots) - direct
        return f"AccountPool({len(self._slots)} слотов: {direct} direct, {proxied} через прокси)"


# ─────────────────────────────────────────────
#  Инициализация пула
# ─────────────────────────────────────────────

def load_tokens(path: str = "tokens.txt") -> list[str]:
    """
    Читает токены из tokens.txt (один токен на строку).
    Если файл не найден — берёт MRKT_TOKEN из .env как fallback.
    """
    tokens: list[str] = []

    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    tokens.append(line)

    if not tokens:
        env_token = os.getenv("MRKT_TOKEN", "")
        if env_token and env_token != "your_token_here":
            tokens.append(env_token)

    return tokens


def build_pool(
    tokens_file: str = "tokens.txt",
    proxies_file: str = "proxies.txt",
) -> AccountPool:
    """
    Строит AccountPool из tokens.txt и proxies.txt.

    Логика:
      - Если прокси нет: каждый токен → direct слот
      - Если прокси есть: токены + прокси зипуются по кругу
        Пример: 3 токена, 2 прокси → [t1@p1, t2@p2, t3@p1]
    """
    tokens = load_tokens(tokens_file)
    if not tokens:
        raise RuntimeError(
            "Нет токенов! Создай tokens.txt с токенами (по одному на строку)"
        )

    print(f"  🔑 Токенов: {len(tokens)}")

    proxies = load_proxies(proxies_file)
    print(f"  🌐 Прокси:  {len(proxies)}")

    slots: list[Slot] = []
    if proxies:
        proxy_cycle = itertools.cycle(proxies)
        for token in tokens:
            slots.append(Slot(token=token, proxy=next(proxy_cycle)))
    else:
        for token in tokens:
            slots.append(Slot(token=token))

    return AccountPool(slots)
