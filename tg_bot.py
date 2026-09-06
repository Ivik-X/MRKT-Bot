"""
tg_bot.py — Управление MRKT-сканером через Telegram-бота (aiogram 3).

Функционал:
  - Просмотр и редактирование токенов (добавление, замена, удаление 401).
  - Проверка баланса и статуса каждого токена через MRKT API.
  - Изменение порогов выгоды и интервала сканирования на лету.
  - Статистика, пауза/запуск сканера, просмотр пинга прокси.
  - Отправка мгновенных алертов о найденных подарках.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

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

from account_pool import AccountPool, save_tokens, verify_token_async
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
    min_ton_diff: float = 2.5
    cheap_price_threshold: float = 3.0
    scan_interval: float = 0.5
    scans_count: int = 0
    deals_count: int = 0
    start_time: float = field(default_factory=time.monotonic)
    black_floor_nano: Optional[int] = None
    model_floors_count: int = 0
    force_refresh_models: bool = False
    last_deal: Optional[dict] = None

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


# ─────────────────────────────────────────────
#  Вспомогательные функции
# ─────────────────────────────────────────────

def _mask_token(token: str) -> str:
    """Маскирует UUID токен: bf73c16d...ddbcf3a2."""
    token = token.strip()
    if len(token) > 16:
        return f"{token[:8]}…{token[-8:]}"
    return token


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
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [status_btn, InlineKeyboardButton(text="🔄 Обновить статус", callback_data="nav_main")],
            [
                InlineKeyboardButton(text="🔑 Токены", callback_data="nav_tokens"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="nav_settings"),
            ],
            [
                InlineKeyboardButton(text="🌐 Прокси и Пинг", callback_data="nav_proxies"),
                InlineKeyboardButton(text="🔄 Обновить флоры", callback_data="refresh_floors"),
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


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Порог выгоды (MIN_TON_DIFF)", callback_data="set_min_diff")],
            [InlineKeyboardButton(text="✏️ Порог дешёвых (CHEAP_THRESHOLD)", callback_data="set_cheap")],
            [InlineKeyboardButton(text="✏️ Интервал сканов (SCAN_INTERVAL)", callback_data="set_interval")],
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

    return (
        f"🤖 <b>MRKT Scanner Manager</b>\n\n"
        f"Статус: {status_icon}\n"
        f"⏱ Аптайм: <code>{state.uptime_str()}</code>\n"
        f"📊 Сканов: <code>{state.scans_count:,}</code> | 🎯 Сделок: <code>{state.deals_count}</code>\n\n"
        f"⚙️ <b>Параметры:</b>\n"
        f"• Порог выгоды: <code>{state.min_ton_diff:.2f} TON</code>\n"
        f"• Дешёвые подарки: &lt; <code>{state.cheap_price_threshold:.2f} TON</code>\n"
        f"• Интервал сканов: <code>{state.scan_interval:.2f} с</code>\n\n"
        f"📦 <b>Рыночные данные:</b>\n"
        f"• Флор чёрного фона: <code>{bf_str}</code>\n"
        f"• Моделей в кэше: <code>{state.model_floors_count}</code>\n\n"
        f"🔌 <b>Ресурсы:</b>\n"
        f"• Токенов: <code>{active_tokens}</code>\n"
        f"• Прокси: <code>{active_proxies}</code>"
    )


def format_tokens_text(tokens: list[str], verified_info: Optional[dict] = None) -> str:
    if not tokens:
        return "🔑 <b>Управление токенами</b>\n\n⚠️ В пуле нет активных токенов!"

    lines = [f"🔑 <b>Управление токенами</b> (Всего: <code>{len(tokens)}</code>):\n"]
    for i, tok in enumerate(tokens, 1):
        masked = _mask_token(tok)
        status = ""
        if verified_info and tok in verified_info:
            ok, msg = verified_info[tok]
            status = f" — {'✅' if ok else '❌'} <i>{msg}</i>"
        lines.append(f"{i}. <code>{masked}</code>{status}")

    lines.append("\n💡 <i>Нажмите «Проверить балансы», чтобы проверить валидность токенов и баланс TON.</i>")
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  Создание и запуск Telegram Bot Runner
# ─────────────────────────────────────────────

async def run_telegram_bot(bot_token: str, admin_ids: set[int], state: ScannerState) -> None:
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
    async def cmd_start(msg: Message, state_ctx: FSMContext):
        await state_ctx.clear()
        text = format_main_text(state)
        await msg.answer(text, reply_markup=main_keyboard(state), parse_mode="HTML")

    @dp.callback_query(F.data == "nav_main")
    async def cb_nav_main(cb: CallbackQuery, state_ctx: FSMContext):
        await state_ctx.clear()
        text = format_main_text(state)
        await cb.message.edit_text(text, reply_markup=main_keyboard(state), parse_mode="HTML")
        await cb.answer()

    # ── Управление сканером (Пауза/Старт) ─────────────────────────────────
    @dp.callback_query(F.data == "scanner_pause")
    async def cb_scanner_pause(cb: CallbackQuery):
        state.is_paused = True
        await cb.answer("⏸ Сканер поставлен на паузу")
        text = format_main_text(state)
        await cb.message.edit_text(text, reply_markup=main_keyboard(state), parse_mode="HTML")

    @dp.callback_query(F.data == "scanner_resume")
    async def cb_scanner_resume(cb: CallbackQuery):
        state.is_paused = False
        await cb.answer("▶️ Сканер возобновил работу")
        text = format_main_text(state)
        await cb.message.edit_text(text, reply_markup=main_keyboard(state), parse_mode="HTML")

    @dp.callback_query(F.data == "refresh_floors")
    async def cb_refresh_floors(cb: CallbackQuery):
        state.force_refresh_models = True
        await cb.answer("🔄 Запущено обновление флоров моделей...")

    # ── Раздел: Токены ───────────────────────────────────────────────────
    @dp.callback_query(F.data == "nav_tokens")
    async def cb_nav_tokens(cb: CallbackQuery, state_ctx: FSMContext):
        await state_ctx.clear()
        tokens = state.pool.get_tokens() if state.pool else []
        text = format_tokens_text(tokens)
        await cb.message.edit_text(text, reply_markup=tokens_keyboard(), parse_mode="HTML")
        await cb.answer()

    @dp.callback_query(F.data == "tokens_verify_all")
    async def cb_tokens_verify_all(cb: CallbackQuery):
        tokens = state.pool.get_tokens() if state.pool else []
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
    async def cb_tokens_add(cb: CallbackQuery, state_ctx: FSMContext):
        await state_ctx.set_state(BotStates.waiting_for_add_token)
        text = (
            "➕ <b>Добавление токена</b>\n\n"
            "Отправьте токен (UUID) в ответном сообщении.\n"
            "<i>(Можно скопировать токен целиком, curl-запрос или строку авторизации — бот сам найдёт UUID).</i>"
        )
        await cb.message.edit_text(text, reply_markup=back_to_menu_keyboard("nav_tokens"), parse_mode="HTML")
        await cb.answer()

    @dp.message(BotStates.waiting_for_add_token)
    async def msg_add_token(msg: Message, state_ctx: FSMContext):
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

        current = state.pool.get_tokens() if state.pool else []
        if new_tok in current:
            await msg.answer("⚠️ Этот токен уже есть в списке!", reply_markup=back_to_menu_keyboard("nav_tokens"))
            await state_ctx.clear()
            return

        current.append(new_tok)
        save_tokens(current)
        if state.pool:
            state.pool.reload_tokens(current)

        ver_str = f"✅ Валиден (Баланс: {status_msg})" if ok else f"⚠️ Добавлен, но статус: {status_msg}"
        await msg.answer(
            f"🎉 <b>Токен успешно добавлен!</b>\n\n"
            f"Токен: <code>{_mask_token(new_tok)}</code>\n"
            f"Статус: {ver_str}\n"
            f"Всего токенов в пуле: <code>{len(current)}</code>",
            reply_markup=back_to_menu_keyboard("nav_tokens"),
            parse_mode="HTML",
        )
        await state_ctx.clear()

    @dp.callback_query(F.data == "tokens_replace")
    async def cb_tokens_replace(cb: CallbackQuery, state_ctx: FSMContext):
        await state_ctx.set_state(BotStates.waiting_for_replace_tokens)
        text = (
            "📝 <b>Полная замена токенов</b>\n\n"
            "Отправьте список новых токенов (по одному на строку, либо общий текст с токенами).\n"
            "⚠️ <i>Все старые токены будут заменены новыми!</i>"
        )
        await cb.message.edit_text(text, reply_markup=back_to_menu_keyboard("nav_tokens"), parse_mode="HTML")
        await cb.answer()

    @dp.message(BotStates.waiting_for_replace_tokens)
    async def msg_replace_tokens(msg: Message, state_ctx: FSMContext):
        raw = msg.text or ""
        found = _extract_uuid_tokens(raw)
        if not found:
            await msg.answer("❌ Не найдено ни одного UUID токена!", reply_markup=back_to_menu_keyboard("nav_tokens"))
            return

        unique_tokens = list(dict.fromkeys(found))
        save_tokens(unique_tokens)
        if state.pool:
            state.pool.reload_tokens(unique_tokens)

        await msg.answer(
            f"✅ <b>Список токенов обновлён!</b>\nЗагружено уникальных токенов: <code>{len(unique_tokens)}</code>",
            reply_markup=back_to_menu_keyboard("nav_tokens"),
            parse_mode="HTML",
        )
        await state_ctx.clear()

    @dp.callback_query(F.data == "tokens_cleanup_401")
    async def cb_tokens_cleanup_401(cb: CallbackQuery):
        tokens = state.pool.get_tokens() if state.pool else []
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
            if state.pool:
                state.pool.reload_tokens(valid_tokens)
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
    async def cb_nav_settings(cb: CallbackQuery, state_ctx: FSMContext):
        await state_ctx.clear()
        text = (
            f"⚙️ <b>Настройки сканера</b>\n\n"
            f"1. <b>Порог выгоды (MIN_TON_DIFF):</b> <code>{state.min_ton_diff:.2f} TON</code>\n"
            f"   <i>(Подарок покупается, если он дешевле флора минимум на это значение)</i>\n\n"
            f"2. <b>Порог дешёвых (CHEAP_THRESHOLD):</b> <code>{state.cheap_price_threshold:.2f} TON</code>\n"
            f"   <i>(Любой подарок с ценой ниже этого порога считается выгодным)</i>\n\n"
            f"3. <b>Интервал сканирования:</b> <code>{state.scan_interval:.2f} с</code>\n"
            f"   <i>(Пауза между запросами к витрине)</i>"
        )
        await cb.message.edit_text(text, reply_markup=settings_keyboard(), parse_mode="HTML")
        await cb.answer()

    @dp.callback_query(F.data == "set_min_diff")
    async def cb_set_min_diff(cb: CallbackQuery, state_ctx: FSMContext):
        await state_ctx.set_state(BotStates.waiting_for_min_ton_diff)
        await cb.message.edit_text(
            f"✏️ Текущий порог выгоды: <code>{state.min_ton_diff:.2f} TON</code>\n\n"
            f"Введите новое значение в TON (например <code>2.0</code> или <code>3.5</code>):",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )
        await cb.answer()

    @dp.message(BotStates.waiting_for_min_ton_diff)
    async def msg_set_min_diff(msg: Message, state_ctx: FSMContext):
        try:
            val = float(msg.text.replace(",", ".").strip())
            if val <= 0:
                raise ValueError
            state.min_ton_diff = val
            await msg.answer(f"✅ Порог выгоды изменён на <code>{val:.2f} TON</code>", reply_markup=back_to_menu_keyboard("nav_settings"), parse_mode="HTML")
            await state_ctx.clear()
        except ValueError:
            await msg.answer("❌ Пожалуйста, введите корректное положительное число (например 2.5):")

    @dp.callback_query(F.data == "set_cheap")
    async def cb_set_cheap(cb: CallbackQuery, state_ctx: FSMContext):
        await state_ctx.set_state(BotStates.waiting_for_cheap_threshold)
        await cb.message.edit_text(
            f"✏️ Текущий порог дешёвых подарков: <code>{state.cheap_price_threshold:.2f} TON</code>\n\n"
            f"Введите новое значение в TON (например <code>3.0</code>):",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )
        await cb.answer()

    @dp.message(BotStates.waiting_for_cheap_threshold)
    async def msg_set_cheap(msg: Message, state_ctx: FSMContext):
        try:
            val = float(msg.text.replace(",", ".").strip())
            if val <= 0:
                raise ValueError
            state.cheap_price_threshold = val
            await msg.answer(f"✅ Порог дешёвых подарков изменён на <code>{val:.2f} TON</code>", reply_markup=back_to_menu_keyboard("nav_settings"), parse_mode="HTML")
            await state_ctx.clear()
        except ValueError:
            await msg.answer("❌ Пожалуйста, введите корректное положительное число (например 3.0):")

    @dp.callback_query(F.data == "set_interval")
    async def cb_set_interval(cb: CallbackQuery, state_ctx: FSMContext):
        await state_ctx.set_state(BotStates.waiting_for_scan_interval)
        await cb.message.edit_text(
            f"✏️ Текущий интервал сканирования: <code>{state.scan_interval:.2f} с</code>\n\n"
            f"Введите новый интервал в секундах (например <code>0.5</code>):",
            reply_markup=back_to_menu_keyboard("nav_settings"),
            parse_mode="HTML",
        )
        await cb.answer()

    @dp.message(BotStates.waiting_for_scan_interval)
    async def msg_set_interval(msg: Message, state_ctx: FSMContext):
        try:
            val = float(msg.text.replace(",", ".").strip())
            if val < 0.1:
                raise ValueError
            state.scan_interval = val
            await msg.answer(f"✅ Интервал сканирования изменён на <code>{val:.2f} с</code>", reply_markup=back_to_menu_keyboard("nav_settings"), parse_mode="HTML")
            await state_ctx.clear()
        except ValueError:
            await msg.answer("❌ Пожалуйста, введите положительное число не менее 0.1 (например 0.5):")

    # ── Раздел: Прокси и Пинг ────────────────────────────────────────────
    @dp.callback_query(F.data == "nav_proxies")
    async def cb_nav_proxies(cb: CallbackQuery):
        proxies = state.pool.get_proxies() if state.pool else []
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
        proxies = state.pool.get_proxies() if state.pool else []
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

    # ── Запуск Polling ───────────────────────────────────────────────────
    log.info("Telegram бот запущен для администраторов: %s", admin_ids)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()


# ─────────────────────────────────────────────
#  Отправка Deal Alerts в Telegram
# ─────────────────────────────────────────────

async def send_deal_notification(
    bot_token: str,
    admin_ids: set[int],
    deal: dict,
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
        "BLACK": "🖤 <b>ВЫГОДНЫЙ ЧЁРНЫЙ ФОН</b>",
        "CHEAP": "💸 <b>СВЕРХДЕШЁВЫЙ ПОДАРОК</b>",
        "MODEL": "🎯 <b>НИЖЕ ФЛОРА МОДЕЛИ</b>",
    }
    header = tags.get(deal_type, "🔥 <b>ВЫГОДНАЯ СДЕЛКА!</b>")

    col_name = gift.get("collectionName", "Unknown")
    mod_name = gift.get("modelName", "")
    num = gift.get("number") or gift.get("num") or "?"
    backdrop = gift.get("backdropName", "—")
    gift_id = gift.get("giftId") or gift.get("id") or ""

    text = (
        f"{header}\n\n"
        f"🎁 <b>{col_name} — {mod_name} #{num}</b>\n"
        f"💰 <b>Цена:</b> <code>{price_ton:.2f} TON</code>\n"
        f"🎯 <b>Флор:</b> <code>{floor_ton:.2f} TON</code>\n"
        f"💵 <b>Выгода:</b> <code>{diff_ton:.2f} TON</code> (<b>{pct:.1f}%</b> скидка)\n"
        f"🎨 <b>Фон:</b> {backdrop}\n"
    )

    kb = None
    if gift_id:
        url = f"https://cdn.tgmrkt.io/gift/{gift_id}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Открыть на маркете", url=url)]
            ]
        )

    bot = Bot(token=bot_token)
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
                log.warning("Не удалось отправить алерт в TG %s: %s", admin_id, e)
    finally:
        await bot.session.close()
