"""
request_controller.py — Централизованный контроллер и диспетчер HTTP-запросов к MRKT API.

Задачи контроллера:
1. Строгий контроль таймингов:
   - Глобальный интервал между сканами (pacing).
   - Индивидуальный кулдаун на каждый слот (не чаще 1 раза в 1.5-2.0 сек на один IP/аккаунт).
2. Защита от каскадных 429:
   - При 429 вводится пауза перед переходом к следующему слоту, предотвращающая
     лавинообразный вылет всех слотов за 1 секунду.
3. Полная изоляция Cookies и сессий:
   - Использование discard_cookies=True предотвращает утечку защитных Cloudflare-кук
     между разными прокси и Telegram-аккаунтами.
4. Приоритетная маршрутизация:
   - Обычные сканы (NORMAL) соблюдают темп и очередь.
   - Запросы покупки (HIGH) выполняются немедленно через основной слот без задержек.
5. Интеграция с Telegram-уведомлениями обо всех сбоях (с дебаунсом от спама).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import Any, Callable, Coroutine, Optional

from curl_cffi.requests import AsyncSession

log = logging.getLogger("mrkt.controller")

MARKET_API_URL = "https://api.tgmrkt.io/api/v1"
PENALTY_429_DEFAULT = 15.0
DEFAULT_REQUEST_TIMEOUT = 3.5


class RequestPriority(enum.Enum):
    NORMAL = 1  # Сканирование листинга, проверка флоров
    HIGH = 2    # Моментальная покупка (AutoBuy)


class RequestController:
    """
    Контроллер запросов к MRKT API с гарантированным соблюдением пауз,
    защитой от каскадных 429 и изоляцией сессий между слотами.
    """

    def __init__(
        self,
        pool: Any,
        scanner_state: Any = None,
        min_global_interval: float = 0.65,
        slot_cooldown: float = 1.5,
        anti_cascade_delay: float = 0.8,
        error_notifier: Optional[Callable[[str, str], Coroutine[Any, Any, None]]] = None,
    ):
        self.pool = pool
        self.scanner_state = scanner_state
        self.min_global_interval = min_global_interval
        self.slot_cooldown = slot_cooldown
        self.anti_cascade_delay = anti_cascade_delay
        self.error_notifier = error_notifier

        self._lock = asyncio.Lock()
        self._last_global_req_time: float = 0.0
        self._last_429_time: float = 0.0

    def set_error_notifier(
        self,
        notifier: Callable[[str, str], Coroutine[Any, Any, None]]
    ) -> None:
        self.error_notifier = notifier

    async def _notify_error(self, error_type: str, details: str) -> None:
        """Отправляет уведомление об ошибке в Telegram через callback, если он задан."""
        if self.error_notifier:
            try:
                await self.error_notifier(error_type, details)
            except Exception as e:
                log.debug("Не удалось отправить алерт об ошибке: %s", e)

    async def request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[dict] = None,
        priority: RequestPriority = RequestPriority.NORMAL,
        slot: Optional[Any] = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        max_attempts: int = 4,
    ) -> Any:
        """
        Выполняет запрос с выдержкой таймингов, ротацией слотов и защитой от 429.
        """
        method_upper = method.upper()
        url = f"{MARKET_API_URL}{endpoint}"

        # ── Запросы высокой важности (AutoBuy) идут без ожидания очереди сканера ──
        if priority == RequestPriority.HIGH:
            target_slot = slot or (self.pool.get_primary_slot() if self.pool else None)
            if not target_slot:
                raise RuntimeError("RequestController: нет доступного слота для приоритетного запроса")
            return await self._execute_http(
                method_upper, url, endpoint, target_slot, json_data, timeout
            )

        # ── Обычные запросы: контроль пауз и ротация ──
        last_exc: Optional[Exception] = None

        for attempt in range(max_attempts):
            async with self._lock:
                # 1. Выдерживаем глобальный интервал между запросами
                target_interval = (
                    self.scanner_state.scan_interval
                    if self.scanner_state and getattr(self.scanner_state, "scan_interval", None)
                    else self.min_global_interval
                )
                target_interval = max(self.min_global_interval, target_interval)

                now = time.monotonic()
                elapsed_since_last = now - self._last_global_req_time
                if elapsed_since_last < target_interval:
                    sleep_time = target_interval - elapsed_since_last
                    await asyncio.sleep(sleep_time)

                # 2. Получаем следующий доступный слот из пула
                if slot is not None and attempt == 0:
                    current_slot = slot
                else:
                    current_slot = await self.pool.next_async()

                # 3. Проверяем индивидуальный кулдаун слота
                slot_avail = getattr(current_slot, "available_at", 0.0)
                now = time.monotonic()
                if slot_avail > now:
                    wait_slot = slot_avail - now
                    if wait_slot > 0:
                        await asyncio.sleep(min(wait_slot, 2.0))

                self._last_global_req_time = time.monotonic()

            # 4. Выполняем HTTP-запрос
            t0 = time.monotonic()
            try:
                result = await self._execute_http(
                    method_upper, url, endpoint, current_slot, json_data, timeout
                )
                # Успех — сбрасываем таймауты слота
                if hasattr(current_slot, "record_success"):
                    current_slot.record_success()
                return result

            except asyncio.CancelledError:
                raise

            except Exception as e:
                elapsed = time.monotonic() - t0
                err_str = str(e)
                err_name = type(e).__name__
                last_exc = e

                # ── Обработка 429 Too Many Requests ──
                if "429" in err_str:
                    log.warning("429 | слот: %s | попытка %d/%d", getattr(current_slot, "label", "unknown"), attempt + 1, max_attempts)
                    if hasattr(self.pool, "penalize"):
                        self.pool.penalize(current_slot, PENALTY_429_DEFAULT)

                    if self.scanner_state is not None:
                        if hasattr(self.scanner_state, "record_429"):
                            self.scanner_state.record_429()
                        adaptor = getattr(self.scanner_state, "rate_adaptor", None)
                        if adaptor is not None:
                            adaptor.on_429(PENALTY_429_DEFAULT)
                            self.scanner_state.scan_interval = adaptor.interval

                    # АНТИ-ЛАВИНА: пауза перед следующим слотом, чтобы не сжечь весь пул!
                    await asyncio.sleep(self.anti_cascade_delay)
                    continue

                # ── Обработка 401 Unauthorized ──
                if "401" in err_str:
                    log.error("401 | Токен просрочен (слот: %s)", getattr(current_slot, "label", "unknown"))
                    current_slot.disabled = True
                    await self._notify_error("HTTP 401 Unauthorized", f"Токен в слоте [{getattr(current_slot, 'label', '')}] просрочен")
                    continue

                # ── Обработка сетевых ошибок / сбоев прокси / таймаутов ──
                is_timeout_or_net = (
                    "Timeout" in err_name
                    or "timed out" in err_str.lower()
                    or "Proxy" in err_name
                    or "Connection" in err_name
                    or "Certificate" in err_name
                    or "curl: (28)" in err_str
                    or "curl: (7)" in err_str
                    or "Resolving" in err_str
                )

                if is_timeout_or_net:
                    if self.scanner_state and hasattr(self.scanner_state, "record_proxy_failure"):
                        self.scanner_state.record_proxy_failure()

                    if getattr(current_slot, "proxy", None) is None:
                        # Прямой IP не работает (таймаут DNS/сети/блокировка провайдера)
                        log.warning(
                            "⚠️ Слот [%s] на прямом IP не отвечает (%s). Автоматически отключаем direct и переводим слот на VPN/прокси.",
                            current_slot.token[:8],
                            err_str,
                        )
                        if hasattr(self.pool, "set_use_direct"):
                            self.pool.set_use_direct(False)
                        if self.scanner_state:
                            self.scanner_state.use_direct = False
                            try:
                                from settings_manager import save_settings
                                save_settings(self.scanner_state)
                            except Exception:
                                pass
                        if hasattr(self.pool, "replace_slot_proxy"):
                            new_prx = self.pool.replace_slot_proxy(current_slot)
                            if new_prx:
                                prx_name = getattr(new_prx.cfg, "name", "proxy")
                                log.warning("⚠️ Слот [%s] переведён с direct на резервный прокси [%s]", current_slot.token[:8], prx_name)
                    else:
                        needs_replace = False
                        if hasattr(current_slot, "record_timeout"):
                            needs_replace = current_slot.record_timeout()

                        if needs_replace and hasattr(self.pool, "replace_slot_proxy"):
                            new_prx = self.pool.replace_slot_proxy(current_slot)
                            if new_prx:
                                prx_name = getattr(new_prx.cfg, "name", "proxy")
                                log.warning("⚠️ Слот [%s] сбой прокси — заменён на [%s]", current_slot.token[:8], prx_name)
                            else:
                                log.warning("⚠️ Слот [%s] сбой прокси — резерв пуст", getattr(current_slot, "label", ""))

                log.warning("Ошибка %s %s | %s | %.2fс | %s", method_upper, endpoint, getattr(current_slot, "label", ""), elapsed, e)
                # Тактическая микропауза перед следующей попыткой
                await asyncio.sleep(0.3)

        if last_exc:
            is_net = any(k in type(last_exc).__name__ or k in str(last_exc).lower() for k in ("timeout", "proxy", "connection", "curl: (28)", "curl: (7)"))
            if not is_net:
                await self._notify_error(f"Сбой {method_upper} {endpoint}", f"Все {max_attempts} попыток исчерпаны:\n{str(last_exc)[:200]}")
            raise last_exc
        raise RuntimeError(f"RequestController: все попытки исчерпаны: {method_upper} {endpoint}")

    async def _execute_http(
        self,
        method: str,
        url: str,
        endpoint: str,
        slot: Any,
        json_data: Optional[dict],
        timeout: float,
    ) -> Any:
        """
        Выполняет один низкоуровневый HTTP запрос через curl_cffi.
        ВАЖНО: discard_cookies=True исключает утечку Cloudflare кук между слотами.
        """
        proxies = getattr(slot, "proxies", None)
        headers = getattr(slot, "headers", {})

        async with AsyncSession(impersonate="chrome124", proxies=proxies) as session:
            if method == "GET":
                resp = await session.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    discard_cookies=True,
                )
            else:
                resp = await session.post(
                    url,
                    json=json_data or {},
                    headers=headers,
                    timeout=timeout,
                    discard_cookies=True,
                )

            if resp.status_code == 429:
                raise RuntimeError(f"HTTP 429 Too Many Requests ({endpoint})")
            if resp.status_code == 401:
                raise RuntimeError(f"HTTP 401 Unauthorized ({endpoint})")

            resp.raise_for_status()

            # Парсинг JSON ответа
            try:
                return resp.json()
            except Exception:
                return resp.text

    async def get(self, endpoint: str, **kwargs: Any) -> Any:
        return await self.request("GET", endpoint, **kwargs)

    async def post(self, endpoint: str, json_data: Optional[dict] = None, **kwargs: Any) -> Any:
        return await self.request("POST", endpoint, json_data=json_data, **kwargs)
