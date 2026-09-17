"""
tg_bot.py — Управление MRKT-сканером через Telegram-бота (aiogram 3).

Функционал:
  - Просмотр и редактирование токенов (добавление, замена, удаление 401).
  - Проверка баланса и статуса каждого токена через MRKT API.
  - Изменение порогов выгоды и интервала сканирования на лету.
  - Статистика, пауза/запуск сканера, просмотр пинга прокси.
  - Отправка мгновенных алертов о найденных подарках.
  - Просмотр логов по временному диапазону.
"""

from __future__ import annotations

import asyncio
import collections
import html
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from account_pool import (
    AccountPool,
    save_tokens,
    verify_token_async,
    buy_gift_async,
    verify_gift_in_vault_async,
    find_recent_cheap_buys_async,
    find_recent_filter_buys_async,
)
from settings_manager import save_settings
from xray_proxy import ping_proxy_async

log = logging.getLogger("mrkt.tg_bot")


# ─────────────────────────────────────────────
#  Shared Scanner State
# ─────────────────────────────────────────────

@dataclass
class ScannerState:
    """Общее состояние сканера для управления из Telegram."""
    pool: Optional[AccountPool] = None
    is_paused: bool = False
    auto_buy: bool = False
    min_ton_diff: float = 2.5
    cheap_price_threshold: float = 3.0
    min_margin_pct: float = 5.0
    eval_mode: str = "tiered"
    scan_interval: float = 0.8
    scans_count: int = 0
    deals_count: int = 0
    start_time: float = field(default_factory=time.monotonic)
    black_floor_nano: Optional[int] = None
    collection_floors: dict[str, int] = field(default_factory=dict)
    collection_floors_count: int = 0           # Заменяет model_floors_count
    force_refresh_floors: bool = False          # Заменяет force_refresh_models
    last_deal: Optional[dict] = None
    primary_balance_nano: Optional[int] = None
    filter_by_balance: bool = False
    max_gift_price_ton: float = 0.0
    min_turnover_ratio: float = 0.0
    collection_volumes: dict[str, int] = field(default_factory=dict)
    notify_categories: dict[str, str] = field(default_factory=lambda: {
        "BLACK":  "autobuy",
        "CHEAP":  "autobuy",
        "NFT":    "autobuy",
        "LOW_ID": "autobuy",
    })
    vault: list[dict] = field(default_factory=list)
    # Хранилище отправленных алертов для пометки выкупленных
    # {gift_id: {"sent_at": float, "messages": {admin_id: msg_id}, "text": str, "nft_url": str, "older_ids": set}}
    sent_alerts: dict[str, dict[str, Any]] = field(default_factory=dict)
    sold_queue: list[str] = field(default_factory=list)  # gift_id выкупленных лотов
    buying_in_progress: set[str] = field(default_factory=set)  # gift_id покупаемых сейчас
    rate_adaptor: Optional[Any] = None  # RateAdaptor из scanner.py
    penalties_429: list[float] = field(default_factory=list)  # таймстампы 429 за последний час
    use_direct: bool = False  # 1 слот без VPN на прямом IP сервера
    proxy_failures: list[float] = field(default_factory=list)  # таймстампы сбоев прокси за последние 2 часа
    is_analyzing_feed: bool = False  # Флаг режима анализа ленты
    feed_analysis_cancel: Optional[asyncio.Event] = None  # Сигнал отмены анализа ленты

    def get_category_mode(self, cat: str) -> str:
        """Возвращает режим категории: 'autobuy' | 'notify' | 'off'."""
        val = self.notify_categories.get(cat, "autobuy")
        if isinstance(val, bool):
            return "autobuy" if val else "off"
        val_str = str(val).lower()
        if val_str in ("autobuy", "notify", "off"):
            return val_str
        return "autobuy"

    def record_proxy_failure(self, ts: Optional[float] = None) -> None:
        """Регистрирует факт сбоя прокси и очищает записи старше 2 часов."""
        now = ts or time.time()
        self.proxy_failures.append(now)
        cutoff = now - 7200.0
        self.proxy_failures = [t for t in self.proxy_failures if t >= cutoff]

    def get_proxy_failures_stats(self) -> tuple[int, Optional[str]]:
        """
        Возвращает (количество сбоев за последние 2 часа, время последнего сбоя в HH:MM:SS или None).
        """
        now = time.time()
        cutoff = now - 7200.0
        self.proxy_failures = [t for t in self.proxy_failures if t >= cutoff]
        if not self.proxy_failures:
            return 0, None
        last_ts = self.proxy_failures[-1]
        last_str = datetime.fromtimestamp(last_ts).strftime("%H:%M:%S")
        return len(self.proxy_failures), last_str

    def record_429(self, ts: Optional[float] = None) -> None:
        """Регистрирует факт получения 429 и очищает записи старше 1 часа."""
        now = ts or time.time()
        self.penalties_429.append(now)
        cutoff = now - 3600.0
        self.penalties_429 = [t for t in self.penalties_429 if t >= cutoff]

    def get_429_count_last_hour(self) -> int:
        """Возвращает количество штрафов 429 за последние 60 минут."""
        now = time.time()
        cutoff = now - 3600.0
        self.penalties_429 = [t for t in self.penalties_429 if t >= cutoff]
        return len(self.penalties_429)

    def uptime_str(self) -> str:
        elapsed = int(time.monotonic() - self.start_time)
        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        secs = elapsed % 60
        if hours > 0:
            return f"{hours}ч {minutes}м"
        return f"{minutes}м {secs}с"



# ─────────────────────────────────────────────
#  FSM States
# ─────────────────────────────────────────────

class BotStates(StatesGroup):
    waiting_for_add_token = State()
    waiting_for_replace_tokens = State()
    waiting_for_min_ton_diff = State()
    waiting_for_min_margin = State()
    waiting_for_cheap_threshold = State()
    waiting_for_max_price = State()
    waiting_for_scan_interval = State()
    waiting_for_turnover_ratio = State()
    waiting_for_log_time = State()
    waiting_for_custom_proxies = State()
    waiting_for_feed_analysis_pages = State()
    waiting_for_full_history_pages = State()


# ─────────────────────────────────────────────
#  Вспомогательные функции
# ─────────────────────────────────────────────

def _mask_token(token: str) -> str:
    """Маскирует UUID токен: bf73c16d...ddbcf3a2."""
    token = token.strip()
    if len(token) > 16:
        return f"{token[:8]}…{token[-8:]}"
    return token


def get_tokens_updated_str(path: str = "tokens.txt") -> str:
    """Возвращает дату и время последнего изменения файла tokens.txt."""
    try:
        if os.path.isfile(path):
            mtime = os.path.getmtime(path)
            return datetime.fromtimestamp(mtime).strftime("%d.%m %H:%M")
    except Exception:
        pass
    return "неизвестно"


def make_telegram_nft_url(collection_name: str, number: Any) -> str:
    """
    Генерирует официальную ссылку Telegram NFT вида:
    https://t.me/nft/CandyCane-79154
    """
    if not collection_name or number is None:
        return "https://t.me/nft"
    cleaned = str(collection_name).replace("'", "")
    words = re.findall(r"[A-Za-z0-9]+", cleaned)
    slug = "".join(w.capitalize() for w in words)
    if not slug:
        return "https://t.me/nft"
    return f"https://t.me/nft/{slug}-{number}"


def _extract_uuid_tokens(text: str) -> list[str]:
    """Извлекает валидные UUID токены из любого текста."""
    pattern = r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"
    return re.findall(pattern, text)


def is_admin(user_id: int | None, admin_ids: set[int]) -> bool:
    """Проверяет, является ли пользователь администратором."""
    if not admin_ids:
        return True
    return bool(user_id and user_id in admin_ids)


def _admin_filter(admin_ids: set[int]):
    def check(msg_or_cb: types.TelegramObject) -> bool:
        user = getattr(msg_or_cb, "from_user", None)
        return is_admin(getattr(user, "id", None), admin_ids)
    return check


# ─────────────────────────────────────────────
#  Клавиатуры
# ─────────────────────────────────────────────

def main_keyboard(state: ScannerState) -> InlineKeyboardMarkup:
    status_btn = (
        InlineKeyboardButton(text="▶️ Запустить", callback_data="scanner_resume")
        if state.is_paused
        else InlineKeyboardButton(text="⏸ Пауза", callback_data="scanner_pause")
    )
    vault_count = len(state.vault) if state.vault else 0
    vault_badge = f" ({vault_count})" if vault_count > 0 else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [status_btn, InlineKeyboardButton(text="🔄 Обновить статус", callback_data="nav_main")],
            [
                InlineKeyboardButton(text="⚡ Быстрые выкупы (<2с)", callback_data="fast_buys_menu"),
                InlineKeyboardButton(text="📋 Вся история ленты", callback_data="full_history_menu"),
            ],
            [
                InlineKeyboardButton(text="🔍 Поиск дешёвых", callback_data="find_cheap_feed"),
                InlineKeyboardButton(text="🎯 Выкупы под фильтры", callback_data="find_filter_feed"),
            ],
            [
                InlineKeyboardButton(text=f"📦 Хранилище{vault_badge}", callback_data="nav_vault"),
                InlineKeyboardButton(text="🔔 Категории", callback_data="nav_categories"),
            ],
            [
                InlineKeyboardButton(text="🔑 Токены", callback_data="nav_tokens"),
                InlineKeyboardButton(text="🌐 Прокси и Пинг", callback_data="nav_proxies"),
            ],
            [
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="nav_settings"),
                InlineKeyboardButton(text="🔄 Обновить флоры", callback_data="refresh_floors"),
            ],
            [
                InlineKeyboardButton(text="📋 Просмотр логов", callback_data="nav_logs"),
                InlineKeyboardButton(text="🚀 Загрузить обновление", callback_data="btn_git_update"),
            ],
        ]
    )


def fast_buys_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="50 стр (~1k)", callback_data="fb_pages_50"),
                InlineKeyboardButton(text="200 стр (~4k)", callback_data="fb_pages_200"),
            ],
            [
                InlineKeyboardButton(text="500 стр (~10k)", callback_data="fb_pages_500"),
                InlineKeyboardButton(text="1000 стр (~20k)", callback_data="fb_pages_1000"),
            ],
            [
                InlineKeyboardButton(text="2500 стр (~50k)", callback_data="fb_pages_2500"),
                InlineKeyboardButton(text="5000 стр (~100k)", callback_data="fb_pages_5000"),
            ],
            [
                InlineKeyboardButton(text="⌨️ Ввести своё число страниц", callback_data="fb_pages_custom"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav_main"),
            ],
        ]
    )


def full_history_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="50 стр (~1k)", callback_data="fh_pages_50"),
                InlineKeyboardButton(text="200 стр (~4k)", callback_data="fh_pages_200"),
            ],
            [
                InlineKeyboardButton(text="500 стр (~10k)", callback_data="fh_pages_500"),
                InlineKeyboardButton(text="1000 стр (~20k)", callback_data="fh_pages_1000"),
            ],
            [
                InlineKeyboardButton(text="2500 стр (~50k)", callback_data="fh_pages_2500"),
                InlineKeyboardButton(text="5000 стр (~100k)", callback_data="fh_pages_5000"),
            ],
            [
                InlineKeyboardButton(text="⌨️ Ввести своё число страниц", callback_data="fh_pages_custom"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav_main"),
            ],
        ]
    )


def logs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡ Последние 30 строк", callback_data="logs_tail_30"),
                InlineKeyboardButton(text="📜 Последние 100 строк", callback_data="logs_tail_100"),
            ],
            [
                InlineKeyboardButton(text="🛒 Логи покупок", callback_data="logs_filter_buy"),
                InlineKeyboardButton(text="⚠️ Ошибки (WARN/ERR)", callback_data="logs_filter_err"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav_main"),
            ],
        ]
    )


def tokens_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Проверить балансы", callback_data="tokens_verify_all"),
            ],
            [
                InlineKeyboardButton(text="👑 Сменить основной", callback_data="nav_select_primary"),
            ],
            [
                InlineKeyboardButton(text="➕ Добавить токен", callback_data="tokens_add"),
                InlineKeyboardButton(text="📝 Заменить все", callback_data="tokens_replace"),
            ],
            [
                InlineKeyboardButton(text="🧹 Удалить 401 токены", callback_data="tokens_cleanup_401"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav_main"),
            ],
        ]
    )


def settings_keyboard(state: ScannerState) -> InlineKeyboardMarkup:
    bal_toggle_text = "🟢 ВКЛ" if state.filter_by_balance else "🔴 ВЫКЛ"
    autobuy_toggle_text = "🟢 ВКЛ" if state.auto_buy else "🔴 ВЫКЛ"
    direct_toggle_text = "🟢 ВКЛ" if getattr(state, "use_direct", False) else "🔴 ВЫКЛ"
    max_p = getattr(state, "max_gift_price_ton", 0.0)
    max_p_btn = f"🛑 Макс. цена: {max_p:.1f} TON" if max_p > 0 else "🛑 Макс. цена: ВЫКЛ"
    p429 = state.get_429_count_last_hour()
    p429_badge = f" (429: {p429}/ч)" if p429 > 0 else " (429: 0)"
    em = getattr(state, "eval_mode", "tiered")
    eval_btn_text = "🪜 Оценка: Ступенчатая (3 тира)" if em == "tiered" else "📏 Оценка: Фиксированная"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🤖 Авто-покупка (AutoBuy): {autobuy_toggle_text}", callback_data="toggle_autobuy")],
            [InlineKeyboardButton(text=f"🌐 Прямой IP сервера: {direct_toggle_text}", callback_data="toggle_use_direct")],
            [InlineKeyboardButton(text=f"💰 Фильтр по балансу: {bal_toggle_text}", callback_data="toggle_balance_filter")],
            [InlineKeyboardButton(text=max_p_btn, callback_data="set_max_price")],
            [InlineKeyboardButton(text="📊 Мин. оборот/цена (NFT)", callback_data="set_turnover_ratio")],
            [InlineKeyboardButton(text="👑 Сменить основной аккаунт", callback_data="nav_select_primary")],
            [InlineKeyboardButton(text=eval_btn_text, callback_data="toggle_eval_mode")],
            [InlineKeyboardButton(text="✏️ Порог выгоды (MIN_TON_DIFF)", callback_data="set_min_diff")],
            [InlineKeyboardButton(text=f"📈 Мин. маржа: {getattr(state, 'min_margin_pct', 5.0):.1f}%", callback_data="set_min_margin")],
            [InlineKeyboardButton(text="✏️ Порог дешёвых (CHEAP_THRESHOLD)", callback_data="set_cheap")],
            [
                InlineKeyboardButton(text="➖ 0.05с", callback_data="interval_minus_005"),
                InlineKeyboardButton(text=interval_btn_text, callback_data="set_interval"),
                InlineKeyboardButton(text="➕ 0.05с", callback_data="interval_plus_005"),
            ],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav_main")],
        ]
    )




def proxies_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Перепроверить пинг", callback_data="proxies_reping")],
            [InlineKeyboardButton(text="🔍 Автопоиск быстрых (≤800мс)", callback_data="proxies_autosearch")],
            [
                InlineKeyboardButton(text="➕ Добавить свои прокси", callback_data="proxies_add_custom"),
                InlineKeyboardButton(text="📋 Мои прокси", callback_data="proxies_list_custom"),
            ],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav_main")],
        ]
    )


def categories_keyboard(state: ScannerState) -> InlineKeyboardMarkup:
    cat_names = {
        "BLACK":  "🖤 Чёрный фон",
        "CHEAP":  "💸 Сверхдешёвые",
        "NFT":    "🎯 Ниже флора",
        "LOW_ID": "🏷️ Редкий ID (<100)",
    }
    mode_labels = {
        "autobuy": "⚡ Автопокупка",
        "notify":  "🔔 Только уведомл.",
        "off":     "🔴 Отключено",
    }
    rows = []
    for cat_key, cat_label in cat_names.items():
        mode = state.get_category_mode(cat_key)
        status = mode_labels.get(mode, "⚡ Автопокупка")
        rows.append([
            InlineKeyboardButton(
                text=f"{cat_label}: {status}",
                callback_data=f"toggle_cat_{cat_key}",
            )
        ])
    vault_count = len(state.vault) if state.vault else 0
    rows.append([InlineKeyboardButton(text=f"📦 Перейти в Хранилище ({vault_count})", callback_data="nav_vault")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def vault_keyboard(state: ScannerState) -> InlineKeyboardMarkup:
    vault_len = len(state.vault) if state.vault else 0
    rows = []
    if vault_len > 0:
        rows.append([InlineKeyboardButton(text=f"📤 Отправить все ({vault_len}) в чат", callback_data="vault_send_all")])
        rows.append([InlineKeyboardButton(text="🗑 Очистить хранилище", callback_data="vault_clear")])
    rows.append([InlineKeyboardButton(text="🔔 Настройка категорий", callback_data="nav_categories")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_menu_keyboard(target: str = "nav_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]
        ]
    )



async def safe_edit_text(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
) -> bool:
    """Безопасное редактирование сообщения Telegram с защитой от ошибки 'message is not modified'."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return False
        log.warning("safe_edit_text TelegramBadRequest: %s", e)
        return False
    except Exception as e:
        log.warning("safe_edit_text ошибка: %s", e)
        return False


# ─────────────────────────────────────────────
#  Формирование текста экранов
# ─────────────────────────────────────────────

def format_main_text(state: ScannerState) -> str:
    if getattr(state, "is_analyzing_feed", False):
        status_icon = "⏳ <b>АНАЛИЗ ЛЕНТЫ (&le; 2с)</b>"
    elif state.is_paused:
        status_icon = "⏸ <b>НА ПАУЗЕ</b>"
    else:
        status_icon = "🟢 <b>СКАНИРУЕТ</b>"
    bf_str = f"{state.black_floor_nano / 1e9:.2f} TON" if state.black_floor_nano else "не определён"

    active_tokens = len(state.pool.get_tokens()) if state.pool else 0
    active_proxies = len(state.pool.get_proxies()) if state.pool else 0

    primary_tok = state.pool.primary_token if state.pool else None
    primary_str = _mask_token(primary_tok) if primary_tok else "не задан"
    bal_str = f"{state.primary_balance_nano / 1e9:.2f} TON" if state.primary_balance_nano is not None else "не проверен"
    filter_bal_str = "🟢 ВКЛ" if state.filter_by_balance else "🔴 ВЫКЛ"
    turnover_str = f"≥ {state.min_turnover_ratio:.1f}x" if state.min_turnover_ratio > 0 else "выключен"
    floors_count = state.collection_floors_count if hasattr(state, "collection_floors_count") else 0
    vault_count = len(state.vault) if state.vault else 0

    # Авто-интервал
    adaptor = getattr(state, "rate_adaptor", None)
    if adaptor is not None and adaptor.is_auto:
        interval_str = f"{state.scan_interval:.2f} с <i>(авто)</i>"
    else:
        interval_str = f"{state.scan_interval:.2f} с <i>(ручной)</i>"

    p429 = state.get_429_count_last_hour()
    p429_main = f" | ⚠️ <b>429: {p429}/ч</b>" if p429 > 0 else " | 429: <code>0/ч</code>"

    fail_cnt, fail_last = state.get_proxy_failures_stats()
    fail_str = f"<b>{fail_cnt}</b> (посл: <code>{fail_last}</code>)" if fail_cnt > 0 else "<code>0</code>"

    em = getattr(state, "eval_mode", "tiered")
    eval_main_str = "🪜 Ступенчатая (3 тира)" if em == "tiered" else f"📏 Фикс. ({state.min_ton_diff:.2f} TON / {getattr(state, 'min_margin_pct', 5.0):.1f}%)"

    return (
        f"🤖 <b>MRKT Scanner Manager</b>\n\n"
        f"Статус: {status_icon}\n"
        f"⏱ Аптайм: <code>{state.uptime_str()}</code> | 🕒 <code>{datetime.now().strftime('%H:%M:%S')}</code>\n"
        f"📊 Сканов: <code>{state.scans_count:,}</code> | 🎯 Сделок: <code>{state.deals_count}</code>\n"
        f"📦 В хранилище: <b>{vault_count}</b> сделок\n\n"
        f"⚙️ <b>Параметры:</b>\n"
        f"• 🤖 AutoBuy: <b>{'🟢 ВКЛ' if state.auto_buy else '🔴 ВЫКЛ'}</b>\n"
        f"• Оценка выгоды: <b>{eval_main_str}</b>\n"
        f"• Дешёвые подарки: &lt; <code>{state.cheap_price_threshold:.2f} TON</code>\n"
        f"• Мин. оборот/цена: <code>{turnover_str}</code>\n"
        f"• Фильтр по балансу: <b>{filter_bal_str}</b>\n"
        f"• ⚡ Интервал: {interval_str}{p429_main}\n\n"

        f"👑 <b>Основной аккаунт:</b>\n"
        f"• Токен: <code>{primary_str}</code>\n"
        f"• Баланс TON: <code>{bal_str}</code>\n\n"
        f"📦 <b>Рыночные данные:</b>\n"
        f"• Флор чёрного фона: <code>{bf_str}</code>\n"
        f"• Коллекций в кэше: <code>{floors_count}</code>\n\n"
        f"🔌 <b>Ресурсы:</b>\n"
        f"• Токенов: <code>{active_tokens}</code> <i>(обновлены: {get_tokens_updated_str()})</i>\n"
        f"• Прокси: <code>{active_proxies}</code>{' <i>(+1 прямой IP)</i>' if getattr(state, 'use_direct', False) else ''}\n"
        f"• Сбои прокси (2ч): {fail_str}"
    )


def format_categories_text(state: ScannerState) -> str:
    vault_len = len(state.vault) if state.vault else 0
    autobuy_status = "🟢 ВКЛ" if state.auto_buy else "🔴 ВЫКЛ"
    return (
        f"🔔 <b>Управление категориями подарков</b>\n\n"
        f"Глобальный AutoBuy: <b>{autobuy_status}</b>\n\n"
        f"Нажмите на категорию, чтобы переключить режим:\n\n"
        f"• ⚡ <b>Автопокупка</b> — мгновенный выкуп с основного аккаунта (если глобальный AutoBuy включен), либо алерт в чат с кнопкой покупки.\n"
        f"• 🔔 <b>Только уведомления</b> — присылает алерт в чат с кнопкой ручной покупки <code>[💳 Купить]</code>, <b>никогда не выкупает автоматически</b>.\n"
        f"• 🔴 <b>Отключено</b> — полностью глушит алерты в чат, сделки сохраняются в 📦 <b>Хранилище</b> (сейчас там: <b>{vault_len}</b> шт.).\n"
    )


def format_vault_text(state: ScannerState) -> str:
    vault = state.vault or []
    count = len(vault)
    if count == 0:
        return (
            "📦 <b>Хранилище сделок</b>\n\n"
            "Хранилище сейчас <b>пусто</b>.\n\n"
            "<i>Сюда автоматически сохраняются сделки тех категорий, для которых выключены моментальные уведомления.</i>"
        )

    by_cat: dict[str, int] = {}
    for d in vault:
        t = d.get("type", "NFT")
        by_cat[t] = by_cat.get(t, 0) + 1

    return (
        f"📦 <b>Хранилище сделок</b>\n\n"
        f"Всего накоплено сделок: <b>{count}</b> шт.\n"
        f"• 🖤 Чёрный фон: <b>{by_cat.get('BLACK', 0)}</b>\n"
        f"• 💸 Сверхдешёвые: <b>{by_cat.get('CHEAP', 0)}</b>\n"
        f"• 🎯 Ниже флора коллекции: <b>{by_cat.get('NFT', 0)}</b>\n"
        f"• 🏷️ Редкие номера (&lt;100): <b>{by_cat.get('LOW_ID', 0)}</b>\n\n"
        f"Нажмите <b>«📤 Отправить все в чат»</b>, чтобы выгрузить все накопленные подарки сообщениями."
    )


def format_settings_text(state: ScannerState) -> str:
    autobuy_str = "🟢 ВКЛ" if state.auto_buy else "🔴 ВЫКЛ"
    direct_str = "🟢 ВКЛ (1 слот напрямую)" if getattr(state, "use_direct", False) else "🔴 ВЫКЛ (все через VPN)"
    bal_str = f"{state.primary_balance_nano / 1e9:.2f} TON" if state.primary_balance_nano is not None else "не проверен"
    filter_bal_str = "🟢 ВКЛ" if state.filter_by_balance else "🔴 ВЫКЛ"
    max_p = getattr(state, "max_gift_price_ton", 0.0)
    max_price_str = f"≤ {max_p:.2f} TON" if max_p > 0 else "без ограничений (выключен)"
    turnover_str = f"≥ {state.min_turnover_ratio:.1f}x" if state.min_turnover_ratio > 0 else "выключен (0.0)"
    primary_tok = state.pool.primary_token if state.pool else None
    primary_str = _mask_token(primary_tok) if primary_tok else "не задан"
    adaptor = getattr(state, "rate_adaptor", None)
    auto_mode = adaptor.is_auto if adaptor is not None else True
    interval_mode = "авто" if auto_mode else "ручной"
    p429 = state.get_429_count_last_hour()
    p429_badge = f" [штрафов 429: <b>{p429}</b>/ч]" if p429 > 0 else " [штрафов 429: 0/ч]"

    em = getattr(state, "eval_mode", "tiered")
    eval_mode_str = (
        "🪜 <b>Ступенчатый (3 тира)</b>\n"
        "   • 🥉 <i>&lt; 20 TON:</i> чистыми ≥ 1.5 TON, маржа ≥ 10%\n"
        "   • 🥈 <i>20–100 TON:</i> чистыми ≥ 3.5 TON, маржа ≥ 5%\n"
        "   • 🥇 <i>&gt; 100 TON:</i> чистыми ≥ 7.0 TON, маржа ≥ 2.5% (буфер демпинга)"
        if em == "tiered" else
        f"📏 <b>Фиксированный:</b> чистыми ≥ <code>{state.min_ton_diff:.2f} TON</code> И маржа ≥ <code>{getattr(state, 'min_margin_pct', 5.0):.1f}%</code>"
    )

    return (
        f"⚙️ <b>Настройки сканера</b>\n\n"
        f"0. <b>Авто-покупка (AutoBuy):</b> {autobuy_str}\n"
        f"   <i>(Моментальный выкуп подходящих подарков с основного аккаунта без задержек)</i>\n\n"
        f"1. <b>Прямой IP сервера (Direct):</b> {direct_str}\n"
        f"   <i>(Один токен ходит напрямую с IP VPS без VPN overhead для максимальной скорости)</i>\n\n"
        f"2. <b>Фильтр по балансу:</b> {filter_bal_str}\n"
        f"   <i>(Показывать только подарки, на которые хватает баланса основного аккаунта)</i>\n\n"
        f"3. <b>Мин. оборот/цена для NFT:</b> <code>{turnover_str}</code>\n"
        f"   <i>(Отсекает мёртвый груз: оборот/цена ≥ X; кроме чёрного фона и подарков &lt; {state.cheap_price_threshold:.1f} TON)</i>\n\n"
        f"4. <b>Основной аккаунт:</b> <code>{primary_str}</code>\n"
        f"   <i>(Текущий баланс: <code>{bal_str}</code>; используется для покупок)</i>\n\n"
        f"5. <b>Режим оценки выгоды:</b> {eval_mode_str}\n\n"
        f"6. <b>Базовый порог выгоды (MIN_TON_DIFF):</b> <code>{state.min_ton_diff:.2f} TON</code>\n"
        f"   <i>(Используется в фиксированном режиме и масштабирует тиры)</i>\n\n"
        f"7. <b>Базовая мин. маржа (%):</b> <code>{getattr(state, 'min_margin_pct', 5.0):.1f}%</code>\n\n"
        f"8. <b>Порог дешёвых (CHEAP_THRESHOLD):</b> <code>{state.cheap_price_threshold:.2f} TON</code>\n"
        f"   <i>(Любой подарок с ценой ниже этого порога считается выгодным)</i>\n\n"
        f"9. <b>⚡ Интервал сканирования ({interval_mode}):</b> <code>{state.scan_interval:.2f} с</code>{p429_badge}\n"
        f"   <i>(Пауза между запросами; при установке вручную авто-адаптация отключается)</i>\n\n"
        f"10. <b>🛑 Фильтр макс. цены подарка:</b> <code>{max_price_str}</code>\n"
        f"   <i>(Подарки с ценой выше этого значения сканер сразу игнорирует)</i>"
    )



def format_fast_buys_menu() -> str:
    return (
        "⚡ <b>Глубокий анализ истории выкупов (&le; 2 сек)</b>\n\n"
        "Этот режим сканирует историю ленты <code>/feed</code> и находит все подарки, "
        "которые были мгновенно выкуплены ботами или снайперами менее чем за <b>2.0 секунды</b> "
        "после их выставления или снижения цены.\n\n"
        "📝 <b>Что записывается в отчёт:</b>\n"
        "• Время выставления и выкупа\n"
        "• Скорость выкупа в <b>миллисекундах</b> (например, <code>350 мс</code>)\n"
        "• Цена в TON, коллекция, модель, фон, номер и ID\n"
        "• Рыночные данные: флор коллекции, скидка в %, флор черного фона, оборот, редкий номер\n\n"
        "⚠️ <b>Внимание:</b> на время анализа основной сканер будет <b>автоматически приостановлен</b>, "
        "а запросы будут распределяться по всем вашим токенам и прокси с безопасными задержками.\n\n"
        "По итогу бот пришлёт вам <b>краткий отчёт</b> и полный <b>JSON-файл</b> со всеми данными.\n\n"
        "Выберите количество страниц истории для анализа или введите своё:"
    )


def format_full_history_menu() -> str:
    return (
        "📋 <b>Полная выгрузка всей истории ленты (/feed)</b>\n\n"
        "Этот режим сканирует историю ленты и сохраняет <b>все события продаж и листингов</b>, "
        "с детальной информацией о каждом подарке, флорах, объемах и скорости выкупа.\n\n"
        "📝 <b>Каждая запись содержит:</b>\n"
        "• <code>fast_buy</code>: <code>true</code> (если выкуплен &le; 2 сек) или <code>false</code>\n"
        "• Время выставления, время покупки и дельту в миллисекундах (если листинг найден)\n"
        "• Цену в TON, коллекцию, модель, фон, номер и ID\n"
        "• Рыночные флоры, процент скидки, оборот коллекции\n\n"
        "⚠️ <b>Внимание:</b> на время выгрузки основной сканер будет <b>автоматически приостановлен</b>, "
        "а запросы будут распределяться по всем вашим токенам и прокси с безопасными задержками.\n\n"
        "По итогу бот пришлёт вам <b>краткий отчёт</b>, а также полные файлы <b>JSON</b> и <b>CSV</b> со всеми данными.\n\n"
        "Выберите количество страниц истории для выгрузки или введите своё:"
    )


def format_tokens_text(tokens: list[str], verified_info: Optional[dict] = None) -> str:
    tok_upd = get_tokens_updated_str()
    if not tokens:
        return f"🔑 <b>Управление токенами</b>\n\n⚠️ В пуле нет активных токенов!\n🕒 Файл <code>tokens.txt</code> изменён: <code>{tok_upd}</code>"

    lines = [f"🔑 <b>Управление токенами</b> (Всего: <code>{len(tokens)}</code> | Обновлены: <code>{tok_upd}</code>):\n"]
    primary_tok = tokens[0] if tokens else None
    for i, tok in enumerate(tokens, 1):
        masked = _mask_token(tok)
        is_prim = " 👑 [Основной]" if tok == primary_tok else ""
        status = ""
        if verified_info and tok in verified_info:
            ok, msg = verified_info[tok]
            status = f" — {'✅' if ok else '❌'} <i>{msg}</i>"
        lines.append(f"{i}. <code>{masked}</code>{is_prim}{status}")

    lines.append("\n💡 <i>Первый токен является 👑 основным (с него проверяется баланс). Нажмите «Сменить основной», чтобы переключить.</i>")
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  Создание и запуск Telegram Bot Runner
# ─────────────────────────────────────────────

async def run_telegram_bot(bot_token: str, admin_ids: set[int], scanner_state: ScannerState) -> None:
    """Запускает Telegram бота через aiogram 3 в фоновом режиме."""
    bot = Bot(token=bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    # ── Проверка прав админа ─────────────────────────────────────────────
    @dp.message(lambda m: m.from_user and m.from_user.id not in admin_ids)
    @dp.callback_query(lambda c: c.from_user and c.from_user.id not in admin_ids)
    async def unauthorized_handler(event: types.TelegramObject):
        if isinstance(event, Message):
            await event.answer("⛔ <b>Доступ запрещён.</b> Бот настроен только для администратора.", parse_mode="HTML")
        elif isinstance(event, CallbackQuery):
            await event.answer("⛔ Доступ запрещён.", show_alert=True)

    # ── Главное меню ─────────────────────────────────────────────────────
    @dp.message(CommandStart())
    async def cmd_start(msg: Message, state: FSMContext):
        await state.clear()
        text = format_main_text(scanner_state)
        await msg.answer(text, reply_markup=main_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "nav_main")
    async def cb_nav_main(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer("🔄 Статус обновлен")
        text = format_main_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=main_keyboard(scanner_state), parse_mode="HTML")

    # ── Управление сканером (Пауза/Старт) ─────────────────────────────────
    @dp.callback_query(F.data == "scanner_pause")
    async def cb_scanner_pause(cb: CallbackQuery):
        scanner_state.is_paused = True
        await cb.answer("⏸ Сканер поставлен на паузу")
        text = format_main_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=main_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "scanner_resume")
    async def cb_scanner_resume(cb: CallbackQuery):
        scanner_state.is_paused = False
        await cb.answer("▶️ Сканер возобновил работу")
        text = format_main_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=main_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "refresh_floors")
    async def cb_refresh_floors(cb: CallbackQuery):
        scanner_state.force_refresh_floors = True
        await cb.answer("🔄 Запущено обновление флоров коллекций...")

    # ── Раздел: Поиск дешёвых выкупов в ленте (feed) ─────────────────────
    @dp.callback_query(F.data == "find_cheap_feed")
    async def cb_find_cheap_feed(cb: CallbackQuery):
        await cb.answer("🔍 Запуск поиска выкупов...", show_alert=False)
        thresh_ton = scanner_state.cheap_price_threshold
        max_price_nano = int(round(thresh_ton * 1e9))

        status_msg = await cb.message.answer(
            f"🔍 <b>Поиск последних выкупов дешевле {thresh_ton:.2f} TON...</b>\n"
            f"<i>Опрашиваю ленту маркетплейса (feed)...</i>",
            parse_mode="HTML",
        )

        try:
            pool = scanner_state.pool
            if not pool or not pool.slots:
                await status_msg.edit_text("❌ В пуле нет активных токенов.")
                return

            matched = await find_recent_cheap_buys_async(
                pool=pool,
                max_price_nano=max_price_nano,
                limit=5,
                max_pages=20,
            )

            if not matched:
                await status_msg.edit_text(
                    f"⚠️ В ленте не найдено выкупов дешевле <b>{thresh_ton:.2f} TON</b> за последние 20 страниц.",
                    parse_mode="HTML",
                )
                return

            await status_msg.edit_text(
                f"✅ Найдено <b>{len(matched)}</b> последних быстрых выкупов (&lt; <code>{thresh_ton:.2f} TON</code>):",
                parse_mode="HTML",
            )

            for i, m in enumerate(matched, 1):
                gift = m.get("gift", {})
                col_name = gift.get("collectionName") or gift.get("collectionTitle") or gift.get("title") or "NFT"
                mod_name = gift.get("modelName") or gift.get("modelTitle") or ""
                num = gift.get("number")
                num_str = f" #{num}" if num else ""
                name_str = f"{col_name} — {mod_name}{num_str}" if mod_name else f"{col_name}{num_str}"

                amount_nano = m.get("amount", 0)
                amount_ton = amount_nano / 1e9
                delta_ms = m.get("delta_ms", 0)
                delta_str = f"<code>{delta_ms} мс</code>" if delta_ms < 1000 else f"<code>{delta_ms / 1000:.2f} с</code> (<code>{delta_ms} мс</code>)"

                nft_url = make_telegram_nft_url(col_name, num)

                # Флор коллекции
                floor_nano = scanner_state.collection_floors.get(col_name)
                if floor_nano:
                    floor_ton = floor_nano / 1e9
                    diff = floor_ton - amount_ton
                    profit_str = f" <i>(дешевле флора на +{diff:.2f} TON)</i>" if diff > 0 else ""
                    floor_info = f"<code>{floor_ton:.2f} TON</code>{profit_str}"
                else:
                    floor_info = "<i>не определён</i>"

                sale_dt = m.get("sale_date")
                dt_str = sale_dt.strftime("%d.%m.%Y %H:%M:%S UTC") if sale_dt else "—"

                text = (
                    f"⚡ <b>Выкуп #{i}</b>\n\n"
                    f"🎁 <b>{name_str}</b>\n"
                    f"💸 <b>Цена лота:</b> <code>{amount_ton:.2f} TON</code>\n"
                    f"📈 <b>Флор коллекции:</b> {floor_info}\n"
                    f"⚡ <b>Выкуплен за:</b> {delta_str}\n"
                    f"🕒 <b>Время сделки:</b> <code>{dt_str}</code>\n"
                    f"🔗 <a href=\"{nft_url}\">Открыть подарок в Telegram</a>"
                )
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text=f"🔗 Открыть {col_name}{num_str}", url=nft_url)]
                    ]
                )
                await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
                await asyncio.sleep(0.2)

        except Exception as e:
            log.error("Ошибка поиска по ленте: %s", e, exc_info=True)
            await status_msg.edit_text(f"❌ Ошибка при поиске по ленте: {e}")

    # ── Раздел: Поиск выкупов под активные фильтры в ленте (feed) ───────
    @dp.callback_query(F.data == "find_filter_feed")
    async def cb_find_filter_feed(cb: CallbackQuery):
        await cb.answer("🎯 Запуск поиска выкупов по фильтрам...", show_alert=False)

        status_msg = await cb.message.answer(
            "🎯 <b>Поиск последних выкупов под ваши фильтры...</b>\n"
            "<i>Анализирую историю ленты (/feed) на соответствие флору, дешёвым лотам, редкостям...</i>",
            parse_mode="HTML",
        )

        try:
            pool = scanner_state.pool
            if not pool or not pool.slots:
                await status_msg.edit_text("❌ В пуле нет активных токенов.")
                return

            matched = await find_recent_filter_buys_async(
                pool=pool,
                scanner_state=scanner_state,
                limit=5,
                max_pages=30,
            )

            if not matched:
                await status_msg.edit_text(
                    "⚠️ В ленте не найдено выкупов, подходящих под текущие активные фильтры, за последние 30 страниц.\n"
                    "<i>Возможно, пороги выгоды слишком строгие либо подходящие сделки были раньше.</i>",
                    parse_mode="HTML",
                )
                return

            await status_msg.edit_text(
                f"✅ Найдено <b>{len(matched)}</b> последних сделок, подходивших под ваши фильтры:",
                parse_mode="HTML",
            )

            cat_labels = {
                "BLACK": "🖤 Чёрный фон",
                "CHEAP": "💸 Сверхдешёвый",
                "NFT": "🎯 Ниже флора коллекции",
                "LOW_ID": "🏷️ Редкий ID (<100)",
            }

            for i, m in enumerate(matched, 1):
                gift = m.get("gift", {})
                col_name = gift.get("collectionName") or gift.get("collectionTitle") or gift.get("title") or "NFT"
                mod_name = gift.get("modelName") or gift.get("modelTitle") or ""
                num = gift.get("number")
                num_str = f" #{num}" if num else ""
                name_str = f"{col_name} — {mod_name}{num_str}" if mod_name else f"{col_name}{num_str}"

                amount_nano = m.get("amount", 0)
                amount_ton = amount_nano / 1e9
                delta_str = m.get("delta_str", "—")
                deal = m.get("deal", {})
                cat_type = deal.get("type", "NFT")
                cat_label = cat_labels.get(cat_type, cat_type)

                nft_url = make_telegram_nft_url(col_name, num)

                # Флор коллекции
                floor_nano = scanner_state.collection_floors.get(col_name)
                if floor_nano:
                    floor_ton = floor_nano / 1e9
                    diff = floor_ton - amount_ton
                    profit_str = f" <i>(дешевле флора на +{diff:.2f} TON)</i>" if diff > 0 else ""
                    floor_info = f"<code>{floor_ton:.2f} TON</code>{profit_str}"
                else:
                    floor_info = "<i>не определён</i>"

                sale_dt = m.get("sale_date")
                dt_str = sale_dt.strftime("%d.%m.%Y %H:%M:%S UTC") if sale_dt else "—"

                text = (
                    f"🎯 <b>Сделка #{i} (под фильтры)</b>\n\n"
                    f"🎁 <b>{name_str}</b>\n"
                    f"🏷️ <b>Категория:</b> {cat_label}\n"
                    f"💸 <b>Цена покупки:</b> <code>{amount_ton:.2f} TON</code>\n"
                    f"📈 <b>Флор коллекции:</b> {floor_info}\n"
                    f"⚡ <b>Выкуплен за:</b> <b>{delta_str}</b>\n"
                    f"🕒 <b>Время сделки:</b> <code>{dt_str}</code>\n"
                    f"🔗 <a href=\"{nft_url}\">Открыть подарок в Telegram</a>"
                )
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text=f"🔗 Открыть {col_name}{num_str}", url=nft_url)]
                    ]
                )
                await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
                await asyncio.sleep(0.2)

        except Exception as e:
            log.error("Ошибка поиска по фильтрам в ленте: %s", e, exc_info=True)
            await status_msg.edit_text(f"❌ Ошибка при поиске по ленте: {e}")

    # ── Режим глубокого анализа истории ленты (/feed <= 2с) ──────────────
    async def _safe_edit_status(
        bot: Bot,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    async def run_feed_analysis_session(
        pages: int,
        bot: Bot,
        chat_id: int,
        scanner_state: ScannerState,
        mode: str = "fast_buys",
    ) -> None:
        from feed_analyzer import FeedAnalyzer

        if getattr(scanner_state, "is_analyzing_feed", False):
            await bot.send_message(chat_id, "⚠️ Анализ ленты уже выполняется в данный момент!")
            return

        pool = scanner_state.pool
        if not pool or not pool.slots:
            await bot.send_message(chat_id, "❌ В пуле нет доступных токенов для запуска анализа.")
            return

        cancel_event = asyncio.Event()
        scanner_state.feed_analysis_cancel = cancel_event

        is_full = (mode == "full_history")
        header_title = "📋 <b>Запуск выгрузки всей истории ленты...</b>" if is_full else "⚡ <b>Запуск анализа истории ленты (&le; 2с)...</b>"
        mode_note = "• Режим: <b>все события (с флагом fast_buy)</b>\n\n" if is_full else "• Порог выкупа: <code>&le; 2000 мс</code>\n\n"

        status_msg = await bot.send_message(
            chat_id,
            f"{header_title}\n\n"
            f"• Страниц к анализу: <code>{pages:,}</code> (~{pages * 20:,} событий)\n"
            f"{mode_note}"
            f"⏸ <i>Основной сканер временно приостановлен.</i>\n"
            f"🌐 <i>Запросы распределяются по пулу токенов и прокси...</i>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Прервать анализ", callback_data="fb_cancel")]
                ]
            ),
            parse_mode="HTML",
        )

        last_update_ts = 0.0

        def on_progress(p_done: int, p_total: int, events: int, found: int) -> None:
            nonlocal last_update_ts
            now = time.monotonic()
            if now - last_update_ts < 3.0:
                return
            last_update_ts = now
            pct = int(p_done / p_total * 100) if p_total > 0 else 0
            filled = int(pct / 10)
            bar = "█" * filled + "░" * (10 - filled)
            found_label = f"💾 Сохранено записей: <b>{found:,}</b> шт." if is_full else f"⚡ Найдено выкупов &le; 2с: <b>{found:,}</b> шт."
            text = (
                f"⏳ <b>Анализ ленты в процессе...</b>\n\n"
                f"[{bar}] <b>{pct}%</b> (<code>{p_done:,}</code> / <code>{p_total:,}</code> стр)\n\n"
                f"📦 Обработано событий: <code>{events:,}</code>\n"
                f"{found_label}\n\n"
                f"⏸ <i>Основной сканер приостановлен</i>"
            )
            asyncio.create_task(
                _safe_edit_status(
                    bot,
                    chat_id,
                    status_msg.message_id,
                    text,
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="❌ Прервать анализ", callback_data="fb_cancel")]
                        ]
                    ),
                )
            )

        analyzer = FeedAnalyzer(pool=pool, scanner_state=scanner_state)
        res = await analyzer.analyze_history(
            max_pages=pages,
            threshold_ms=2000,
            mode=mode,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

        status_word = "прерван пользователем" if res.cancelled else "успешно завершён"
        status_icon = "🛑" if res.cancelled else "✅"

        total_saved = len(res.matched_buys)
        fast_buys_only = [r for r in res.matched_buys if r.fast_buy]

        if is_full:
            summary_lines = [
                f"{status_icon} <b>Выгрузка всей истории {status_word}!</b>\n",
                f"⏱ Время выполнения: <code>{res.elapsed_sec:.1f} с</code>",
                f"📄 Проверено страниц: <code>{res.total_pages:,}</code> из {pages:,}",
                f"📦 Обработано событий ленты: <code>{res.total_events:,}</code>",
                f"💾 <b>Всего сохранено записей:</b> <code>{total_saved:,}</code> шт.",
                f"⚡ <b>Из них быстрых выкупов (&le; 2 сек):</b> <code>{len(fast_buys_only):,}</code> шт.\n",
            ]
            timed = [r for r in res.matched_buys if r.duration_ms is not None]
            if timed:
                avg_dur = sum(r.duration_ms for r in timed) / len(timed)
                fastest = min(r.duration_ms for r in timed)
                summary_lines.append("📊 <b>Статистика скорости (где найден листинг):</b>")
                summary_lines.append(f"• Самый быстрый выкуп: <b>{fastest} мс</b>")
                summary_lines.append(f"• Среднее время выкупа: <b>{avg_dur:.0f} мс</b>\n")
            summary_lines.append("📎 <i>Файлы JSON и CSV со всеми данными отправлены ниже.</i>")
        else:
            summary_lines = [
                f"{status_icon} <b>Анализ истории {status_word}!</b>\n",
                f"⏱ Время выполнения: <code>{res.elapsed_sec:.1f} с</code>",
                f"📄 Проверено страниц: <code>{res.total_pages:,}</code> из {pages:,}",
                f"📦 Обработано событий ленты: <code>{res.total_events:,}</code>",
                f"⚡ <b>Найдено быстрых выкупов (&le; 2 сек):</b> <code>{total_saved:,}</code> шт.\n",
            ]
            if res.matched_buys:
                valid_durations = [r.duration_ms for r in res.matched_buys if r.duration_ms is not None]
                if valid_durations:
                    avg_dur = sum(valid_durations) / len(valid_durations)
                    fastest = min(valid_durations)
                    summary_lines.append("📊 <b>Статистика скорости:</b>")
                    summary_lines.append(f"• Самый быстрый выкуп: <b>{fastest} мс</b>")
                    summary_lines.append(f"• Среднее время выкупа: <b>{avg_dur:.0f} мс</b>\n")

                top_fastest = sorted([r for r in res.matched_buys if r.duration_ms is not None], key=lambda x: x.duration_ms)[:5]
                if top_fastest:
                    summary_lines.append("🏆 <b>Топ самых быстрых выкупов:</b>")
                    for i, r in enumerate(top_fastest, 1):
                        disc_str = f" (скидка {r.discount_pct:+.0f}%)" if r.discount_pct is not None else ""
                        summary_lines.append(
                            f"{i}. <a href=\"{r.nft_url}\"><b>{html.escape(r.collection)} #{r.number}</b></a>\n"
                            f"   ⚡ <code>{r.duration_ms} мс</code> | 💰 <b>{r.price_ton:.2f} TON</b>{disc_str}"
                        )
                summary_lines.append("\n📎 <i>Полный отчёт в формате JSON и CSV отправлен файлами ниже.</i>")
            else:
                summary_lines.append("ℹ️ <i>Выкупов быстрее 2 секунд в просмотренном отрезке ленты не обнаружено.</i>")

        summary_lines.append("\n▶️ <i>Основной сканер автоматически вернулся в штатный режим.</i>")

        await _safe_edit_status(
            bot,
            chat_id,
            status_msg.message_id,
            "\n".join(summary_lines),
            reply_markup=back_to_menu_keyboard("nav_main"),
        )

        prefix_title = "Вся история ленты" if is_full else "Быстрые выкупы (<2с)"

        # Присылаем JSON с полными данными пользователю
        if res.matched_buys and res.json_path.exists():
            try:
                await bot.send_document(
                    chat_id=chat_id,
                    document=FSInputFile(str(res.json_path)),
                    caption=f"📋 {prefix_title} ({len(res.matched_buys)} записей, JSON)",
                )
            except Exception as e:
                log.warning("Не удалось отправить JSON файл: %s", e)

        # Присылаем также CSV для удобного просмотра в Excel
        if res.matched_buys and res.csv_path.exists():
            try:
                await bot.send_document(
                    chat_id=chat_id,
                    document=FSInputFile(str(res.csv_path)),
                    caption=f"📊 {prefix_title} (CSV для Excel)",
                )
            except Exception as e:
                log.warning("Не удалось отправить CSV файл: %s", e)

    @dp.callback_query(F.data == "fast_buys_menu")
    async def cb_fast_buys_menu(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        text = format_fast_buys_menu()
        await safe_edit_text(cb.message, text, reply_markup=fast_buys_keyboard(), parse_mode="HTML")

    @dp.callback_query(F.data.startswith("fb_pages_"))
    async def cb_fb_pages(cb: CallbackQuery, state: FSMContext):
        val = cb.data.replace("fb_pages_", "")
        if val == "custom":
            await state.set_state(BotStates.waiting_for_feed_analysis_pages)
            await cb.answer()
            await safe_edit_text(
                cb.message,
                "⌨️ <b>Введите количество страниц истории для анализа быстрых выкупов:</b>\n\n"
                "<i>(Каждая страница содержит 20 событий ленты. Например, <code>500</code> = 10 000 событий)</i>\n\n"
                "Допустимое число: от <code>1</code> до <code>50 000</code>.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="fast_buys_menu")]]
                ),
                parse_mode="HTML",
            )
            return

        if not val.isdigit():
            await cb.answer()
            return

        pages = int(val)
        await cb.answer(f"🚀 Запуск анализа {pages:,} страниц...")
        asyncio.create_task(
            run_feed_analysis_session(
                pages=pages,
                bot=cb.bot,
                chat_id=cb.message.chat.id,
                scanner_state=scanner_state,
                mode="fast_buys",
            )
        )

    @dp.message(BotStates.waiting_for_feed_analysis_pages)
    async def msg_feed_analysis_pages(msg: types.Message, state: FSMContext):
        raw = (msg.text or "").strip().replace(" ", "").replace("_", "")
        if not raw.isdigit():
            await msg.answer("❌ Пожалуйста, введите целое положительное число (например, <code>500</code>):", parse_mode="HTML")
            return

        pages = int(raw)
        if pages < 1 or pages > 50000:
            await msg.answer("❌ Число страниц должно быть от 1 до 50 000. Введите корректное число:")
            return

        await state.clear()
        asyncio.create_task(
            run_feed_analysis_session(
                pages=pages,
                bot=msg.bot,
                chat_id=msg.chat.id,
                scanner_state=scanner_state,
                mode="fast_buys",
            )
        )

    # ── Режим всей истории ленты (/feed) ──────────────────────────────────
    @dp.callback_query(F.data == "full_history_menu")
    async def cb_full_history_menu(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        text = format_full_history_menu()
        await safe_edit_text(cb.message, text, reply_markup=full_history_keyboard(), parse_mode="HTML")

    @dp.callback_query(F.data.startswith("fh_pages_"))
    async def cb_fh_pages(cb: CallbackQuery, state: FSMContext):
        val = cb.data.replace("fh_pages_", "")
        if val == "custom":
            await state.set_state(BotStates.waiting_for_full_history_pages)
            await cb.answer()
            await safe_edit_text(
                cb.message,
                "⌨️ <b>Введите количество страниц истории для выгрузки всей истории:</b>\n\n"
                "<i>(Каждая страница содержит 20 событий ленты. Например, <code>500</code> = 10 000 событий)</i>\n\n"
                "Допустимое число: от <code>1</code> до <code>50 000</code>.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="full_history_menu")]]
                ),
                parse_mode="HTML",
            )
            return

        if not val.isdigit():
            await cb.answer()
            return

        pages = int(val)
        await cb.answer(f"🚀 Запуск выгрузки {pages:,} страниц...")
        asyncio.create_task(
            run_feed_analysis_session(
                pages=pages,
                bot=cb.bot,
                chat_id=cb.message.chat.id,
                scanner_state=scanner_state,
                mode="full_history",
            )
        )

    @dp.message(BotStates.waiting_for_full_history_pages)
    async def msg_full_history_pages(msg: types.Message, state: FSMContext):
        raw = (msg.text or "").strip().replace(" ", "").replace("_", "")
        if not raw.isdigit():
            await msg.answer("❌ Пожалуйста, введите целое положительное число (например, <code>500</code>):", parse_mode="HTML")
            return

        pages = int(raw)
        if pages < 1 or pages > 50000:
            await msg.answer("❌ Число страниц должно быть от 1 до 50 000. Введите корректное число:")
            return

        await state.clear()
        asyncio.create_task(
            run_feed_analysis_session(
                pages=pages,
                bot=msg.bot,
                chat_id=msg.chat.id,
                scanner_state=scanner_state,
                mode="full_history",
            )
        )

    @dp.callback_query(F.data == "fb_cancel")
    async def cb_fb_cancel(cb: CallbackQuery):
        if scanner_state.feed_analysis_cancel and not scanner_state.feed_analysis_cancel.is_set():
            scanner_state.feed_analysis_cancel.set()
            await cb.answer("🛑 Останавливаем анализ, сохраняю результаты и формирую файлы...", show_alert=True)
        else:
            await cb.answer("Анализ не запущен или уже завершается")

    # ── Раздел: Уведомления по категориям ────────────────────────────────
    @dp.callback_query(F.data == "nav_categories")
    async def cb_nav_categories(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        text = format_categories_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=categories_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data.startswith("toggle_cat_"))
    async def cb_toggle_cat(cb: CallbackQuery):
        cat = cb.data.replace("toggle_cat_", "")
        current = scanner_state.get_category_mode(cat)
        # Циклическое переключение: autobuy -> notify -> off -> autobuy
        next_mode = {
            "autobuy": "notify",
            "notify": "off",
            "off": "autobuy",
        }.get(current, "autobuy")
        scanner_state.notify_categories[cat] = next_mode
        save_settings(scanner_state)

        mode_names = {
            "autobuy": "⚡ Автопокупка",
            "notify": "🔔 Только уведомления",
            "off": "🔴 Отключено",
        }
        status = mode_names.get(next_mode, next_mode)
        await cb.answer(f"{cat}: {status}")
        text = format_categories_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=categories_keyboard(scanner_state), parse_mode="HTML")

    # ── Раздел: Хранилище (Vault) ─────────────────────────────────────────
    @dp.callback_query(F.data == "nav_vault")
    async def cb_nav_vault(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        text = format_vault_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=vault_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "vault_send_all")
    async def cb_vault_send_all(cb: CallbackQuery):
        vault_deals = list(scanner_state.vault)
        if not vault_deals:
            await cb.answer("Хранилище пусто!", show_alert=True)
            return

        scanner_state.vault.clear()
        await cb.answer(f"Отправка {len(vault_deals)} сделок...")
        await safe_edit_text(
            cb.message,
            f"📤 <i>Отправка {len(vault_deals)} сделок из Хранилища в чат...</i>",
            parse_mode="HTML",
        )

        for d in vault_deals:
            await send_deal_notification(bot_token, admin_ids, d, bot=bot)
            await asyncio.sleep(0.08)

        text = format_vault_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=vault_keyboard(scanner_state), parse_mode="HTML")
        await cb.message.answer(
            f"✅ Все <b>{len(vault_deals)}</b> сделок из Хранилища успешно отправлены!",
            reply_markup=back_to_menu_keyboard("nav_vault"),
            parse_mode="HTML",
        )

    @dp.callback_query(F.data == "vault_clear")
    async def cb_vault_clear(cb: CallbackQuery):
        count = len(scanner_state.vault)
        scanner_state.vault.clear()
        await cb.answer(f"Хранилище очищено ({count} удалено)", show_alert=True)
        text = format_vault_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=vault_keyboard(scanner_state), parse_mode="HTML")

    # ── Раздел: Токены ───────────────────────────────────────────────────
    @dp.callback_query(F.data == "nav_tokens")
    async def cb_nav_tokens(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        tokens = scanner_state.pool.get_tokens() if scanner_state.pool else []
        text = format_tokens_text(tokens)
        await safe_edit_text(cb.message, text, reply_markup=tokens_keyboard(), parse_mode="HTML")

    @dp.callback_query(F.data == "tokens_verify_all")
    async def cb_tokens_verify_all(cb: CallbackQuery):
        tokens = scanner_state.pool.get_tokens() if scanner_state.pool else []
        if not tokens:
            await cb.answer("Токенов нет", show_alert=True)
            return

        await cb.answer("🔍 Проверяем токены через MRKT API...")
        await safe_edit_text(cb.message, "⏳ <i>Проверка токенов через MRKT API...</i>", parse_mode="HTML")

        verified: dict[str, tuple[bool, str]] = {}
        for tok in tokens:
            ok, msg, _ = await verify_token_async(tok)
            verified[tok] = (ok, msg)

        text = format_tokens_text(tokens, verified_info=verified)
        await safe_edit_text(cb.message, text, reply_markup=tokens_keyboard(), parse_mode="HTML")

    @dp.callback_query(F.data == "tokens_add")
    async def cb_tokens_add(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_add_token)
        await cb.answer()
        text = (
            "➕ <b>Добавление токена</b>\n\n"
            "Отправьте токен (UUID) в ответном сообщении.\n"
            "<i>(Можно скопировать токен целиком, curl-запрос или строку авторизации — бот сам найдёт UUID).</i>"
        )
        await safe_edit_text(cb.message, text, reply_markup=back_to_menu_keyboard("nav_tokens"), parse_mode="HTML")

    @dp.message(BotStates.waiting_for_add_token)
    async def msg_add_token(msg: Message, state: FSMContext):
        raw = msg.text or ""
        found = _extract_uuid_tokens(raw)
        if not found:
            await msg.answer(
                "❌ В сообщении не найден валидный UUID токен!\nПопробуйте ещё раз или нажмите /start.",
                reply_markup=back_to_menu_keyboard("nav_tokens"),
            )
            return

        new_tok = found[0]
        await msg.answer(f"🔍 Проверяем токен <code>{_mask_token(new_tok)}</code>...", parse_mode="HTML")
        ok, status_msg, _ = await verify_token_async(new_tok)

        current = scanner_state.pool.get_tokens() if scanner_state.pool else []
        if new_tok in current:
            await msg.answer("⚠️ Этот токен уже есть в списке!", reply_markup=back_to_menu_keyboard("nav_tokens"))
            await state.clear()
            return

        current.append(new_tok)
        save_tokens(current)
        if scanner_state.pool:
            scanner_state.pool.reload_tokens(current)

        ver_str = f"✅ Валиден (Баланс: {status_msg})" if ok else f"⚠️ Добавлен, но статус: {status_msg}"
        await msg.answer(
            f"🎉 <b>Токен успешно добавлен!</b>\n\n"
            f"Токен: <code>{_mask_token(new_tok)}</code>\n"
            f"Статус: {ver_str}\n"
            f"Всего токенов в пуле: <code>{len(current)}</code>",
            reply_markup=back_to_menu_keyboard("nav_tokens"),
            parse_mode="HTML",
        )
        await state.clear()

    @dp.callback_query(F.data == "tokens_replace")
    async def cb_tokens_replace(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_replace_tokens)
        await cb.answer()
        text = (
            "📝 <b>Полная замена токенов</b>\n\n"
            "Отправьте список новых токенов (по одному на строку, либо общий текст с токенами).\n"
            "⚠️ <i>Все старые токены будут заменены новыми!</i>"
        )
        await safe_edit_text(cb.message, text, reply_markup=back_to_menu_keyboard("nav_tokens"), parse_mode="HTML")

    @dp.message(BotStates.waiting_for_replace_tokens)
    async def msg_replace_tokens(msg: Message, state: FSMContext):
        raw = msg.text or ""
        found = _extract_uuid_tokens(raw)
        if not found:
            await msg.answer("❌ Не найдено ни одного UUID токена!", reply_markup=back_to_menu_keyboard("nav_tokens"))
            return

        unique_tokens = list(dict.fromkeys(found))
        save_tokens(unique_tokens)
        if scanner_state.pool:
            scanner_state.pool.reload_tokens(unique_tokens)

        await msg.answer(
            f"✅ <b>Список токенов обновлён!</b>\nЗагружено уникальных токенов: <code>{len(unique_tokens)}</code>",
            reply_markup=back_to_menu_keyboard("nav_tokens"),
            parse_mode="HTML",
        )
        await state.clear()

    @dp.callback_query(F.data == "tokens_cleanup_401")
    async def cb_tokens_cleanup_401(cb: CallbackQuery):
        tokens = scanner_state.pool.get_tokens() if scanner_state.pool else []
        if not tokens:
            await cb.answer("Токенов нет", show_alert=True)
            return

        await cb.answer("🧹 Проверяем и удаляем протухшие токены...")
        await safe_edit_text(cb.message, "⏳ <i>Идёт проверка токенов для очистки...</i>", parse_mode="HTML")

        valid_tokens = []
        removed = []
        for tok in tokens:
            ok, msg, _ = await verify_token_async(tok)
            if ok:
                valid_tokens.append(tok)
            elif "401" in msg:
                removed.append(tok)
            else:
                # Ошибки сети или 429 не удаляем на всякий случай
                valid_tokens.append(tok)

        if not valid_tokens and removed:
            await safe_edit_text(
                cb.message,
                "⚠️ <b>Внимание!</b> Все токены вернули ошибку 401. Они не были удалены, чтобы сканер не остался без токенов.\n"
                "Пожалуйста, добавьте новый токен через кнопку «Добавить токен».",
                reply_markup=tokens_keyboard(),
                parse_mode="HTML",
            )
            return

        if removed:
            save_tokens(valid_tokens)
            if scanner_state.pool:
                scanner_state.pool.reload_tokens(valid_tokens)
            text = (
                f"🧹 <b>Очистка завершена!</b>\n\n"
                f"Удалено протухших (401): <code>{len(removed)}</code>\n"
                f"Осталось активных: <code>{len(valid_tokens)}</code>"
            )
        else:
            text = "✅ <b>Протухших токенов (401) не обнаружено!</b> Все токены работают."

        await safe_edit_text(cb.message, text, reply_markup=tokens_keyboard(), parse_mode="HTML")

    # ── Раздел: Настройки ────────────────────────────────────────────────
    @dp.callback_query(F.data == "nav_settings")
    async def cb_nav_settings(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        text = format_settings_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=settings_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "toggle_autobuy")
    async def cb_toggle_autobuy(cb: CallbackQuery):
        scanner_state.auto_buy = not scanner_state.auto_buy
        save_settings(scanner_state)
        status_text = "включена 🟢" if scanner_state.auto_buy else "выключена 🔴"
        await cb.answer(f"Авто-покупка {status_text}")
        text = format_settings_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=settings_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "toggle_use_direct")
    async def cb_toggle_use_direct(cb: CallbackQuery):
        scanner_state.use_direct = not scanner_state.use_direct
        if scanner_state.pool:
            scanner_state.pool.set_use_direct(scanner_state.use_direct)
        save_settings(scanner_state)
        status_text = "включён 🟢 (1 слот напрямую)" if scanner_state.use_direct else "выключен 🔴 (все через VPN)"
        await cb.answer(f"Прямой IP: {status_text}")
        text = format_settings_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=settings_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "toggle_balance_filter")
    async def cb_toggle_balance_filter(cb: CallbackQuery):
        scanner_state.filter_by_balance = not scanner_state.filter_by_balance
        save_settings(scanner_state)
        status_text = "включён 🟢" if scanner_state.filter_by_balance else "выключен 🔴"
        await cb.answer(f"Фильтр по балансу {status_text}")
        if scanner_state.filter_by_balance and scanner_state.primary_balance_nano is None and scanner_state.pool:
            prim = scanner_state.pool.primary_token
            if prim:
                ok, _, bdata = await verify_token_async(prim)
                if ok and "hard" in bdata:
                    scanner_state.primary_balance_nano = int(bdata["hard"])
        text = format_settings_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=settings_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "toggle_eval_mode")
    async def cb_toggle_eval_mode(cb: CallbackQuery):
        cur = getattr(scanner_state, "eval_mode", "tiered")
        scanner_state.eval_mode = "fixed" if cur == "tiered" else "tiered"
        save_settings(scanner_state)
        mode_label = "🪜 Ступенчатый (3 тира)" if scanner_state.eval_mode == "tiered" else "📏 Фиксированный"
        await cb.answer(f"Режим оценки: {mode_label}")
        text = format_settings_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=settings_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "set_turnover_ratio")
    async def cb_set_turnover_ratio(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_turnover_ratio)
        await cb.answer()
        cur = f"{scanner_state.min_turnover_ratio:.1f}x" if scanner_state.min_turnover_ratio > 0 else "выключен (0.0)"
        await safe_edit_text(
            cb.message,
            f"📊 <b>Фильтр по обороту (оборот / цена) для NFT</b>\n\n"
            f"Текущий порог: <code>{cur}</code>\n\n"
            f"Отсекает «мёртвый груз» — лоты из непопулярных коллекций с низким оборотом.\n"
            f"Формула: <code>объём_коллекции / цена_подарка &gt;= X</code>\n"
            f"<i>💡 Не распространяется на лоты с чёрным фоном и дешевле {scanner_state.cheap_price_threshold:.1f} TON.</i>\n\n"
            f"Введите минимальный коэффициент (например <code>10.0</code> или <code>0</code> для отключения фильтра):",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )
        await cb.answer()

    @dp.message(BotStates.waiting_for_turnover_ratio)
    async def msg_set_turnover_ratio(msg: Message, state: FSMContext):
        try:
            val = float(msg.text.replace(",", ".").strip())
            if val < 0:
                raise ValueError
            scanner_state.min_turnover_ratio = val
            save_settings(scanner_state)
            txt = f"<code>{val:.1f}x</code>" if val > 0 else "выключен (0.0)"
            await msg.answer(f"✅ Фильтр оборота установлен: {txt}", reply_markup=back_to_menu_keyboard("nav_settings"), parse_mode="HTML")
            await state.clear()
        except ValueError:
            await msg.answer("❌ Пожалуйста, введите неотрицательное число (например 10.0 или 0):")

    @dp.callback_query(F.data == "nav_select_primary")
    async def cb_nav_select_primary(cb: CallbackQuery):
        tokens = scanner_state.pool.get_tokens() if scanner_state.pool else []
        if not tokens:
            await cb.answer("В пуле нет токенов", show_alert=True)
            return

        await cb.answer()
        kb_rows = []
        for i, tok in enumerate(tokens):
            is_prim = (i == 0)
            prefix = "👑 " if is_prim else ""
            label = f"{prefix}{i+1}. {_mask_token(tok)}"
            kb_rows.append([InlineKeyboardButton(text=label, callback_data=f"set_primary_{i}")])
        kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="nav_settings")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

        text = (
            "👑 <b>Выбор основного аккаунта</b>\n\n"
            "С основного аккаунта:\n"
            "• Проверяется баланс TON для фильтрации\n"
            "• Будут совершаться покупки\n\n"
            "Выберите аккаунт из списка ниже:"
        )
        await safe_edit_text(cb.message, text, reply_markup=kb, parse_mode="HTML")

    @dp.callback_query(F.data.startswith("set_primary_"))
    async def cb_set_primary_token(cb: CallbackQuery):
        idx_str = cb.data.replace("set_primary_", "")
        try:
            idx = int(idx_str)
            tokens = scanner_state.pool.get_tokens() if scanner_state.pool else []
            if 0 <= idx < len(tokens):
                target_tok = tokens[idx]
                if scanner_state.pool:
                    scanner_state.pool.set_primary_token(target_tok)
                save_settings(scanner_state)
                ok, _, bdata = await verify_token_async(target_tok)
                if ok and "hard" in bdata:
                    scanner_state.primary_balance_nano = int(bdata["hard"])
                await cb.answer(f"👑 Аккаунт {_mask_token(target_tok)} назначен основным!")
        except Exception as e:
            log.error("Ошибка смены основного токена: %s", e)
            await cb.answer("Ошибка смены токена", show_alert=True)

        text = format_settings_text(scanner_state)
        await safe_edit_text(cb.message, text, reply_markup=settings_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "set_min_diff")
    async def cb_set_min_diff(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_min_ton_diff)
        await cb.answer()
        await safe_edit_text(
            cb.message,
            f"✏️ Текущий порог выгоды: <code>{scanner_state.min_ton_diff:.2f} TON</code>\n\n"
            f"Введите новое значение в TON (например <code>2.0</code> или <code>3.5</code>):",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )

    @dp.message(BotStates.waiting_for_min_ton_diff)
    async def msg_set_min_diff(msg: Message, state: FSMContext):
        try:
            val = float(msg.text.replace(",", ".").strip())
            if val <= 0:
                raise ValueError
            scanner_state.min_ton_diff = val
            save_settings(scanner_state)
            await msg.answer(f"✅ Порог выгоды изменён на <code>{val:.2f} TON</code>", reply_markup=back_to_menu_keyboard("nav_settings"), parse_mode="HTML")
            await state.clear()
        except ValueError:
            await msg.answer("❌ Пожалуйста, введите корректное положительное число (например 2.5):")

    @dp.callback_query(F.data == "set_min_margin")
    async def cb_set_min_margin(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_min_margin)
        await cb.answer()
        cur_m = getattr(scanner_state, "min_margin_pct", 5.0)
        await safe_edit_text(
            cb.message,
            f"📈 <b>Минимальная чистая маржа (ROI)</b>\n\n"
            f"Текущий порог: <code>{cur_m:.1f}%</code>\n\n"
            f"Защищает от покупки дорогих подарков с небольшой разницей в цене, "
            f"где комиссии биржи (2% с продажи + 0.1 TON за выставление) съедают прибыль или ведут к убытку.\n\n"
            f"Введите новое значение в процентах (например <code>5.0</code> или <code>7.5</code>):",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )

    @dp.message(BotStates.waiting_for_min_margin)
    async def msg_set_min_margin(msg: Message, state: FSMContext):
        try:
            val = float(msg.text.replace(",", ".").replace("%", "").strip())
            if val < 0:
                raise ValueError
            scanner_state.min_margin_pct = val
            save_settings(scanner_state)
            await msg.answer(
                f"✅ Минимальная чистая маржа изменена на <code>{val:.1f}%</code>",
                reply_markup=back_to_menu_keyboard("nav_settings"),
                parse_mode="HTML",
            )
            await state.clear()
        except ValueError:
            await msg.answer("❌ Пожалуйста, введите корректный процент (например 5.0):")

    @dp.callback_query(F.data == "set_cheap")
    async def cb_set_cheap(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_cheap_threshold)
        await cb.answer()
        await safe_edit_text(
            cb.message,
            f"✏️ Текущий порог дешёвых подарков: <code>{scanner_state.cheap_price_threshold:.2f} TON</code>\n\n"
            f"Введите новое значение в TON (например <code>3.0</code>):",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )

    @dp.message(BotStates.waiting_for_cheap_threshold)
    async def msg_set_cheap(msg: Message, state: FSMContext):
        try:
            val = float(msg.text.replace(",", ".").strip())
            if val <= 0:
                raise ValueError
            scanner_state.cheap_price_threshold = val
            save_settings(scanner_state)
            await msg.answer(f"✅ Порог дешёвых подарков изменён на <code>{val:.2f} TON</code>", reply_markup=back_to_menu_keyboard("nav_settings"), parse_mode="HTML")
            await state.clear()
        except ValueError:
            await msg.answer("❌ Пожалуйста, введите корректное положительное число (например 3.0):")

    @dp.callback_query(F.data == "set_max_price")
    async def cb_set_max_price(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_max_price)
        await cb.answer()
        cur_p = getattr(scanner_state, "max_gift_price_ton", 0.0)
        cur_str = f"{cur_p:.2f} TON" if cur_p > 0 else "выключен (0)"
        await safe_edit_text(
            cb.message,
            f"🛑 <b>Фильтр максимальной стоимости подарка</b>\n\n"
            f"Текущее ограничение: <code>{cur_str}</code>\n\n"
            f"Сканер будет полностью игнорировать любые подарки дороже этого порога.\n\n"
            f"Введите максимальную цену в TON (например <code>15.0</code>) или <code>0</code> для отключения ограничения:",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )

    @dp.message(BotStates.waiting_for_max_price)
    async def msg_set_max_price(msg: Message, state: FSMContext):
        try:
            val = float(msg.text.replace(",", ".").strip())
            if val < 0:
                raise ValueError
            scanner_state.max_gift_price_ton = val
            save_settings(scanner_state)
            if val == 0:
                resp = "✅ Фильтр максимальной цены <b>отключён</b> (все подарки рассматриваются)"
            else:
                resp = f"✅ Максимальная цена подарка ограничена: <code>{val:.2f} TON</code> (дороже игнорируются)"
            await msg.answer(resp, reply_markup=back_to_menu_keyboard("nav_settings"), parse_mode="HTML")
            await state.clear()
        except ValueError:
            await msg.answer("❌ Введите положительное число (например <code>15.0</code>) или <code>0</code> для выключения:")

    @dp.callback_query(F.data == "interval_minus_005")
    async def cb_interval_minus_005(cb: CallbackQuery):
        adaptor = getattr(scanner_state, "rate_adaptor", None)
        if adaptor and hasattr(adaptor, "shift_interval"):
            new_val = adaptor.shift_interval(-0.05)
        else:
            new_val = max(0.10, round(scanner_state.scan_interval - 0.05, 2))
        scanner_state.scan_interval = new_val
        save_settings(scanner_state)
        mode_str = "авто" if (adaptor and adaptor.is_auto) else "ручной"
        await safe_edit_text(
            cb.message,
            format_settings_text(scanner_state),
            reply_markup=settings_keyboard(scanner_state),
            parse_mode="HTML",
        )
        await cb.answer(f"Интервал: {new_val:.2f}с ({mode_str})")

    @dp.callback_query(F.data == "interval_plus_005")
    async def cb_interval_plus_005(cb: CallbackQuery):
        adaptor = getattr(scanner_state, "rate_adaptor", None)
        if adaptor and hasattr(adaptor, "shift_interval"):
            new_val = adaptor.shift_interval(0.05)
        else:
            new_val = min(5.0, round(scanner_state.scan_interval + 0.05, 2))
        scanner_state.scan_interval = new_val
        save_settings(scanner_state)
        mode_str = "авто" if (adaptor and adaptor.is_auto) else "ручной"
        await safe_edit_text(
            cb.message,
            format_settings_text(scanner_state),
            reply_markup=settings_keyboard(scanner_state),
            parse_mode="HTML",
        )
        await cb.answer(f"Интервал: {new_val:.2f}с ({mode_str})")

    @dp.callback_query(F.data == "set_interval")
    async def cb_set_interval(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_scan_interval)
        await cb.answer()
        adaptor = getattr(scanner_state, "rate_adaptor", None)
        mode_str = "авто" if (adaptor and adaptor.is_auto) else "ручной"
        p429 = scanner_state.get_429_count_last_hour()
        p429_text = f"⚠️ Штрафов 429 за последний час: <b>{p429}</b>\n\n" if p429 > 0 else "Штрафов 429 за последний час: <code>0</code>\n\n"
        await safe_edit_text(
            cb.message,
            f"✏️ Текущий интервал: <code>{scanner_state.scan_interval:.2f} с</code> ({mode_str})\n"
            f"{p429_text}"
            f"Введите новый интервал в секундах (например <code>0.5</code>).\n"
            f"<i>После ручной установки авто-адаптация отключается.</i>\n"
            f"Введите <code>auto</code> для возврата в авто-режим:",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )

    @dp.message(BotStates.waiting_for_scan_interval)
    async def msg_set_interval(msg: Message, state: FSMContext):
        raw = (msg.text or "").strip().lower()
        adaptor = getattr(scanner_state, "rate_adaptor", None)
        if raw == "auto":
            if adaptor:
                adaptor.set_auto()
                scanner_state.scan_interval = adaptor.interval
            save_settings(scanner_state)
            await msg.answer(f"✅ Авто-режим интервала включён (текущий: <code>{scanner_state.scan_interval:.2f} с</code>)", reply_markup=back_to_menu_keyboard("nav_settings"), parse_mode="HTML")
            await state.clear()
            return
        try:
            val = float(raw.replace(",", "."))
            if val < 0.1:
                raise ValueError
            scanner_state.scan_interval = val
            if adaptor:
                adaptor.set_manual(val)
            save_settings(scanner_state)
            await msg.answer(f"✅ Интервал изменён на <code>{val:.2f} с</code> (ручной режим)", reply_markup=back_to_menu_keyboard("nav_settings"), parse_mode="HTML")
            await state.clear()
        except ValueError:
            await msg.answer("❌ Введите положительное число >= 0.1 или <code>auto</code>:", parse_mode="HTML")


    # ── Раздел: Просмотр логов ────────────────────────────────────────────
    async def _send_logs_to_user(target: Union[Message, CallbackQuery], query_str: str):
        log_dir = Path(os.getenv("LOG_DIR", "logs"))
        title, lines = read_logs_smart(query_str, log_dir)

        send_fn = target.answer if isinstance(target, Message) else target.message.answer

        if not lines:
            await send_fn(
                f"📋 <b>{html.escape(title)}</b>",
                parse_mode="HTML",
                reply_markup=back_to_menu_keyboard("nav_logs"),
            )
            return

        header = f"<b>{html.escape(title)}</b>\n\n"
        full_body = "\n".join(lines)
        if len(header) + len(full_body) + 15 <= 4000:
            await send_fn(
                f"{header}<pre>{html.escape(full_body)}</pre>",
                parse_mode="HTML",
                reply_markup=back_to_menu_keyboard("nav_logs"),
            )
            return

        # Разбиваем на порции для Telegram (< 3800 символов)
        chunks = []
        cur_lines = []
        cur_len = 0
        for l in lines:
            if cur_len + len(l) + 1 > 3500:
                chunks.append("\n".join(cur_lines))
                cur_lines = [l]
                cur_len = len(l)
            else:
                cur_lines.append(l)
                cur_len += len(l) + 1
        if cur_lines:
            chunks.append("\n".join(cur_lines))

        for idx, chunk in enumerate(chunks):
            is_last = (idx == len(chunks) - 1)
            prefix = header if idx == 0 else ""
            markup = back_to_menu_keyboard("nav_logs") if is_last else None
            await send_fn(
                f"{prefix}<pre>{html.escape(chunk)}</pre>",
                parse_mode="HTML",
                reply_markup=markup,
            )
            if not is_last:
                await asyncio.sleep(0.15)

    @dp.callback_query(F.data == "nav_logs")
    async def cb_nav_logs(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_log_time)
        await cb.answer()
        now = datetime.now()
        now_str = now.strftime("%H:%M:%S")
        text = (
            "📋 <b>Просмотр логов</b>\n\n"
            f"🕒 Время сервера: <code>{now_str}</code>\n\n"
            "Нажмите быструю кнопку или отправьте:\n"
            "• <code>now</code> или число (напр. <code>50</code>) — последние строки\n"
            "• Время <code>11:04:53</code> или <code>11:04</code> — поиск (±30с)\n"
            "• Слово: <code>buy</code>, <code>429</code>, <code>error</code> — фильтр"
        )
        await safe_edit_text(cb.message, text, reply_markup=logs_keyboard(), parse_mode="HTML")

    @dp.callback_query(F.data == "logs_tail_30")
    async def cb_logs_tail_30(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        await _send_logs_to_user(cb, "30")

    @dp.callback_query(F.data == "logs_tail_100")
    async def cb_logs_tail_100(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        await _send_logs_to_user(cb, "100")

    @dp.callback_query(F.data == "logs_filter_buy")
    async def cb_logs_filter_buy(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        await _send_logs_to_user(cb, "buy")

    @dp.callback_query(F.data == "logs_filter_err")
    async def cb_logs_filter_err(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        await _send_logs_to_user(cb, "err")

    @dp.message(BotStates.waiting_for_log_time)
    async def msg_log_time(msg: Message, state: FSMContext):
        raw = (msg.text or "").strip()
        await state.clear()
        await _send_logs_to_user(msg, raw)

    # ── Кнопка: Загрузить обновление (Git Pull & Restart) ─────────────────
    @dp.callback_query(F.data == "btn_git_update")
    async def cb_btn_git_update(cb: CallbackQuery):
        if not is_admin(cb.from_user.id if cb.from_user else None, admin_ids):
            await cb.answer("⛔ Нет доступа", show_alert=True)
            return

        await cb.answer()
        status_msg = await cb.message.answer(
            "⏳ <b>Загрузка обновления...</b>\n\nВыполняю <code>git pull origin main</code>...",
            parse_mode="HTML",
        )

        try:
            # Настройка git safe.directory на случай работы в Docker с разными правами
            try:
                conf_proc = await asyncio.create_subprocess_exec(
                    "git", "config", "--global", "--add", "safe.directory", "*",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await conf_proc.communicate()
            except Exception:
                pass

            repo_dir = Path(__file__).resolve().parent

            # Проверяем текущий коммит
            p_local = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(repo_dir),
            )
            out_local, _ = await p_local.communicate()
            current_head = out_local.decode("utf-8", errors="replace").strip()

            # Выполняем fetch
            fetch_proc = await asyncio.create_subprocess_exec(
                "git", "fetch", "origin", "main",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(repo_dir),
            )
            out_fetch, err_fetch = await fetch_proc.communicate()
            if fetch_proc.returncode != 0:
                err_msg = err_fetch.decode("utf-8", errors="replace").strip() or out_fetch.decode("utf-8", errors="replace").strip()
                await status_msg.edit_text(
                    f"❌ <b>Ошибка при связи с GitHub (git fetch):</b>\n\n"
                    f"<code>{html.escape(err_msg or 'Не удалось подключиться к GitHub')}</code>",
                    parse_mode="HTML",
                    reply_markup=back_to_menu_keyboard("nav_main"),
                )
                return

            # Проверяем удаленный коммит
            p_remote = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "origin/main",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(repo_dir),
            )
            out_remote, _ = await p_remote.communicate()
            remote_head = out_remote.decode("utf-8", errors="replace").strip()

            if current_head and remote_head and current_head == remote_head:
                await status_msg.edit_text(
                    f"✅ <b>Бот уже обновлен до последней версии!</b>\n\n"
                    f"Текущий коммит: <code>{current_head[:8]}</code>\n"
                    f"Новых изменений в репозитории нет.",
                    parse_mode="HTML",
                    reply_markup=back_to_menu_keyboard("nav_main"),
                )
                return

            proc = await asyncio.create_subprocess_exec(
                "git", "reset", "--hard", "origin/main",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(repo_dir),
            )
            stdout, stderr = await proc.communicate()
            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                await status_msg.edit_text(
                    f"❌ <b>Ошибка при обновлении git (код {proc.returncode}):</b>\n\n"
                    f"<code>{html.escape(err or out or 'Неизвестная ошибка')}</code>",
                    parse_mode="HTML",
                    reply_markup=back_to_menu_keyboard("nav_main"),
                )
                return

            # Успешно стянуто обновление
            await status_msg.edit_text(
                f"✅ <b>Обновление успешно загружено!</b>\n\n"
                f"<code>{html.escape(out)}</code>\n\n"
                "🔄 <b>Перезапуск бота...</b>\n"
                "<i>Бот применит изменения через 2 секунды.</i>",
                parse_mode="HTML",
            )
            log.info("🚀 Обновление через Git успешно применено: %s. Перезапуск бота...", out)

            async def _do_restart():
                await asyncio.sleep(2.0)
                os._exit(0)

            asyncio.create_task(_do_restart())

        except Exception as e:
            log.error("Ошибка при обновлении бота: %s", e, exc_info=True)
            await status_msg.edit_text(
                f"❌ <b>Ошибка:</b>\n<code>{html.escape(str(e))}</code>",
                parse_mode="HTML",
                reply_markup=back_to_menu_keyboard("nav_main"),
            )

    # ── Раздел: Прокси и Пинг ────────────────────────────────────────────
    @dp.callback_query(F.data == "nav_proxies")
    async def cb_nav_proxies(cb: CallbackQuery):
        await cb.answer()
        pool = scanner_state.pool
        proxies = pool.get_proxies() if pool else []
        reserves = getattr(pool, "_reserve_proxies", []) if pool else []
        quarantined = getattr(pool, "_quarantined_proxies", []) if pool else []
        direct_str = " (Слот #1 напрямую с IP сервера)" if getattr(scanner_state, "use_direct", False) else ""

        fail_cnt, fail_last = scanner_state.get_proxy_failures_stats()
        if fail_cnt > 0:
            fail_line = f"\n⚠️ <b>Сбоев прокси за 2ч:</b> <code>{fail_cnt}</code> (последний: <code>{fail_last}</code>)"
        else:
            fail_line = "\n🛡️ <b>Сбоев прокси за 2ч:</b> <code>0</code> (всё стабильно)"

        if not proxies and not reserves:
            text = (
                f"🌐 <b>Прокси</b>{direct_str}{fail_line}\n\n"
                f"Прокси сейчас не назначены.\n"
                f"Нажмите <b>«🔍 Автопоиск быстрых прокси»</b> для автоматического скачивания, "
                f"замера пинга и наполнения активных слотов и резерва."
            )
        else:
            lines = [f"🌐 <b>Прокси в пуле</b> (Активных: <code>{len(proxies)}</code>, Резерв: <code>{len(reserves)}</code>){direct_str}:{fail_line}\n"]
            for i, p in enumerate(proxies, 1):
                lat = getattr(p, "ping_ms", 0)
                lat_str = f" | ⚡ <code>{lat:.0f} мс</code>" if lat > 0 else ""
                lines.append(f"{i}. <b>[{p.cfg.name}]</b>{lat_str}")
            if reserves:
                lines.append(f"\n📦 <b>В горячем резерве:</b> <code>{len(reserves)}</code> шт. (авто-замена при сбоях):")
                for idx, r in enumerate(reserves[:6], 1):
                    r_lat = getattr(r, "ping_ms", 0)
                    r_lat_str = f" | ⚡ <code>{r_lat:.0f} мс</code>" if r_lat > 0 else ""
                    lines.append(f"  • <b>[{r.cfg.name}]</b>{r_lat_str}")
                if len(reserves) > 6:
                    lines.append(f"  • <i>... и ещё {len(reserves) - 6} в резерве</i>")
            if quarantined:
                lines.append(f"\n⏳ <b>На авто-перепроверке:</b> <code>{len(quarantined)}</code> шт. (проверяются каждые 2 мин)")
            lines.append("\n💡 <i>Нажмите «🔍 Автопоиск», чтобы спарсить свежие прокси и отобрать топ с наименьшим пингом.</i>")
            text = "\n".join(lines)

        await safe_edit_text(cb.message, text, reply_markup=proxies_keyboard(), parse_mode="HTML")

    @dp.callback_query(F.data == "proxies_reping")
    async def cb_proxies_reping(cb: CallbackQuery):
        proxies = scanner_state.pool.get_proxies() if scanner_state.pool else []
        if not proxies:
            await cb.answer("Прокси нет", show_alert=True)
            return

        await cb.answer("⚡ Замеряем пинг прокси...")
        await safe_edit_text(cb.message, "⏳ <i>Замер пинга всех прокси к api.tgmrkt.io...</i>", parse_mode="HTML")

        tasks = [ping_proxy_async(p, timeout=2.5) for p in proxies]
        results = await asyncio.gather(*tasks)

        lines = [f"🌐 <b>Результаты замера пинга</b> ({len(proxies)} прокси):\n"]
        for p, (ok, lat, err) in zip(proxies, results):
            status = f"⚡ <code>{lat:.0f} мс</code> (OK)" if ok else f"❌ {err}"
            lines.append(f"• <b>[{p.cfg.name}]</b>: {status}")

        await safe_edit_text(cb.message, "\n".join(lines), reply_markup=proxies_keyboard(), parse_mode="HTML")

    @dp.callback_query(F.data == "proxies_autosearch")
    async def cb_proxies_autosearch(cb: CallbackQuery):
        pool = scanner_state.pool
        if not pool:
            await cb.answer("Пул аккаунтов не инициализирован", show_alert=True)
            return

        await cb.answer("🔍 Запущен автопоиск прокси...")
        await safe_edit_text(
            cb.message,
            "⏳ <b>Автопоиск и замер быстрых прокси (≤800 мс)...</b>\n\n"
            "• Проверяем ваши личные сохранённые прокси (custom)...\n"
            "• Скачиваем базы <b>Databay</b> и <b>Proxifly</b> (SOCKS5 / HTTP)...\n"
            "• Параллельно замеряем пинг каждого кандидата к <code>api.tgmrkt.io</code>...\n"
            "• <b>Бракуем любые серверы с пингом &gt; 800 мс</b>...\n\n"
            "<i>Обычно это занимает 10-15 секунд, пожалуйста подождите...</i>",
            parse_mode="HTML",
        )

        from proxy_finder import find_fastest_proxies, save_proxies_to_file

        try:
            custom_fast, public_fast, total_candidates = await find_fastest_proxies(
                max_candidates=1200,
                max_ping_ms=800.0,
                concurrency=120,
                max_results=35,
                include_custom=True,
            )
        except Exception as e:
            await safe_edit_text(
                cb.message,
                f"❌ <b>Ошибка при автопоиске:</b> {e}",
                reply_markup=proxies_keyboard(),
                parse_mode="HTML",
            )
            return

        all_fast = custom_fast + public_fast
        if not all_fast:
            await safe_edit_text(
                cb.message,
                "⚠️ <b>Ни один публичный прокси не прошёл порог скорости (≤800 мс).</b>\n\n"
                "Серверы с пингом выше 800 мс были отбракованы. "
                "Вы можете повторить поиск через пару минут или добавить свои приватные прокси кнопкой ниже.",
                reply_markup=proxies_keyboard(),
                parse_mode="HTML",
            )
            return

        # Применяем найденные прокси в активные слоты и горячий резерв
        pool.apply_new_proxies(all_fast)
        save_proxies_to_file(pool.get_proxies(), getattr(pool, "_reserve_proxies", []), use_direct=scanner_state.use_direct)

        active_count = len(pool.get_proxies())
        reserve_count = len(getattr(pool, "_reserve_proxies", []))
        best_ms = all_fast[0].ping_ms

        lines = [
            f"✅ <b>Автопоиск завершён успешно!</b>\n",
            f"🔍 Проверено кандидатов: <b>{total_candidates}</b> (Databay + Proxifly)",
            f"🛡 Фильтр: <b>строго ≤ 800 мс</b> (медленные забракованы)",
            f"⭐ Пользовательских в строю: <b>{len(custom_fast)}</b> шт.",
            f"🌐 Отобрано публичных: <b>{len(public_fast)}</b> шт.",
            f"⚡ Лучший пинг: <code>{best_ms:.0f} мс</code>",
            f"🟢 В активных слотах: <b>{active_count}</b> | 📦 В резерве: <b>{reserve_count}</b>\n",
            "<b>Топ серверов в пуле:</b>",
        ]
        for rank, p in enumerate(all_fast[:6], 1):
            lines.append(f"{rank}. <b>[{p.cfg.name}]</b> — <code>{p.ping_ms:.0f} мс</code>")

        lines.append("\n💡 <i>Ваши личные прокси сохранены и всегда имеют приоритет. При сбоях бот моментально берёт следующий прокси из резерва.</i>")

        await safe_edit_text(cb.message, "\n".join(lines), reply_markup=proxies_keyboard(), parse_mode="HTML")

    # ── Ручное управление своими прокси ──────────────────────────────────
    @dp.callback_query(F.data == "proxies_add_custom")
    async def cb_proxies_add_custom(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_custom_proxies)
        await cb.answer()
        await safe_edit_text(
            cb.message,
            "➕ <b>Добавление собственных прокси</b>\n\n"
            "Отправьте список ваших прокси (по одному на строку).\n\n"
            "<b>Поддерживаемые форматы:</b>\n"
            "• <code>socks5://user:pass@ip:port</code>\n"
            "• <code>http://user:pass@ip:port</code>\n"
            "• <code>vless://uuid@host:port?...#Name</code>\n"
            "• <code>trojan://pass@host:port?...#Name</code>\n"
            "• <code>ss://base64@host:port#Name</code>\n"
            "• <code>ip:port</code> <i>(будет распознан как SOCKS5)</i>\n\n"
            "🔒 <i>Ваши прокси сохраняются в persistent файл и <b>никогда не перезаписываются</b> при автопоиске. В пуле они всегда получают наивысший приоритет.</i>",
            reply_markup=back_to_menu_keyboard("nav_proxies"),
            parse_mode="HTML",
        )

    @dp.message(BotStates.waiting_for_custom_proxies)
    async def msg_add_custom_proxies(msg: Message, state: FSMContext):
        raw_lines = [l.strip() for l in msg.text.splitlines() if l.strip() and not l.strip().startswith("#")]
        if not raw_lines:
            await msg.answer("❌ Вы не ввели ни одного валидного прокси.", reply_markup=back_to_menu_keyboard("nav_proxies"))
            await state.clear()
            return

        from proxy_finder import add_custom_proxies, ping_custom_proxy, save_proxies_to_file
        added_count, all_custom = add_custom_proxies(raw_lines)

        wait_msg = await msg.answer("⏳ <i>Проверяем пинг добавленных прокси к api.tgmrkt.io...</i>", parse_mode="HTML")

        sem = asyncio.Semaphore(10)
        tasks = [ping_custom_proxy(l, idx, sem, max_ping_ms=2000.0) for idx, l in enumerate(raw_lines)]
        results = await asyncio.gather(*tasks)

        working_custom: list[Any] = []
        result_lines = [f"✅ <b>Добавлено прокси:</b> {added_count} шт. (всего ваших: {len(all_custom)})\n"]
        for line, res in zip(raw_lines, results):
            short = line.split("@")[-1] if "@" in line else line
            if len(short) > 40:
                short = short[:38] + "…"
            if res is not None:
                p_obj, lat = res
                working_custom.append(p_obj)
                warn = " <i>(&gt;800мс)</i>" if lat > 800 else ""
                result_lines.append(f"• 🟢 <code>{short}</code> — <b>{lat:.0f} мс</b>{warn}")
            else:
                result_lines.append(f"• 🔴 <code>{short}</code> — <i>не отвечает / ошибка</i>")

        # Если есть подключённый пул, сразу внедряем их в слоты
        pool = scanner_state.pool
        if pool and working_custom:
            pool.add_reserve_proxies(working_custom)
            save_proxies_to_file(pool.get_proxies(), getattr(pool, "_reserve_proxies", []), use_direct=scanner_state.use_direct)
            result_lines.append(f"\n⚡ <b>Подключено в пул:</b> +{len(working_custom)} рабочих прокси.")

        await wait_msg.delete()
        await msg.answer(
            "\n".join(result_lines),
            reply_markup=proxies_keyboard(),
            parse_mode="HTML",
        )
        await state.clear()

    @dp.callback_query(F.data == "proxies_list_custom")
    async def cb_proxies_list_custom(cb: CallbackQuery):
        await cb.answer()
        from proxy_finder import load_custom_proxies
        custom_lines = load_custom_proxies()
        if not custom_lines:
            text = (
                "📋 <b>Ваши сохранённые прокси</b>\n\n"
                "Вы ещё не добавили ни одного своего прокси.\n"
                "Нажмите <b>«➕ Добавить свои прокси»</b>, чтобы привязать свои личные серверы."
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить свои прокси", callback_data="proxies_add_custom")],
                    [InlineKeyboardButton(text="⬅️ К списку прокси", callback_data="nav_proxies")],
                ]
            )
        else:
            lines = [f"📋 <b>Ваши сохранённые прокси</b> (Всего: <code>{len(custom_lines)}</code>):\n"]
            for i, l in enumerate(custom_lines, 1):
                mask = l.split("@")[-1] if "@" in l else l
                lines.append(f"{i}. <code>{mask}</code>")
            lines.append("\n💡 <i>Эти прокси защищены от удаления при перепоиске и имеют наивысший приоритет.</i>")
            text = "\n".join(lines)
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="proxies_add_custom")],
                    [InlineKeyboardButton(text="🗑 Очистить мои прокси", callback_data="proxies_clear_custom")],
                    [InlineKeyboardButton(text="⬅️ К списку прокси", callback_data="nav_proxies")],
                ]
            )

        await safe_edit_text(cb.message, text, reply_markup=kb, parse_mode="HTML")

    @dp.callback_query(F.data == "proxies_clear_custom")
    async def cb_proxies_clear_custom(cb: CallbackQuery):
        from proxy_finder import save_custom_proxies
        save_custom_proxies([])
        await cb.answer("Ваши сохранённые прокси очищены", show_alert=True)
        text = "🗑 <b>Список ваших личных прокси очищен.</b>\n\nВ пуле теперь используются только публичные серверы."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить новые", callback_data="proxies_add_custom")],
                [InlineKeyboardButton(text="⬅️ К списку прокси", callback_data="nav_proxies")],
            ]
        )
        await safe_edit_text(cb.message, text, reply_markup=kb, parse_mode="HTML")

    # ── Ручная покупка подарка из уведомления ─────────────────────────────
    @dp.callback_query(F.data.startswith("buy:"))
    async def cb_buy_gift(cb: CallbackQuery):
        raw_parts = cb.data.split(":")
        if len(raw_parts) < 3:
            await cb.answer("Неверные данные лота", show_alert=True)
            return

        gift_id = raw_parts[1]
        try:
            price_nano = int(raw_parts[2])
        except ValueError:
            await cb.answer("Некорректная цена лота", show_alert=True)
            return

        if gift_id in scanner_state.buying_in_progress:
            await cb.answer("⏳ Покупка уже выполняется...", show_alert=True)
            return

        scanner_state.buying_in_progress.add(gift_id)
        await cb.answer("⚡ Отправка запроса на покупку...")

        pool = scanner_state.pool
        prim_slot = pool.get_primary_slot() if pool else None
        if not prim_slot:
            scanner_state.buying_in_progress.discard(gift_id)
            await cb.answer("❌ Нет активного основного аккаунта", show_alert=True)
            return

        # Оптимистично уменьшаем баланс
        if scanner_state.primary_balance_nano is not None:
            scanner_state.primary_balance_nano = max(0, scanner_state.primary_balance_nano - price_nano)

        t_start = time.monotonic()
        ok, msg, item = await buy_gift_async(
            gift_id=gift_id,
            price_nano=price_nano,
            token=prim_slot.token,
            proxies=prim_slot.proxies,
        )
        elapsed = time.monotonic() - t_start

        # Фоновое обновление точного баланса
        async def _refresh_bal():
            try:
                ok_b, _, bdata = await verify_token_async(prim_slot.token, proxy=prim_slot.proxy)
                if ok_b and "hard" in bdata:
                    scanner_state.primary_balance_nano = int(bdata["hard"])
            except Exception:
                pass

        asyncio.create_task(_refresh_bal())

        alert_info = scanner_state.sent_alerts.pop(gift_id, None)
        deal = alert_info.get("deal") if alert_info else {}
        gift = deal.get("gift", {}) if deal else {}
        col_name = gift.get("collectionName", "NFT")
        mod_name = gift.get("modelName", "")
        num = gift.get("number") or gift.get("num") or "?"
        price_ton = price_nano / 1e9

        scanner_state.buying_in_progress.discard(gift_id)

        if ok:
            in_vault = await verify_gift_in_vault_async(gift_id, prim_slot.token, prim_slot.proxies)
            vault_str = "Подтверждено в Хранилище ✅" if in_vault else "В Хранилище (по чеку покупки) ✅"
            bal_str = (
                f"~{scanner_state.primary_balance_nano / 1e9:.2f} TON"
                if scanner_state.primary_balance_nano is not None
                else "обновляется"
            )
            success_text = (
                f"🎉 <b>УСПЕШНАЯ ПОКУПКА!</b>\n\n"
                f"🎁 <b>{col_name} — {mod_name} #{num}</b>\n"
                f"💰 <b>Куплено за:</b> <code>{price_ton:.2f} TON</code>\n"
                f"📦 <b>Статус:</b> {vault_str}\n"
                f"💳 <b>Остаток баланса:</b> <code>{bal_str}</code>\n"
                f"⏱ <b>Время выкупа:</b> <code>{elapsed:.2f} с</code>\n"
            )
            nft_url = make_telegram_nft_url(col_name, num)
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Выкуплено вами", callback_data="noop")],
                    [InlineKeyboardButton(text="🎁 Открыть NFT в Telegram", url=nft_url)],
                ]
            )
            try:
                await cb.message.edit_text(success_text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                await cb.message.reply(success_text, reply_markup=kb, parse_mode="HTML")
        else:
            await cb.answer(f"❌ Не удалось купить: {msg}", show_alert=True)
            nft_url = alert_info.get("nft_url", "https://t.me/nft") if alert_info else "https://t.me/nft"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=f"❌ Ошибка ({msg[:25]})", callback_data="noop")],
                    [InlineKeyboardButton(text="🎁 Открыть NFT в Telegram", url=nft_url)],
                ]
            )
            try:
                await cb.message.edit_reply_markup(reply_markup=kb)
            except Exception:
                pass

    @dp.callback_query(F.data == "noop")
    async def cb_noop(cb: CallbackQuery):
        await cb.answer()

    # ── Запуск Polling ───────────────────────────────────────────────────

    log.info("Telegram бот запущен для администраторов: %s", admin_ids)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()


# ─────────────────────────────────────────────
#  Умное чтение логов
# ─────────────────────────────────────────────

def read_logs_smart(query: str, log_dir: Path, max_lines: int = 50) -> tuple[str, list[str]]:
    """
    Умное чтение логов:
    - 'now', пусто, или число (напр. '50') -> последние N строк из scanner.log
    - 'buy', 'покупка' -> последние логи покупок
    - 'err', 'error', 'ошибки' -> последние ошибки и предупреждения
    - время '11:04:53' или '11:04' -> поиск строк в окрестности времени
    - ключевое слово -> строки содержащие слово
    """
    raw = (query or "").strip()
    raw_lower = raw.lower()

    candidates: list[Path] = []
    main_log = log_dir / "scanner.log"
    if main_log.exists():
        candidates.append(main_log)
    for p in sorted(log_dir.glob("scanner.log.*"), key=lambda f: f.stat().st_mtime, reverse=True):
        if p not in candidates:
            candidates.append(p)

    if not candidates:
        return "Лог-файлы не найдены в директории logs.", []

    # 1. Запрос хвоста логов (tail): 'now', пусто, или число (напр. '30', '50', '100')
    is_tail = False
    n_lines = max_lines
    if not raw or raw_lower in ("now", "сейчас", "хвост", "последние", "tail"):
        is_tail = True
        n_lines = 40
    elif raw.isdigit():
        is_tail = True
        n_lines = max(5, min(int(raw), 150))

    if is_tail:
        collected: collections.deque[str] = collections.deque(maxlen=n_lines)
        for log_path in candidates:
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line_s = line.rstrip()
                        if line_s:
                            collected.append(line_s)
                if len(collected) >= n_lines:
                    break
            except OSError:
                pass
        title = f"📋 Последние {len(collected)} строк лога:"
        return title, list(collected)

    # 2. Фильтр покупок ('buy', 'покупка', 'autobuy', 'сделка')
    if raw_lower in ("buy", "покупка", "покупки", "autobuy", "сделка", "сделки"):
        buy_lines: list[str] = []
        keywords = ("buy", "покуп", "купил", "сделка", "[buy]", "[vault]")
        for log_path in candidates:
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line_lower = line.lower()
                        if any(k in line_lower for k in keywords):
                            buy_lines.append(line.rstrip())
            except OSError:
                pass
            if len(buy_lines) >= 60:
                break
        buy_res = buy_lines[-50:]
        title = f"🛒 Логи покупок ({len(buy_res)}):" if buy_res else "🛒 Логи покупок пока пусты."
        return title, buy_res

    # 3. Фильтр ошибок ('err', 'error', 'ошибки', 'warn', 'warning')
    if raw_lower in ("err", "error", "errors", "ошибка", "ошибки", "warn", "warning"):
        err_lines: list[str] = []
        keywords = ("[error", "[warn", "[critical", "traceback", "exception")
        for log_path in candidates:
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line_lower = line.lower()
                        if any(k in line_lower for k in keywords):
                            err_lines.append(line.rstrip())
            except OSError:
                pass
            if len(err_lines) >= 60:
                break
        err_res = err_lines[-50:]
        title = f"⚠️ Ошибки и предупреждения ({len(err_res)}):" if err_res else "✅ Ошибок в логах не обнаружено."
        return title, err_res

    # 4. Поиск по времени (ЧЧ:ММ:СС или ЧЧ:ММ)
    time_match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", raw)
    if time_match:
        h = int(time_match.group(1))
        m = int(time_match.group(2))
        s = int(time_match.group(3)) if time_match.group(3) is not None else 0
        has_sec = time_match.group(3) is not None
        window = 30 if has_sec else 60

        def search_by_time(target_h: int, target_m: int, target_s: int, win_s: int) -> list[str]:
            target_sec = (target_h % 24) * 3600 + (target_m % 60) * 60 + (target_s % 60)
            res: list[str] = []
            _re_ts = re.compile(r"(\d{2}):(\d{2}):(\d{2})")
            for log_path in candidates:
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            m_ts = _re_ts.search(line[:30])
                            if not m_ts:
                                continue
                            lh, lm, ls = int(m_ts.group(1)), int(m_ts.group(2)), int(m_ts.group(3))
                            l_sec = lh * 3600 + lm * 60 + ls
                            diff = abs(l_sec - target_sec)
                            if diff <= win_s or diff >= (86400 - win_s):
                                res.append(line.rstrip())
                except OSError:
                    pass
                if len(res) >= 100:
                    break
            return res

        matched = search_by_time(h, m, s, window)
        note = ""
        if not matched:
            for offset_hours in (-3, -4, 3, 4):
                try_matched = search_by_time((h + offset_hours) % 24, m, s, window)
                if try_matched:
                    matched = try_matched
                    sign = "+" if offset_hours > 0 else ""
                    note = f" (с поправкой на часовой пояс: {sign}{offset_hours}ч)"
                    break

        if matched:
            return f"📋 Логи около {raw}{note} ({len(matched)} строк):", matched[-80:]

        latest_ts = "неизвестно"
        try:
            with open(candidates[0], "r", encoding="utf-8", errors="replace") as f:
                for line in collections.deque(f, maxlen=10):
                    m_ts = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    if m_ts:
                        latest_ts = m_ts.group(1)
        except Exception:
            pass

        now_srv = datetime.now().strftime("%H:%M:%S")
        return (
            f"❌ В районе времени {raw} записей не найдено.\n"
            f"🕒 Время сервера: {now_srv}\n"
            f"🕒 Последняя запись в логе: {latest_ts}",
            [],
        )

    found_lines: list[str] = []
    q_low = raw_lower
    for log_path in candidates:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if q_low in line.lower():
                        found_lines.append(line.rstrip())
        except OSError:
            pass
        if len(found_lines) >= 80:
            break

    if found_lines:
        return f"🔍 Найдено по запросу '{raw}' ({len(found_lines)} строк):", found_lines[-60:]
    else:
        return f"❌ По запросу '{raw}' ничего не найдено в логах.", []


def read_log_window(timestamp_str: str, log_dir: Path, window_sec: int = 10) -> list[str]:
    """Сохраняем обратную совместимость."""
    _, lines = read_logs_smart(timestamp_str, log_dir)
    return lines


# ─────────────────────────────────────────────
#  Отправка Deal Alerts в Telegram
# ─────────────────────────────────────────────

async def send_deal_notification(
    bot_token: str,
    admin_ids: set[int],
    deal: dict,
    bot: Optional[Bot] = None,
    scanner_state: Optional["ScannerState"] = None,
    older_ids: Optional[set[str]] = None,
    with_buy_button: bool = True,
) -> None:
    """Отправляет богато оформленное уведомление о выгодной сделке в Telegram."""
    if not bot_token or not admin_ids:
        return

    gift = deal.get("gift", {})
    deal_type = deal.get("type", "")
    price = deal.get("price", 0)
    floor = deal.get("floor", 0)
    diff_ton = deal.get("diff_ton", 0.0)
    pct = deal.get("pct", 0.0)
    price_ton = price / 1e9
    floor_ton = floor / 1e9

    tags = {
        "BLACK":  "🖤 <b>ВЫГОДНЫЙ ЧЁРНЫЙ ФОН</b>",
        "CHEAP":  "💸 <b>СВЕРХДЕШЁВЫЙ ПОДАРОК</b>",
        "NFT":    "🎯 <b>НИЖЕ ФЛОРА КОЛЛЕКЦИИ</b>",
        "LOW_ID": "🏷️ <b>РЕДКИЙ НОМЕР (#1 — #99)</b>",
    }
    header = tags.get(deal_type, "🔥 <b>ВЫГОДНАЯ СДЕЛКА!</b>")

    col_name = gift.get("collectionName", "Unknown")
    mod_name = gift.get("modelName", "")
    num = gift.get("number") or gift.get("num") or "?"
    backdrop = gift.get("backdropName", "—")
    gift_id = gift.get("id", "")

    text = (
        f"{header}\n\n"
        f"🎁 <b>{col_name} — {mod_name} #{num}</b>\n"
        f"💰 <b>Цена:</b> <code>{price_ton:.2f} TON</code>\n"
        f"🎯 <b>Флор:</b> <code>{floor_ton:.2f} TON</code>\n"
        f"💵 <b>Выгода:</b> <code>{diff_ton:.2f} TON</code> (<b>{pct:.1f}%</b> скидка)\n"
    )
    if deal.get("net_profit_ton") is not None:
        net_profit = deal["net_profit_ton"]
        margin_pct = deal.get("margin_pct", 0.0)
        text += f"📈 <b>Чистыми (после 2% + 0.1 TON):</b> <code>+{net_profit:.2f} TON</code> (ROI: <b>{margin_pct:+.1f}%</b>)\n"
    if deal.get("tier"):
        rp = deal.get("req_profit", 0.0)
        rm = deal.get("req_margin", 0.0)
        text += f"🪜 <b>Тир сделки:</b> {deal['tier']} (требовалось: ≥ <code>{rp:.1f} TON</code> и <code>{rm:.1f}%</code>)\n"
    text += f"🎨 <b>Фон:</b> {backdrop}\n"

    if deal.get("turnover_ratio") is not None:
        tr = deal["turnover_ratio"]
        vol_ton = deal.get("collection_volume", 0) / 1e9
        text += f"📊 <b>Оборот/Цена:</b> <code>{tr:.1f}x</code> (объём: <code>{vol_ton:,.0f} TON</code>)\n"

    nft_url = make_telegram_nft_url(col_name, num)
    text += (
        f"\n🔗 <b>Ссылка на NFT:</b>\n"
        f"• 🎁 <a href=\"{nft_url}\">{nft_url}</a>\n"
    )

    kb_rows = []
    if with_buy_button and gift_id:
        kb_rows.append([InlineKeyboardButton(text=f"💳 Купить за {price_ton:.2f} TON", callback_data=f"buy:{gift_id}:{price}")])
    kb_rows.append([InlineKeyboardButton(text="🎁 Открыть NFT в Telegram", url=nft_url)])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    should_close = False
    if bot is None:
        bot = Bot(token=bot_token)
        should_close = True

    try:
        for admin_id in admin_ids:
            try:
                sent_msg = await bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
                # Сохраняем данные алерта для последующей пометки выкупленных и ручной покупки
                if scanner_state is not None and gift_id:
                    if gift_id not in scanner_state.sent_alerts:
                        scanner_state.sent_alerts[gift_id] = {
                            "sent_at": time.time(),
                            "messages": {},
                            "text": text,
                            "nft_url": nft_url,
                            "older_ids": set(older_ids) if older_ids else set(),
                            "deal": deal,
                            "price_nano": price,
                        }
                    scanner_state.sent_alerts[gift_id]["messages"][admin_id] = sent_msg.message_id
            except Exception as e:
                log.warning("Не удалось отправить алерт в TG %s: %s", admin_id, e)
    finally:
        if should_close:
            await bot.session.close()


async def send_autobuy_success_report(
    bot_token: str,
    admin_ids: set[int],
    deal: dict,
    buy_data: dict,
    in_vault: bool,
    buy_elapsed: float,
    scanner_state: Optional["ScannerState"] = None,
    bot: Optional[Bot] = None,
) -> None:
    """Отправляет отчёт об успешной автоматической покупке подарка."""
    if not bot_token or not admin_ids:
        return

    gift = deal.get("gift", {})
    col_name = gift.get("collectionName", "Unknown")
    mod_name = gift.get("modelName", "")
    num = gift.get("number") or gift.get("num") or "?"
    price = deal.get("price", 0)
    floor = deal.get("floor", 0)
    diff_ton = deal.get("diff_ton", 0.0)
    price_ton = price / 1e9
    floor_ton = floor / 1e9
    backdrop = gift.get("backdropName", "—")

    vault_str = "✅ Подтверждено в инвентаре" if in_vault else "⚠️ <b>Не найден в инвентаре!</b> Проверьте вручную."
    bal_str = (
        f"{scanner_state.primary_balance_nano / 1e9:.2f} TON"
        if scanner_state and scanner_state.primary_balance_nano is not None
        else "обновляется"
    )

    text = (
        f"🤖⚡ <b>УСПЕШНАЯ АВТО-ПОКУПКА!</b>\n\n"
        f"🎁 <b>{col_name} — {mod_name} #{num}</b>\n"
        f"💰 <b>Куплено за:</b> <code>{price_ton:.2f} TON</code>\n"
        f"🎯 <b>Флор:</b> <code>{floor_ton:.2f} TON</code>\n"
        f"💵 <b>Выгода:</b> <code>{diff_ton:.2f} TON</code>\n"
    )
    if deal.get("net_profit_ton") is not None:
        net_profit = deal["net_profit_ton"]
        margin_pct = deal.get("margin_pct", 0.0)
        text += f"📈 <b>Чистыми (после 2% + 0.1 TON):</b> <code>+{net_profit:.2f} TON</code> (ROI: <b>{margin_pct:+.1f}%</b>)\n"
    if deal.get("tier"):
        rp = deal.get("req_profit", 0.0)
        rm = deal.get("req_margin", 0.0)
        text += f"🪜 <b>Тир сделки:</b> {deal['tier']} (требовалось: ≥ <code>{rp:.1f} TON</code> и <code>{rm:.1f}%</code>)\n"
    text += (
        f"🎨 <b>Фон:</b> {backdrop}\n"
        f"📦 <b>Хранилище:</b> {vault_str}\n"
        f"💳 <b>Остаток баланса:</b> ~<code>{bal_str}</code>\n"
        f"⚡ <b>Скорость выкупа:</b> <code>{buy_elapsed:.2f} с</code>\n"
    )
    nft_url = make_telegram_nft_url(col_name, num)
    text += f"\n🔗 <a href=\"{nft_url}\">{nft_url}</a>"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Открыть NFT в Telegram", url=nft_url)]
        ]
    )

    should_close = False
    if bot is None:
        bot = Bot(token=bot_token)
        should_close = True

    try:
        for admin_id in admin_ids:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning("Не удалось отправить отчет об автопокупке в TG %s: %s", admin_id, e)
    finally:
        if should_close:
            await bot.session.close()


async def send_autobuy_failed_report(
    bot_token: str,
    admin_ids: set[int],
    deal: dict,
    reason: str,
    buy_elapsed: float,
    scanner_state: Optional["ScannerState"] = None,
    bot: Optional[Bot] = None,
) -> None:
    """Отправляет отчёт о сбое при авто-покупке."""
    if not bot_token or not admin_ids:
        return

    gift = deal.get("gift", {})
    col_name = gift.get("collectionName", "Unknown")
    mod_name = gift.get("modelName", "")
    num = gift.get("number") or gift.get("num") or "?"
    price_ton = deal.get("price", 0) / 1e9
    floor_ton = deal.get("floor", 0) / 1e9

    text = (
        f"❌ <b>СБОЙ АВТО-ПОКУПКИ</b>\n\n"
        f"🎁 <b>{col_name} — {mod_name} #{num}</b>\n"
        f"💰 <b>Цена:</b> <code>{price_ton:.2f} TON</code> | 🎯 <b>Флор:</b> <code>{floor_ton:.2f} TON</code>\n"
        f"⚠️ <b>Причина:</b> <code>{reason}</code>\n"
        f"⏱ <b>Время отклика:</b> <code>{buy_elapsed:.2f} с</code>\n"
    )
    nft_url = make_telegram_nft_url(col_name, num)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Открыть NFT в Telegram", url=nft_url)]
        ]
    )

    should_close = False
    if bot is None:
        bot = Bot(token=bot_token)
        should_close = True

    try:
        for admin_id in admin_ids:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning("Не удалось отправить отчет о сбое автопокупки в TG %s: %s", admin_id, e)
    finally:
        if should_close:
            await bot.session.close()


async def send_autobuy_skipped_notification(
    bot_token: str,
    admin_ids: set[int],
    deal: dict,
    reason: str,
    bot: Optional[Bot] = None,
) -> None:
    """Уведомляет о пропуске автопокупки (например, недостаточно баланса)."""
    if not bot_token or not admin_ids:
        return

    gift = deal.get("gift", {})
    col_name = gift.get("collectionName", "Unknown")
    mod_name = gift.get("modelName", "")
    num = gift.get("number") or gift.get("num") or "?"
    price_ton = deal.get("price", 0) / 1e9

    text = (
        f"⚠️ <b>АВТО-ПОКУПКА ПРОПУЩЕНА</b>\n\n"
        f"🎁 <b>{col_name} — {mod_name} #{num}</b>\n"
        f"💰 <b>Цена лота:</b> <code>{price_ton:.2f} TON</code>\n"
        f"ℹ️ <b>Причина:</b> {reason}\n"
    )

    should_close = False
    if bot is None:
        bot = Bot(token=bot_token)
        should_close = True

    try:
        for admin_id in admin_ids:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning("Не удалось отправить уведомление о пропуске автопокупки в TG %s: %s", admin_id, e)
    finally:
        if should_close:
            await bot.session.close()


async def send_token_expired_alert(
    bot_token: str,
    admin_ids: set[int],
    slot_label: str,
    token_str: str,
    bot: Optional[Bot] = None,
) -> None:
    """Уведомляет админов об аннулировании / просрочке токена (HTTP 401 Unauthorized)."""
    if not bot_token or not admin_ids:
        return

    masked = _mask_token(token_str)
    text = (
        f"🚨 <b>ВНИМАНИЕ: ТОКЕН ПРОСРОЧЕН (HTTP 401)!</b>\n\n"
        f"Слот: <code>{slot_label}</code>\n"
        f"Токен: <code>{masked}</code>\n\n"
        f"⚠️ Слот автоматически временно отключен от запросов.\n"
        f"Пожалуйста, обновите токен через браузерное расширение или меню управления токенами."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Управление токенами", callback_data="nav_tokens")],
            [InlineKeyboardButton(text="🧹 Удалить 401 токены", callback_data="tokens_cleanup_401")],
        ]
    )

    should_close = False
    if bot is None:
        bot = Bot(token=bot_token)
        should_close = True

    try:
        for admin_id in admin_ids:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning("Не удалось отправить 401 алерт в TG %s: %s", admin_id, e)
    finally:
        if should_close:
            await bot.session.close()


_last_error_alerts: dict[str, float] = {}

async def send_error_alert(
    bot_token: str,
    admin_ids: set[int],
    error_type: str,
    details: str,
    bot: Optional[Bot] = None,
    min_interval: float = 60.0,
) -> None:
    """Уведомляет админов об ошибках и сбоях сканера с дебаунсом (анти-спамом)."""
    if not bot_token or not admin_ids:
        return

    # Не шлём алерты о сбоях прокси и direct IP (статистика сбоев отображается в меню)
    err_low = (str(error_type) + " " + str(details)).lower()
    if any(k in err_low for k in ("прокси", "proxy", "direct ip", "резерв прокси")):
        return

    now = time.monotonic()
    last_sent = _last_error_alerts.get(error_type, 0.0)
    if now - last_sent < min_interval:
        return  # Дебаунс: не шлем чаще раза в минуту на один тип ошибки
    _last_error_alerts[error_type] = now

    cur_time = datetime.now().strftime("%H:%M:%S")
    text = (
        f"⚠️ <b>ВНИМАНИЕ: СБОЙ СИСТЕМЫ!</b>\n\n"
        f"⏰ <b>Время:</b> <code>{cur_time}</code>\n"
        f"🚨 <b>Тип:</b> <code>{html.escape(error_type)}</code>\n\n"
        f"📝 <b>Детали:</b>\n"
        f"<pre>{html.escape(details[:500])}</pre>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Посмотреть логи", callback_data="nav_logs")],
            [InlineKeyboardButton(text="🔄 Главное меню", callback_data="nav_main")],
        ]
    )

    should_close = False
    if bot is None:
        bot = Bot(token=bot_token)
        should_close = True

    try:
        for admin_id in admin_ids:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning("Не удалось отправить алерт об ошибке в TG %s: %s", admin_id, e)
    finally:
        if should_close:
            await bot.session.close()



