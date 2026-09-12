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
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
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
    scan_interval: float = 0.5
    scans_count: int = 0
    deals_count: int = 0
    start_time: float = field(default_factory=time.monotonic)
    black_floor_nano: Optional[int] = None
    collection_floors_count: int = 0           # Заменяет model_floors_count
    force_refresh_floors: bool = False          # Заменяет force_refresh_models
    last_deal: Optional[dict] = None
    primary_balance_nano: Optional[int] = None
    filter_by_balance: bool = False
    min_turnover_ratio: float = 0.0
    collection_volumes: dict[str, int] = field(default_factory=dict)
    notify_categories: dict[str, bool] = field(default_factory=lambda: {
        "BLACK":  os.getenv("NOTIFY_BLACK",  "true").lower() in ("1", "true", "yes"),
        "CHEAP":  os.getenv("NOTIFY_CHEAP",  "true").lower() in ("1", "true", "yes"),
        "NFT":    os.getenv("NOTIFY_NFT",    "true").lower() in ("1", "true", "yes"),
        "LOW_ID": os.getenv("NOTIFY_LOW_ID", "true").lower() in ("1", "true", "yes"),
    })
    vault: list[dict] = field(default_factory=list)
    # Хранилище отправленных алертов для пометки выкупленных
    # {gift_id: {"sent_at": float, "messages": {admin_id: msg_id}, "text": str, "nft_url": str, "older_ids": set}}
    sent_alerts: dict[str, dict[str, Any]] = field(default_factory=dict)
    sold_queue: list[str] = field(default_factory=list)  # gift_id выкупленных лотов
    buying_in_progress: set[str] = field(default_factory=set)  # gift_id покупаемых сейчас
    rate_adaptor: Optional[Any] = None  # RateAdaptor из scanner.py
    penalties_429: list[float] = field(default_factory=list)  # таймстампы 429 за последний час

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
    waiting_for_cheap_threshold = State()
    waiting_for_scan_interval = State()
    waiting_for_turnover_ratio = State()
    waiting_for_log_time = State()


# ─────────────────────────────────────────────
#  Вспомогательные функции
# ─────────────────────────────────────────────

def _mask_token(token: str) -> str:
    """Маскирует UUID токен: bf73c16d...ddbcf3a2."""
    token = token.strip()
    if len(token) > 16:
        return f"{token[:8]}…{token[-8:]}"
    return token


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


def _admin_filter(admin_ids: set[int]):
    def check(msg_or_cb: types.TelegramObject) -> bool:
        user = getattr(msg_or_cb, "from_user", None)
        return bool(user and user.id in admin_ids)
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
                InlineKeyboardButton(text=f"📦 Хранилище{vault_badge}", callback_data="nav_vault"),
                InlineKeyboardButton(text="🔔 Категории", callback_data="nav_categories"),
            ],
            [
                InlineKeyboardButton(text="🔑 Токены", callback_data="nav_tokens"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="nav_settings"),
            ],
            [
                InlineKeyboardButton(text="🌐 Прокси и Пинг", callback_data="nav_proxies"),
                InlineKeyboardButton(text="🔄 Обновить флоры", callback_data="refresh_floors"),
            ],
            [
                InlineKeyboardButton(text="📋 Просмотр логов", callback_data="nav_logs"),
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
    p429 = state.get_429_count_last_hour()
    p429_badge = f" (429: {p429}/ч)" if p429 > 0 else " (429: 0)"
    interval_btn_text = f"✏️ Интервал: {state.scan_interval:.2f}с{p429_badge}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🤖 Авто-покупка (AutoBuy): {autobuy_toggle_text}", callback_data="toggle_autobuy")],
            [InlineKeyboardButton(text=f"💰 Фильтр по балансу: {bal_toggle_text}", callback_data="toggle_balance_filter")],
            [InlineKeyboardButton(text="📊 Мин. оборот/цена (NFT)", callback_data="set_turnover_ratio")],
            [InlineKeyboardButton(text="👑 Сменить основной аккаунт", callback_data="nav_select_primary")],
            [InlineKeyboardButton(text="✏️ Порог выгоды (MIN_TON_DIFF)", callback_data="set_min_diff")],
            [InlineKeyboardButton(text="✏️ Порог дешёвых (CHEAP_THRESHOLD)", callback_data="set_cheap")],
            [InlineKeyboardButton(text=interval_btn_text, callback_data="set_interval")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav_main")],
        ]
    )




def proxies_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Перепроверить пинг", callback_data="proxies_reping")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="nav_main")],
        ]
    )


def categories_keyboard(state: ScannerState) -> InlineKeyboardMarkup:
    cat_names = {
        "BLACK":  "🖤 Чёрный фон",
        "CHEAP":  "💸 Сверхдешёвые",
        "NFT":    "🎯 Ниже флора коллекции",
        "LOW_ID": "🏷️ Редкий ID (<100)",
    }
    rows = []
    for cat_key, cat_label in cat_names.items():
        is_on = state.notify_categories.get(cat_key, True)
        status = "🟢 ВКЛ" if is_on else "🔴 ВЫКЛ (в хранилище)"
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


# ─────────────────────────────────────────────
#  Формирование текста экранов
# ─────────────────────────────────────────────

def format_main_text(state: ScannerState) -> str:
    status_icon = "⏸ <b>НА ПАУЗЕ</b>" if state.is_paused else "🟢 <b>СКАНИРУЕТ</b>"
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

    return (
        f"🤖 <b>MRKT Scanner Manager</b>\n\n"
        f"Статус: {status_icon}\n"
        f"⏱ Аптайм: <code>{state.uptime_str()}</code>\n"
        f"📊 Сканов: <code>{state.scans_count:,}</code> | 🎯 Сделок: <code>{state.deals_count}</code>\n"
        f"📦 В хранилище: <b>{vault_count}</b> сделок\n\n"
        f"⚙️ <b>Параметры:</b>\n"
        f"• 🤖 AutoBuy: <b>{'🟢 ВКЛ' if state.auto_buy else '🔴 ВЫКЛ'}</b>\n"
        f"• Порог выгоды: <code>{state.min_ton_diff:.2f} TON</code>\n"
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
        f"• Токенов: <code>{active_tokens}</code>\n"
        f"• Прокси: <code>{active_proxies}</code>"
    )


def format_categories_text(state: ScannerState) -> str:
    vault_len = len(state.vault) if state.vault else 0
    return (
        f"🔔 <b>Уведомления по категориям</b>\n\n"
        f"Нажмите на категорию, чтобы включить или выключить моментальные алерты в чат:\n\n"
        f"• <b>ВКЛ 🟢</b> — алерты сразу приходят в этот чат.\n"
        f"• <b>ВЫКЛ 🔴</b> — алерты не спамят в чат, а бережно сохраняются в 📦 <b>Хранилище</b> (сейчас там: <b>{vault_len}</b> шт.). "
        f"Вы можете выгрузить их все одной кнопкой в любой удобный момент.\n"
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
    bal_str = f"{state.primary_balance_nano / 1e9:.2f} TON" if state.primary_balance_nano is not None else "не проверен"
    filter_bal_str = "🟢 ВКЛ" if state.filter_by_balance else "🔴 ВЫКЛ"
    turnover_str = f"≥ {state.min_turnover_ratio:.1f}x" if state.min_turnover_ratio > 0 else "выключен (0.0)"
    primary_tok = state.pool.primary_token if state.pool else None
    primary_str = _mask_token(primary_tok) if primary_tok else "не задан"
    adaptor = getattr(state, "rate_adaptor", None)
    auto_mode = adaptor.is_auto if adaptor is not None else True
    interval_mode = "авто" if auto_mode else "ручной"
    p429 = state.get_429_count_last_hour()
    p429_badge = f" [штрафов 429: <b>{p429}</b>/ч]" if p429 > 0 else " [штрафов 429: 0/ч]"

    return (
        f"⚙️ <b>Настройки сканера</b>\n\n"
        f"0. <b>Авто-покупка (AutoBuy):</b> {autobuy_str}\n"
        f"   <i>(Моментальный выкуп подходящих подарков с основного аккаунта без задержек)</i>\n\n"
        f"1. <b>Фильтр по балансу:</b> {filter_bal_str}\n"
        f"   <i>(Показывать только подарки, на которые хватает баланса основного аккаунта)</i>\n\n"
        f"2. <b>Мин. оборот/цена для NFT:</b> <code>{turnover_str}</code>\n"
        f"   <i>(Отсекает мёртвый груз: оборот/цена ≥ X; кроме чёрного фона и подарков &lt; {state.cheap_price_threshold:.1f} TON)</i>\n\n"
        f"3. <b>Основной аккаунт:</b> <code>{primary_str}</code>\n"
        f"   <i>(Текущий баланс: <code>{bal_str}</code>; используется для покупок)</i>\n\n"
        f"4. <b>Порог выгоды (MIN_TON_DIFF):</b> <code>{state.min_ton_diff:.2f} TON</code>\n"
        f"   <i>(Подарок покупается, если он дешевле флора минимум на это значение)</i>\n\n"
        f"5. <b>Порог дешёвых (CHEAP_THRESHOLD):</b> <code>{state.cheap_price_threshold:.2f} TON</code>\n"
        f"   <i>(Любой подарок с ценой ниже этого порога считается выгодным)</i>\n\n"
        f"6. <b>⚡ Интервал сканирования ({interval_mode}):</b> <code>{state.scan_interval:.2f} с</code>{p429_badge}\n"
        f"   <i>(Пауза между запросами; при установке вручную авто-адаптация отключается)</i>"
    )



def format_tokens_text(tokens: list[str], verified_info: Optional[dict] = None) -> str:
    if not tokens:
        return "🔑 <b>Управление токенами</b>\n\n⚠️ В пуле нет активных токенов!"

    lines = [f"🔑 <b>Управление токенами</b> (Всего: <code>{len(tokens)}</code>):\n"]
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
        text = format_main_text(scanner_state)
        await cb.message.edit_text(text, reply_markup=main_keyboard(scanner_state), parse_mode="HTML")
        await cb.answer()

    # ── Управление сканером (Пауза/Старт) ─────────────────────────────────
    @dp.callback_query(F.data == "scanner_pause")
    async def cb_scanner_pause(cb: CallbackQuery):
        scanner_state.is_paused = True
        await cb.answer("⏸ Сканер поставлен на паузу")
        text = format_main_text(scanner_state)
        await cb.message.edit_text(text, reply_markup=main_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "scanner_resume")
    async def cb_scanner_resume(cb: CallbackQuery):
        scanner_state.is_paused = False
        await cb.answer("▶️ Сканер возобновил работу")
        text = format_main_text(scanner_state)
        await cb.message.edit_text(text, reply_markup=main_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "refresh_floors")
    async def cb_refresh_floors(cb: CallbackQuery):
        scanner_state.force_refresh_floors = True
        await cb.answer("🔄 Запущено обновление флоров коллекций...")

    # ── Раздел: Уведомления по категориям ────────────────────────────────
    @dp.callback_query(F.data == "nav_categories")
    async def cb_nav_categories(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        text = format_categories_text(scanner_state)
        await cb.message.edit_text(text, reply_markup=categories_keyboard(scanner_state), parse_mode="HTML")
        await cb.answer()

    @dp.callback_query(F.data.startswith("toggle_cat_"))
    async def cb_toggle_cat(cb: CallbackQuery):
        cat = cb.data.replace("toggle_cat_", "")
        current = scanner_state.notify_categories.get(cat, True)
        scanner_state.notify_categories[cat] = not current
        save_settings(scanner_state)
        status = "ВКЛ 🟢" if not current else "ВЫКЛ 🔴"

        await cb.answer(f"{cat}: {status}")
        text = format_categories_text(scanner_state)
        await cb.message.edit_text(text, reply_markup=categories_keyboard(scanner_state), parse_mode="HTML")

    # ── Раздел: Хранилище (Vault) ─────────────────────────────────────────
    @dp.callback_query(F.data == "nav_vault")
    async def cb_nav_vault(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        text = format_vault_text(scanner_state)
        await cb.message.edit_text(text, reply_markup=vault_keyboard(scanner_state), parse_mode="HTML")
        await cb.answer()

    @dp.callback_query(F.data == "vault_send_all")
    async def cb_vault_send_all(cb: CallbackQuery):
        vault_deals = list(scanner_state.vault)
        if not vault_deals:
            await cb.answer("Хранилище пусто!", show_alert=True)
            return

        scanner_state.vault.clear()
        await cb.answer(f"Отправка {len(vault_deals)} сделок...")
        await cb.message.edit_text(
            f"📤 <i>Отправка {len(vault_deals)} сделок из Хранилища в чат...</i>",
            parse_mode="HTML",
        )

        for d in vault_deals:
            await send_deal_notification(bot_token, admin_ids, d, bot=bot)
            await asyncio.sleep(0.08)

        text = format_vault_text(scanner_state)
        await cb.message.edit_text(text, reply_markup=vault_keyboard(scanner_state), parse_mode="HTML")
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
        await cb.message.edit_text(text, reply_markup=vault_keyboard(scanner_state), parse_mode="HTML")

    # ── Раздел: Токены ───────────────────────────────────────────────────
    @dp.callback_query(F.data == "nav_tokens")
    async def cb_nav_tokens(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        tokens = scanner_state.pool.get_tokens() if scanner_state.pool else []
        text = format_tokens_text(tokens)
        await cb.message.edit_text(text, reply_markup=tokens_keyboard(), parse_mode="HTML")
        await cb.answer()

    @dp.callback_query(F.data == "tokens_verify_all")
    async def cb_tokens_verify_all(cb: CallbackQuery):
        tokens = scanner_state.pool.get_tokens() if scanner_state.pool else []
        if not tokens:
            await cb.answer("Токенов нет", show_alert=True)
            return

        await cb.answer("🔍 Проверяем токены через MRKT API...")
        await cb.message.edit_text("⏳ <i>Проверка токенов через MRKT API...</i>", parse_mode="HTML")

        verified: dict[str, tuple[bool, str]] = {}
        for tok in tokens:
            ok, msg, _ = await verify_token_async(tok)
            verified[tok] = (ok, msg)

        text = format_tokens_text(tokens, verified_info=verified)
        await cb.message.edit_text(text, reply_markup=tokens_keyboard(), parse_mode="HTML")

    @dp.callback_query(F.data == "tokens_add")
    async def cb_tokens_add(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_add_token)
        text = (
            "➕ <b>Добавление токена</b>\n\n"
            "Отправьте токен (UUID) в ответном сообщении.\n"
            "<i>(Можно скопировать токен целиком, curl-запрос или строку авторизации — бот сам найдёт UUID).</i>"
        )
        await cb.message.edit_text(text, reply_markup=back_to_menu_keyboard("nav_tokens"), parse_mode="HTML")
        await cb.answer()

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
        text = (
            "📝 <b>Полная замена токенов</b>\n\n"
            "Отправьте список новых токенов (по одному на строку, либо общий текст с токенами).\n"
            "⚠️ <i>Все старые токены будут заменены новыми!</i>"
        )
        await cb.message.edit_text(text, reply_markup=back_to_menu_keyboard("nav_tokens"), parse_mode="HTML")
        await cb.answer()

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
        await cb.message.edit_text("⏳ <i>Идёт проверка токенов для очистки...</i>", parse_mode="HTML")

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
            await cb.message.edit_text(
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

        await cb.message.edit_text(text, reply_markup=tokens_keyboard(), parse_mode="HTML")

    # ── Раздел: Настройки ────────────────────────────────────────────────
    @dp.callback_query(F.data == "nav_settings")
    async def cb_nav_settings(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        text = format_settings_text(scanner_state)
        await cb.message.edit_text(text, reply_markup=settings_keyboard(scanner_state), parse_mode="HTML")
        await cb.answer()

    @dp.callback_query(F.data == "toggle_autobuy")
    async def cb_toggle_autobuy(cb: CallbackQuery):
        scanner_state.auto_buy = not scanner_state.auto_buy
        save_settings(scanner_state)
        status_text = "включена 🟢" if scanner_state.auto_buy else "выключена 🔴"
        await cb.answer(f"Авто-покупка {status_text}")
        text = format_settings_text(scanner_state)
        await cb.message.edit_text(text, reply_markup=settings_keyboard(scanner_state), parse_mode="HTML")

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
        await cb.message.edit_text(text, reply_markup=settings_keyboard(scanner_state), parse_mode="HTML")


    @dp.callback_query(F.data == "set_turnover_ratio")
    async def cb_set_turnover_ratio(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_turnover_ratio)
        cur = f"{scanner_state.min_turnover_ratio:.1f}x" if scanner_state.min_turnover_ratio > 0 else "выключен (0.0)"
        await cb.message.edit_text(
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
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await cb.answer()

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
        await cb.message.edit_text(text, reply_markup=settings_keyboard(scanner_state), parse_mode="HTML")

    @dp.callback_query(F.data == "set_min_diff")
    async def cb_set_min_diff(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_min_ton_diff)
        await cb.message.edit_text(
            f"✏️ Текущий порог выгоды: <code>{scanner_state.min_ton_diff:.2f} TON</code>\n\n"
            f"Введите новое значение в TON (например <code>2.0</code> или <code>3.5</code>):",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )
        await cb.answer()

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

    @dp.callback_query(F.data == "set_cheap")
    async def cb_set_cheap(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_cheap_threshold)
        await cb.message.edit_text(
            f"✏️ Текущий порог дешёвых подарков: <code>{scanner_state.cheap_price_threshold:.2f} TON</code>\n\n"
            f"Введите новое значение в TON (например <code>3.0</code>):",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )
        await cb.answer()

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

    @dp.callback_query(F.data == "set_interval")
    async def cb_set_interval(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_scan_interval)
        adaptor = getattr(scanner_state, "rate_adaptor", None)
        mode_str = "авто" if (adaptor and adaptor.is_auto) else "ручной"
        p429 = scanner_state.get_429_count_last_hour()
        p429_text = f"⚠️ Штрафов 429 за последний час: <b>{p429}</b>\n\n" if p429 > 0 else "Штрафов 429 за последний час: <code>0</code>\n\n"
        await cb.message.edit_text(
            f"✏️ Текущий интервал: <code>{scanner_state.scan_interval:.2f} с</code> ({mode_str})\n"
            f"{p429_text}"
            f"Введите новый интервал в секундах (например <code>0.5</code>).\n"
            f"<i>После ручной установки авто-адаптация отключается.</i>\n"
            f"Введите <code>auto</code> для возврата в авто-режим:",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )
        await cb.answer()

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
    @dp.callback_query(F.data == "nav_logs")
    async def cb_nav_logs(cb: CallbackQuery, state: FSMContext):
        await state.set_state(BotStates.waiting_for_log_time)
        text = (
            "📋 <b>Просмотр логов</b>\n\n"
            "Отправьте временную метку для поиска строк в диапазоне ±10 секунд.\n\n"
            "Форматы:\n"
            "• <code>20:15:33</code> — время в формате ЧЧ:ММ:СС\n"
            "• <code>20:15</code> — время ЧЧ:ММ (секунды = 0)\n"
            "• <code>now</code> — последние 60 секунд логов\n"
        )
        await cb.message.edit_text(text, reply_markup=back_to_menu_keyboard("nav_main"), parse_mode="HTML")
        await cb.answer()

    @dp.message(BotStates.waiting_for_log_time)
    async def msg_log_time(msg: Message, state: FSMContext):
        raw = (msg.text or "").strip()
        log_dir = Path(os.getenv("LOG_DIR", "logs"))
        lines = read_log_window(raw, log_dir)
        await state.clear()
        if not lines:
            await msg.answer(
                f"📋 По запросу <code>{raw}</code> ничего не найдено в логах.\n"
                "<i>Убедитесь, что время указано в формате ЧЧ:ММ:СС или ЧЧ:ММ.</i>",
                parse_mode="HTML",
                reply_markup=back_to_menu_keyboard("nav_main"),
            )
            return

        full_text = f"📋 <b>Логи ±10с от {raw}:</b>\n\n" + "\n".join(lines)
        # Разбиваем на части если слишком длинно
        MAX_LEN = 4000
        chunks = [full_text[i:i+MAX_LEN] for i in range(0, len(full_text), MAX_LEN)]
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                await msg.answer(
                    f"<code>{chunk}</code>",
                    parse_mode="HTML",
                    reply_markup=back_to_menu_keyboard("nav_main"),
                )
            else:
                await msg.answer(f"<code>{chunk}</code>", parse_mode="HTML")
            if len(chunks) > 1:
                await asyncio.sleep(0.1)

    # ── Раздел: Прокси и Пинг ────────────────────────────────────────────
    @dp.callback_query(F.data == "nav_proxies")
    async def cb_nav_proxies(cb: CallbackQuery):
        proxies = scanner_state.pool.get_proxies() if scanner_state.pool else []
        if not proxies:
            text = "🌐 <b>Прокси</b>\n\nПрокси не используются (прямое подключение direct)."
        else:
            lines = [f"🌐 <b>Активные прокси в пуле</b> (Всего: <code>{len(proxies)}</code>):\n"]
            for i, p in enumerate(proxies, 1):
                lines.append(f"{i}. <b>[{p.cfg.name}]</b> → <code>127.0.0.1:{p.cfg.local_port}</code>")
            lines.append("\n💡 <i>Нажмите «Перепроверить пинг» для замера задержки к api.tgmrkt.io.</i>")
            text = "\n".join(lines)

        await cb.message.edit_text(text, reply_markup=proxies_keyboard(), parse_mode="HTML")
        await cb.answer()

    @dp.callback_query(F.data == "proxies_reping")
    async def cb_proxies_reping(cb: CallbackQuery):
        proxies = scanner_state.pool.get_proxies() if scanner_state.pool else []
        if not proxies:
            await cb.answer("Прокси нет", show_alert=True)
            return


        await cb.answer("⚡ Замеряем пинг прокси...")
        await cb.message.edit_text("⏳ <i>Замер пинга всех прокси к api.tgmrkt.io...</i>", parse_mode="HTML")

        tasks = [ping_proxy_async(p, timeout=2.5) for p in proxies]
        results = await asyncio.gather(*tasks)

        lines = [f"🌐 <b>Результаты замера пинга</b> ({len(proxies)} прокси):\n"]
        for p, (ok, lat, err) in zip(proxies, results):
            status = f"⚡ <code>{lat:.0f} мс</code> (OK)" if ok else f"❌ {err}"
            lines.append(f"• <b>[{p.cfg.name}]</b>: {status}")

        await cb.message.edit_text("\n".join(lines), reply_markup=proxies_keyboard(), parse_mode="HTML")

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
#  Чтение логов по временному диапазону
# ─────────────────────────────────────────────

def read_log_window(timestamp_str: str, log_dir: Path, window_sec: int = 10) -> list[str]:
    """
    Читает строки из scanner.log в окне [ts - window_sec, ts + window_sec].
    timestamp_str: 'ЧЧ:ММ:СС', 'ЧЧ:ММ', или 'now'.
    """
    now = datetime.now()
    raw = timestamp_str.strip().lower()

    if raw == "now":
        target_dt = now
        window_sec = 30
    else:
        try:
            parts = raw.split(":")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            s = int(parts[2]) if len(parts) > 2 else 0
            target_dt = now.replace(hour=h, minute=m, second=s, microsecond=0)
        except (ValueError, IndexError):
            return []

    start_dt = target_dt - timedelta(seconds=window_sec)
    end_dt   = target_dt + timedelta(seconds=window_sec)

    # Ищем текущий лог-файл и вчерашний (на случай перехода через полночь)
    date_str_today = now.strftime("%Y-%m-%d")
    date_str_prev  = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    candidates = [
        log_dir / "scanner.log",
        log_dir / f"scanner.log.{date_str_today}",
        log_dir / f"scanner.log.{date_str_prev}",
    ]

    matched: list[str] = []
    # Формат строки: 2026-09-07 20:15:33 [INFO    ] ...
    _ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

    for log_path in candidates:
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _ts_re.match(line)
                    if not m:
                        continue
                    try:
                        line_dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if start_dt <= line_dt <= end_dt:
                        matched.append(line.rstrip())
        except OSError:
            pass

    return matched


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
        f"🎨 <b>Фон:</b> {backdrop}\n"
    )

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

    vault_str = "Подтверждено в Хранилище ✅" if in_vault else "В Хранилище (по чеку покупки) ✅"
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

