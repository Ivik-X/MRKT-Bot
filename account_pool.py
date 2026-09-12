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
SLOT_COOLDOWN = float(os.getenv("SLOT_COOLDOWN", 1.2))
# Кулдаун при 429 (секунды)

PENALTY_SECONDS = float(os.getenv("PENALTY_SECONDS", 15.0))


# ─────────────────────────────────────────────
#  Slot
# ─────────────────────────────────────────────

@dataclass
class Slot:
    """Один (токен + прокси) слот для API запросов."""
    token: str
    proxy: Optional[Any] = None
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

    def record_success(self) -> None:
        """Сбрасывает счетчик таймаутов при успешном запросе."""
        self.timeout_count = 0

    def record_timeout(self) -> bool:
        """
        Фиксирует таймаут.
        При 1-2 таймаутах: временный кулдаун на 10 сек.
        При 3+ таймаутах подряд: отключает слот и возвращает True (сигнал для авто-замены).
        """
        self.timeout_count += 1
        if self.timeout_count >= 3:
            self.disabled = True
            return True
        self._available_at = max(self._available_at, time.monotonic() + 10.0)
        return False

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

    def __init__(
        self,
        slots: list[Slot],
        reserve_proxies: Optional[list[Any]] = None,
        use_direct: bool = False,
    ):
        if not slots:
            raise ValueError("AccountPool: список слотов пуст")
        self._slots = slots
        self._reserve_proxies = list(reserve_proxies or [])
        self._lock = threading.RLock()
        self._async_lock = asyncio.Lock()
        self.use_direct = use_direct

    def set_use_direct(self, use_direct: bool) -> None:
        """Переключает использование прямого IP сервера для первого слота на лету."""
        with self._lock:
            if self.use_direct == use_direct:
                return
            self.use_direct = use_direct
            tokens = [s.token for s in self._slots]
            # Собираем все прокси: те, что сейчас в слотах + резервные
            all_proxies = []
            for s in self._slots:
                if s.proxy and s.proxy not in all_proxies:
                    all_proxies.append(s.proxy)
            for r in self._reserve_proxies:
                if r not in all_proxies:
                    all_proxies.append(r)

            new_slots: list[Slot] = []
            if use_direct and tokens:
                new_slots.append(Slot(token=tokens[0], proxy=None))
                for i, t in enumerate(tokens[1:]):
                    prx = all_proxies[i] if i < len(all_proxies) else None
                    new_slots.append(Slot(token=t, proxy=prx))
                self._reserve_proxies = all_proxies[len(tokens[1:]):]
            else:
                for i, t in enumerate(tokens):
                    prx = all_proxies[i] if i < len(all_proxies) else None
                    new_slots.append(Slot(token=t, proxy=prx))
                self._reserve_proxies = all_proxies[len(tokens):]

            self._slots = new_slots

    def replace_slot_proxy(self, slot: Slot) -> Optional[Any]:
        """Заменяет проблемный прокси в слоте на следующий быстрый из резерва."""
        with self._lock:
            if not self._reserve_proxies:
                return None
            old_prx = slot.proxy
            new_prx = self._reserve_proxies.pop(0)
            slot.proxy = new_prx
            slot.reset_stats()
            if old_prx:
                try:
                    old_prx.stop()
                except Exception:
                    pass
            return new_prx

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

    def get_proxies(self) -> list[Any]:
        """Возвращает список уникальных активных прокси."""
        with self._lock:
            seen = set()
            proxies = []
            for s in self._slots:
                if s.proxy and id(s.proxy) not in seen:
                    seen.add(id(s.proxy))
                    proxies.append(s.proxy)
            return proxies

    def avg_ping_ms(self) -> float | None:
        """
        Возвращает средний пинг (мс) по всем слотам, у которых есть данные о задержке.
        Используется для расчёта стартового интервала сканирования.
        """
        pings = []
        with self._lock:
            for s in self._slots:
                # Slot хранит avg_latency_ms если был измерен пинг
                lat = getattr(s, "avg_latency_ms", None) or getattr(s, "ping_ms", None)
                if lat and lat > 0:
                    pings.append(lat)
        if not pings:
            return None
        return sum(pings) / len(pings)

    @property
    def slots(self) -> list[Slot]:
        """Возвращает копию списка слотов пула."""
        with self._lock:
            return list(self._slots)

    @property
    def primary_token(self) -> Optional[str]:
        """Первый токен считается основным (с него читается баланс и делаются покупки)."""
        tokens = self.get_tokens()
        return tokens[0] if tokens else None

    def get_primary_slot(self) -> Optional[Slot]:
        """Возвращает слот основного аккаунта (с его токеном и прокси)."""
        with self._lock:
            if not self._slots:
                return None
            prim_tok = self.primary_token
            for s in self._slots:
                if s.token == prim_tok:
                    return s
            return self._slots[0]


    def set_primary_token(self, token: str) -> None:
        """Перемещает токен на 1-е место, делая его основным, сохраняет в файл и перезагружает пул."""
        tokens = self.get_tokens()
        if token in tokens:
            tokens.remove(token)
            tokens.insert(0, token)
            save_tokens(tokens)
            self.reload_tokens(tokens)

    def reload_tokens(self, new_tokens: list[str]) -> None:
        """
        Горячая перезагрузка списка токенов на лету.
        Пересобирает слоты с распределением по ВСЕМ активным прокси с учётом use_direct.
        """
        if not new_tokens:
            raise ValueError("Список токенов не может быть пустым")

        with self._lock:
            active_proxies = self.get_proxies()
            new_slots: list[Slot] = []

            if not active_proxies:
                for tok in new_tokens:
                    new_slots.append(Slot(token=tok))
            elif self.use_direct:
                for i, tok in enumerate(new_tokens):
                    if i == 0:
                        new_slots.append(Slot(token=tok, proxy=None))
                    else:
                        prx = active_proxies[(i - 1) % len(active_proxies)]
                        new_slots.append(Slot(token=tok, proxy=prx))
            else:
                for i, tok in enumerate(new_tokens):
                    prx = active_proxies[i % len(active_proxies)]
                    new_slots.append(Slot(token=tok, proxy=prx))

            self._slots = new_slots

    def stop_all_proxies(self) -> None:
        """Останавливает все xray процессы (активные и резервные)."""
        seen: set = set()
        for slot in self._slots:
            if slot.proxy and id(slot.proxy) not in seen:
                seen.add(id(slot.proxy))
                slot.proxy.stop()
        for prx in self._reserve_proxies:
            if id(prx) not in seen:
                seen.add(id(prx))
                prx.stop()

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


def load_use_direct_setting(path: str = "proxies.txt") -> bool:
    """
    Проверяет настройку использования прямого IP сервера (без VPN).
    Смотрит в proxies.txt на строчку:
      USE_DIRECT=true / false
      direct=true / false
    Поддерживает как открытые строки, так и комментарии: # USE_DIRECT=true.
    Если в файле не указано, проверяет переменную окружения USE_DIRECT (по умолчанию false).
    """
    file_to_read = None
    if os.path.isfile(path):
        file_to_read = path
    elif os.path.isdir(path):
        for candidate in sorted(os.listdir(path)):
            cand_path = os.path.join(path, candidate)
            if os.path.isfile(cand_path):
                file_to_read = cand_path
                break

    if file_to_read:
        try:
            with open(file_to_read, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    # Поддерживаем USE_DIRECT=true, direct=true, # USE_DIRECT=true
                    cleaned = line.lstrip("#").strip()
                    if "=" in cleaned:
                        k, v = cleaned.split("=", 1)
                        k = k.strip().upper()
                        v = v.strip().lower()
                        if k in ("USE_DIRECT", "DIRECT", "USE_LOCAL_IP"):
                            return v in ("1", "true", "yes")
        except Exception:
            pass

    return os.getenv("USE_DIRECT", "false").lower() in ("1", "true", "yes")


async def build_pool_async(
    tokens_file: str = "tokens.txt",
    proxies_file: str = "proxies.txt",
    max_ping_seconds: float = float(os.getenv("MAX_PING_SECONDS", 3.0)),
) -> AccountPool:
    """
    Асинхронно строит AccountPool из tokens.txt и proxies.txt.
    Запускает прокси, параллельно замеряет пинг каждого к MRKT API
    и отсеивает слишком медленные (> max_ping_seconds) или неработающие.
    Поддерживает настройку USE_DIRECT (true/false) в начале proxies.txt.
    """
    tokens = load_tokens(tokens_file)
    if not tokens:
        raise RuntimeError(
            "Нет токенов! Создай tokens.txt с токенами (по одному на строку)"
        )

    print(f"  🔑 Токенов: {len(tokens)}")

    use_direct = load_use_direct_setting(proxies_file)
    direct_status = "ВКЛ 🟢 (1 слот без VPN на прямом IP)" if use_direct else "ВЫКЛ 🔴 (все слоты строго через VPN)"
    print(f"  ⚙️ Прямой IP (USE_DIRECT): {direct_status}")

    raw_proxies = load_proxies(proxies_file)
    print(f"  🌐 Прокси запущено: {len(raw_proxies)}")

    if raw_proxies:
        proxies = await filter_fast_proxies_async(
            raw_proxies, max_ping_seconds=max_ping_seconds
        )
    else:
        proxies = []

    # Отбираем самые быстрые прокси под количество токенов
    selected_proxies: list[Any] = []
    reserve_proxies: list[Any] = []
    num_proxies_needed = max(0, len(tokens) - 1) if use_direct else len(tokens)

    if proxies:
        selected_proxies = proxies[:num_proxies_needed]
        # До 10 лучших оставшихся держим в горячем резерве для авто-замены при сбоях
        reserve_proxies = proxies[num_proxies_needed:num_proxies_needed + 10]
        for extra in proxies[num_proxies_needed + 10:]:
            print(f"  💤 Прокси [{extra.cfg.name}] остановлен (избыточный резерв)")
            extra.stop()

    slots: list[Slot] = []
    if use_direct:
        # 1-й токен (основной) идёт напрямую без VPN
        slots.append(Slot(token=tokens[0], proxy=None))
        for i, token in enumerate(tokens[1:]):
            if selected_proxies:
                prx = selected_proxies[i % len(selected_proxies)]
                slots.append(Slot(token=token, proxy=prx))
            else:
                slots.append(Slot(token=token))
    elif selected_proxies:
        for i, token in enumerate(tokens):
            prx = selected_proxies[i % len(selected_proxies)]
            slots.append(Slot(token=token, proxy=prx))
    else:
        for token in tokens:
            slots.append(Slot(token=token))

    print(f"  🎯 Распределение {len(slots)} слотов:")
    for idx, slot in enumerate(slots, 1):
        tok_mask = f"{slot.token[:8]}…{slot.token[-4:]}" if len(slot.token) > 12 else slot.token
        if slot.proxy:
            print(f"     #{idx}: {tok_mask} ⇄ [{slot.proxy.cfg.name}] ({slot.proxy.ping_ms:.0f} мс)")
        else:
            print(f"     #{idx}: {tok_mask} ⇄ [direct] (текущий прямой IP сервера, 0 мс)")

    if reserve_proxies:
        print(f"  🛡️ В горячем резерве: {len(reserve_proxies)} прокси для быстрой авто-замены при сбоях")

    return AccountPool(slots, reserve_proxies=reserve_proxies, use_direct=use_direct)


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


async def buy_gift_async(
    gift_id: str,
    price_nano: int,
    token: str,
    proxies: Optional[dict] = None,
    timeout: float = 5.0,
) -> tuple[bool, str, dict]:
    """
    Выполняет моментальную покупку подарка через POST /gifts/buy.
    Возвращает (success: bool, status_message: str, response_data: dict).
    """
    from curl_cffi.requests import AsyncSession

    url = "https://api.tgmrkt.io/api/v1/gifts/buy"
    headers = {
        "Authorization": token,
        "Cookie": f"access_token={token}",
        "Origin": "https://cdn.tgmrkt.io",
        "Referer": "https://cdn.tgmrkt.io/",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
    }
    payload = {
        "ids": [gift_id],
        "prices": {
            gift_id: int(price_nano)
        }
    }

    try:
        async with AsyncSession(impersonate="chrome124", proxies=proxies) as session:
            resp = await session.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                item = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
                return True, "Подарок успешно куплен", item
            elif resp.status_code == 400:
                err_text = resp.text
                try:
                    err_json = resp.json()
                    msg = err_json.get("message") or err_json.get("error") or err_text
                except Exception:
                    msg = err_text
                if "balance" in str(msg).lower():
                    reason = "Недостаточно средств на балансе маркета"
                elif "price" in str(msg).lower():
                    reason = "Цена лота изменилась"
                else:
                    reason = f"Ошибка 400: {str(msg)[:80]}"
                return False, reason, {}
            elif resp.status_code in (404, 409):
                return False, "Лот уже выкуплен другим пользователем или снят с продажи", {}
            elif resp.status_code == 401:
                return False, "401 Unauthorized (токен авторизации протух)", {}
            elif resp.status_code == 429:
                return False, "429 Too Many Requests (рейтлимит)", {}
            else:
                return False, f"HTTP {resp.status_code}: {resp.text[:80]}", {}
    except Exception as e:
        err_msg = str(e) or e.__class__.__name__
        if "timeout" in err_msg.lower():
            err_msg = "Таймаут запроса покупки"
        return False, err_msg, {}


async def verify_gift_in_vault_async(
    gift_id: str,
    token: str,
    proxies: Optional[dict] = None,
    timeout: float = 4.0,
) -> bool:
    """
    Проверяет наличие подарка в Хранилище (инвентаре) пользователя через POST /gifts.
    """
    from curl_cffi.requests import AsyncSession

    url = "https://api.tgmrkt.io/api/v1/gifts"
    headers = {
        "Authorization": token,
        "Cookie": f"access_token={token}",
        "Origin": "https://cdn.tgmrkt.io",
        "Referer": "https://cdn.tgmrkt.io/",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
    }
    payload = {
        "isListed": False,
        "count": 20,
        "cursor": "",
    }
    try:
        async with AsyncSession(impersonate="chrome124", proxies=proxies) as session:
            resp = await session.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                gifts = data.get("gifts", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                for g in gifts:
                    if g.get("id") == gift_id:
                        return True
    except Exception:
        pass
    return False


async def find_recent_cheap_buys_async(
    pool: AccountPool,
    max_price_nano: int,
    limit: int = 5,
    max_pages: int = 20,
) -> list[dict]:
    """
    Опрашивает ленту /feed через слоты пула с учетом задержек и кулдауна.
    Ищет последние завершенные сделки (пары listing + sale) дешевле max_price_nano,
    исключая lucky_buy, и вычисляет точное время выкупа в миллисекундах.
    """
    from datetime import datetime
    from curl_cffi.requests import AsyncSession

    cursor = None
    matched: list[dict] = []
    events_by_gift: dict[str, dict[str, Any]] = {}
    matched_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        slot = await pool.next_async()
        payload: dict[str, Any] = {
            "count": 20,
            "type": ["sale", "listing"],
            "maxPrice": max_price_nano,
        }
        if cursor:
            payload["cursor"] = cursor

        try:
            async with AsyncSession(impersonate="chrome124", proxies=slot.proxies) as session:
                r = await session.post(
                    "https://api.tgmrkt.io/api/v1/feed",
                    headers=slot.headers,
                    json=payload,
                    timeout=7.0,
                )
                if r.status_code == 429:
                    pool.penalize(slot, 15.0)
                    await asyncio.sleep(1.0)
                    continue

                if r.status_code != 200:
                    break

                data = r.json()
                items = data.get("items", []) if isinstance(data, dict) else []
                cursor = data.get("cursor") if isinstance(data, dict) else None

                if not items:
                    break

                for item in items:
                    itype = item.get("type")
                    gift = item.get("gift", {})
                    if not gift or gift.get("luckyBuy") or itype not in ("sale", "listing"):
                        continue
                    gid = gift.get("id")
                    if not gid or gid in matched_ids:
                        continue

                    if gid not in events_by_gift:
                        events_by_gift[gid] = {}
                    events_by_gift[gid][itype] = item

                    # Если найдены и listing, и sale для одного подарка
                    if "sale" in events_by_gift[gid] and "listing" in events_by_gift[gid]:
                        s = events_by_gift[gid]["sale"]
                        l = events_by_gift[gid]["listing"]
                        try:
                            s_date = datetime.fromisoformat(s["date"].replace("Z", "+00:00"))
                            l_date = datetime.fromisoformat(l["date"].replace("Z", "+00:00"))
                            delta_ms = max(0, int((s_date - l_date).total_seconds() * 1000))
                        except Exception:
                            delta_ms = 0
                            s_date = None
                            l_date = None

                        matched.append({
                            "gift_id": gid,
                            "gift": s.get("gift", {}),
                            "amount": s.get("amount", 0),
                            "delta_ms": delta_ms,
                            "sale_date": s_date,
                            "listing_date": l_date,
                        })
                        matched_ids.add(gid)
                        if len(matched) >= limit:
                            break

                if len(matched) >= limit:
                    break

                if not cursor:
                    break

        except Exception:
            pass

        # Пауза между страницами для защиты от 429
        await asyncio.sleep(0.4)

    return matched[:limit]




