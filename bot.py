# -*- coding: utf-8 -*-
"""
Hestle External Telegram bot
Install: pip install -r requirements.txt
Run:     python bot.py
"""
import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from datetime import datetime
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LinkPreviewOptions, MessageEntity, FSInputFile
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = "9054532984:AAFSjnOPWqniiRPjtZED0el7mIXc5h-I1EM"
OWNER_ID = 7608889836
DB_PATH = "hestle_bot.db"

# Ваши Telegram Premium Custom Emoji ID.
# Они вставляются в тексты сообщений как custom_emoji entities.
EMOJI = {
    "user": "5906581476639513176",
    "link": "5906839307821259375",
    "hourglass": "5906598824012420908",
    "wave": "5906611897892870255",
    "question": "5906952733612579003",
    "trash": "5904631707516016581",
    "eye": "5906853781861046999",
    "megaphone": "5906797358875681525",
    "money": "5904576890848419790",
    "settings": "5906513053515519760",
    "down": "5906851011607140828",
}

# Символы-заполнители для ваших Premium Custom Emoji.
# Telegram отрисует их как указанные пользовательские эмодзи через tg-emoji.
PREMIUM_SYMBOLS = {
    "👤": EMOJI["user"],
    "🔗": EMOJI["link"],
    "⏳": EMOJI["hourglass"],
    "👋🏻": EMOJI["wave"],
    "❓": EMOJI["question"],
    "🗑️": EMOJI["trash"],
    "👁️": EMOJI["eye"],
    "📢": EMOJI["megaphone"],
    "💰": EMOJI["money"],
    "⚙️": EMOJI["settings"],
    "👇🏻": EMOJI["down"],
}

def premium_text(text):
    """Replace supplied emoji glyphs with Telegram custom-emoji HTML tags."""
    if not isinstance(text, str):
        return text
    # Normalize the eye glyph to the exact fallback used by its Premium Emoji.
    text = text.replace("👁", "👁️")
    # Longer emoji sequences first (skin tone / variation selector).
    for symbol in sorted(PREMIUM_SYMBOLS, key=len, reverse=True):
        text = text.replace(symbol, f'<tg-emoji emoji-id="{PREMIUM_SYMBOLS[symbol]}">{symbol}</tg-emoji>')
    return text

START_IMAGE = Path(__file__).resolve().with_name("Start.jpg")
# =====================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hestle")
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            joined_at TEXT, invited_by INTEGER, referrals INTEGER DEFAULT 0,
            tokens INTEGER DEFAULT 0, rewarded_milestones INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS owner_messages(
            owner_message_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            prompt TEXT, created_at TEXT
        )""")


def custom_text(text, key, offset):
    """Return one Telegram custom-emoji entity at the given UTF-16 offset."""
    return text, [MessageEntity(type="custom_emoji", offset=offset,
                                length=2, custom_emoji_id=EMOJI[key])]


async def send_rich(chat_id, text, key, marker="◆", **kwargs):
    # marker is exactly one BMP character, so its UTF-16 offset is 0.
    try:
        return await send_bot_message(
            chat_id, text,
            entities=[MessageEntity(type="custom_emoji", offset=0, length=1,
                                    custom_emoji_id=EMOJI[key])],
            **kwargs
        )
    except Exception:
        # Graceful fallback if Telegram rejects an unavailable custom emoji.
        return await send_bot_message(chat_id, text, **kwargs)


def kb(rows):
    buttons = []
    for row in rows:
        built_row = []
        for item in row:
            t, d = item[0], item[1]
            style = item[2] if len(item) >= 3 else None
            icon_id = None
            for symbol in sorted(PREMIUM_SYMBOLS, key=len, reverse=True):
                if t.startswith(symbol):
                    icon_id = PREMIUM_SYMBOLS[symbol]
                    break
            # Telegram draws icon_custom_emoji_id separately to the left of the label.
            # Remove the matching fallback glyph from the label to avoid a duplicate
            # (Premium icon + ordinary emoji) on the same button.
            label = t
            if icon_id:
                for symbol in sorted(PREMIUM_SYMBOLS, key=len, reverse=True):
                    if label.startswith(symbol) and PREMIUM_SYMBOLS[symbol] == icon_id:
                        label = label[len(symbol):].lstrip()
                        break
            built_row.append(InlineKeyboardButton(
                text=label, callback_data=d, style=style, icon_custom_emoji_id=icon_id
            ))
        buttons.append(built_row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def answer(target, text, **kwargs):
    """Send a message with the supplied Premium Emoji IDs applied."""
    return await target.answer(premium_text(text), **kwargs)


async def send_bot_message(chat_id, text, **kwargs):
    return await bot.send_message(chat_id, premium_text(text), **kwargs)


def main_menu():
    return kb([
        [("💰 Каталог", "catalog", "success")],
        [("🔗 Реферал", "referral", "danger")],
        [("👤 Обращение к владельцу", "owner_contact", "danger")],
        [("👁 Стата бота", "stats", "danger")],
    ])


def back_menu(target="home", color_label="👋🏻 Назад", style="danger"):
    return kb([[(color_label, target, style)]])


class Flow(StatesGroup):
    prompt = State()
    material = State()
    description = State()
    contact = State()
    broadcast = State()
    add_tokens = State()
    remove_tokens = State()


def get_user(uid):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def save_user(user, referrer=None):
    uid = user.id
    with db() as c:
        exists = c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
        if exists:
            c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?",
                      (user.username or "", user.first_name or "", uid))
            return False
        if referrer == uid:
            referrer = None
        c.execute("""INSERT INTO users(user_id,username,first_name,joined_at,invited_by)
                     VALUES(?,?,?,?,?)""",
                  (uid, user.username or "", user.first_name or "",
                   datetime.utcnow().isoformat(), referrer))
        if referrer:
            c.execute("UPDATE users SET referrals=referrals+1 WHERE user_id=?", (referrer,))
            row = c.execute("SELECT referrals,rewarded_milestones FROM users WHERE user_id=?",
                            (referrer,)).fetchone()
            if row:
                # One purchase token for every completed group of 5 valid referrals.
                earned = row["referrals"] // 5
                if earned > row["rewarded_milestones"]:
                    new_tokens = earned - row["rewarded_milestones"]
                    c.execute("""UPDATE users SET tokens=tokens+?,
                                 rewarded_milestones=? WHERE user_id=?""",
                              (new_tokens, earned, referrer))
                    return ("ref_reward", referrer, new_tokens, row["referrals"])
        return True


def owner_only(uid):
    return uid == OWNER_ID


async def safe_edit(call, text, reply_markup=None):
    """Show a readable section screen after a button press.

    The start screen is a photo message. Editing only its caption can make the
    interface feel like the user is still looking at the same picture/buttons.
    On photo messages, remove the old keyboard and send a fresh text screen with
    the section description and its own navigation buttons.
    """
    try:
        if call.message.photo:
            await call.message.edit_reply_markup(reply_markup=None)
            await answer(call.message, text, reply_markup=reply_markup)
        else:
            await call.message.edit_text(premium_text(text), reply_markup=reply_markup)
    except Exception:
        # Fallback also creates a separate descriptive screen if Telegram refuses
        # to edit the original message.
        await answer(call.message, text, reply_markup=reply_markup)


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    ref = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            ref = int(parts[1][4:])
        except ValueError:
            ref = None

    result = save_user(message.from_user, ref)
    if isinstance(result, tuple) and result[0] == "ref_reward":
        _, inviter, amount, total = result
        try:
            await send_bot_message(
                inviter,
                f"🎉 У вас уже {total} приглашений!\n"
                f"🎟 Начислено токенов: <b>{amount}</b>.\n"
                "Их можно использовать для бесплатного заказа."
            )
        except Exception:
            pass

    name = escape(message.from_user.first_name or "друг")
    text = (
        f"◆ Здравствуй, <b>{name}</b>!\n\n"
        "Добро пожаловать в нашего бота\n"
        "<b>Hestle External</b>\n\n"
        "👇🏻 Выбери пункт ниже:"
    )
    if START_IMAGE.is_file():
        await message.answer_photo(
            photo=FSInputFile(START_IMAGE),
            caption=premium_text(text),
            reply_markup=main_menu()
        )
    else:
        await answer(message, text, reply_markup=main_menu())
        log.warning("Start.jpg не найден рядом с bot.py: старт отправлен без фотографии")


@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, "◆ <b>Hestle External</b>\n\nВыбери нужный раздел 👇🏻",
                    main_menu())
    await call.answer()


@dp.callback_query(F.data == "catalog")
async def catalog(call: CallbackQuery):
    text = (
        "💰 <b>Каталог Hestle External</b>\n\n"
        "Все товары всё ещё в продаже!\n"
        "Дёшево и быстро.\n\n"
        "Нажми «Заказать» — бот объяснит, что делать."
    )
    await safe_edit(call, text, kb([
        [("💰 Заказать", "order", "success")],
        [("👋🏻 Назад", "home", "danger")]
    ]))
    await call.answer()


@dp.callback_query(F.data == "order")
async def order(call: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.prompt)
    await safe_edit(
        call,
        "📝 <b>Оформление заказа</b>\n\n"
        "Вы можете заказать Telegram-бота, сайт или приложение/APK.\n\n"
        "Напишите одним сообщением, что именно нужно. Добавьте как можно больше "
        "подробностей: функции, дизайн, примеры и требования.\n\n"
        "После отправки бот передаст ваш запрос владельцу.",
        back_menu("catalog", style="success")
    )
    await call.answer()


@dp.message(Flow.prompt)
async def receive_prompt(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 5:
        await answer(message, "Пожалуйста, отправь текстовый промпт подробнее (минимум 5 символов).")
        return
    prompt = message.text.strip()
    with db() as c:
        c.execute("INSERT INTO orders(user_id,prompt,created_at) VALUES(?,?,?)",
                  (message.from_user.id, prompt, datetime.utcnow().isoformat()))
    u = message.from_user
    owner_msg = await send_bot_message(
        OWNER_ID,
        "📥 <b>Новый запрос на заказ</b>\n"
        f"От: {escape(u.full_name)} (@{escape(u.username or 'нет')})\n"
        f"ID: <code>{u.id}</code>\n\n"
        f"<b>Промпт:</b>\n{escape(prompt)}"
    )
    with db() as c:
        c.execute("INSERT OR REPLACE INTO owner_messages(owner_message_id,user_id) VALUES(?,?)",
                  (owner_msg.message_id, u.id))
    await state.set_state(Flow.material)
    await answer(message, 
        "✅ <b>Запрос отправлен владельцу!</b>\n\n"
        "Теперь отправьте готовый материал по вашему промпту: файл, фото, видео "
        "или текст. К материалу можно будет добавить описание отдельной кнопкой.",
        reply_markup=kb([[("Отправить материал", "material_hint")],
                         [("👋🏻 В меню", "home")]])
    )


@dp.callback_query(F.data == "material_hint")
async def material_hint(call: CallbackQuery):
    await call.answer("Просто отправьте файл, фото, видео или текст следующим сообщением.", show_alert=True)


@dp.message(Flow.material)
async def receive_material(message: Message, state: FSMContext):
    u = message.from_user
    try:
        copied = await bot.copy_message(OWNER_ID, message.chat.id, message.message_id)
        with db() as c:
            c.execute("INSERT OR REPLACE INTO owner_messages(owner_message_id,user_id) VALUES(?,?)",
                      (copied.message_id, u.id))
        await send_bot_message(
            OWNER_ID,
            f"📎 <b>Материал от пользователя</b>\n"
            f"ID: <code>{u.id}</code> | @{escape(u.username or 'нет')}"
        )
    except Exception as e:
        log.exception("Не удалось передать материал владельцу")
        await answer(message, "Не получилось отправить материал. Попробуйте ещё раз.")
        return
    await state.set_state(Flow.description)
    await answer(message, 
        "✅ Материал передан владельцу!\n\n"
        "Хотите добавить описание к материалу? Нажмите кнопку ниже или завершите отправку.",
        reply_markup=kb([[("📝 Добавить описание", "add_description")],
                         [("✅ Завершить", "finish_material")]])
    )


@dp.callback_query(F.data == "add_description")
async def add_description(call: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.description)
    await answer(call.message, "Напишите описание следующим сообщением:")
    await call.answer()


@dp.message(Flow.description)
async def receive_description(message: Message, state: FSMContext):
    u = message.from_user
    try:
        copied = await bot.copy_message(OWNER_ID, message.chat.id, message.message_id)
        with db() as c:
            c.execute("INSERT OR REPLACE INTO owner_messages(owner_message_id,user_id) VALUES(?,?)",
                      (copied.message_id, u.id))
        await send_bot_message(OWNER_ID,
            f"📝 <b>Описание к материалу</b>\nПользователь: <code>{u.id}</code>")
        await answer(message, "✅ Описание отправлено владельцу. Спасибо!")
    except Exception:
        await answer(message, "Не удалось отправить описание. Попробуйте ещё раз.")
        return
    await state.clear()
    await answer(message, "Главное меню:", reply_markup=main_menu())


@dp.callback_query(F.data == "finish_material")
async def finish_material(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await answer(call.message, "✅ Отправка завершена. Спасибо! Владелец свяжется с вами при необходимости.",
                              reply_markup=main_menu())
    await call.answer()


@dp.callback_query(F.data == "referral")
async def referral(call: CallbackQuery):
    uid = call.from_user.id
    row = get_user(uid)
    if not row:
        save_user(call.from_user)
        row = get_user(uid)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    total = row["referrals"]
    progress = total % 5
    text = (
        "🔗 <b>Реферальная программа</b>\n\n"
        "Пригласи 5 новых пользователей по своей ссылке и получи "
        "<b>1 токен на бесплатную покупку</b>!\n\n"
        f"👥 Приглашено: <b>{total}</b>\n"
        f"🎟 Токенов: <b>{row['tokens']}</b>\n"
        f"📊 Прогресс до следующего токена: <b>{progress}/5</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        "Учитываются только первые запуски новых пользователей. "
        "Повторные переходы и приглашение самого себя не засчитываются."
    )
    await safe_edit(call, text, kb([
        [("🔒 Отправить друзьям", "share_ref")],
        [("👋🏻 Назад", "home")]
    ]))
    await call.answer()


@dp.callback_query(F.data == "share_ref")
async def share_ref(call: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"
    share_text = (
        "⭐ Нашёл Hestle External — здесь можно заказать Telegram-бота, сайт "
        "или приложение. Присоединяйся!\n\n" + link
    )
    url = "https://t.me/share/url?url=" + link + "&text=" + (
        "⭐ Нашёл Hestle External — присоединяйся!"
    ).replace(" ", "%20")
    await answer(call.message, 
        f"Отправь друзьям свою ссылку:\n<code>{link}</code>\n\n"
        "За каждые 5 приглашённых новых пользователей — 1 токен.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📨 Поделиться", url=url)],
            [InlineKeyboardButton(text="👋🏻 Назад", callback_data="referral")]
        ])
    )
    await call.answer()


@dp.callback_query(F.data == "stats")
async def stats(call: CallbackQuery):
    with db() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        refs = c.execute("SELECT COALESCE(SUM(referrals),0) n FROM users").fetchone()["n"]
        orders = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    await safe_edit(call,
        "👁 <b>Статистика Hestle External</b>\n\n"
        f"👤 Пользователей: <b>{users}</b>\n"
        f"🔗 Приглашений: <b>{refs}</b>\n"
        f"📝 Заявок: <b>{orders}</b>",
        back_menu("home"))
    await call.answer()


@dp.callback_query(F.data == "owner_contact")
async def owner_contact(call: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.contact)
    await safe_edit(call,
        "👤 <b>Обращение к владельцу</b>\n\n"
        "Напишите сообщение одним текстом. Оно будет отправлено владельцу бота.",
        back_menu("home"))
    await call.answer()


@dp.message(Flow.contact)
async def send_owner_contact(message: Message, state: FSMContext):
    u = message.from_user
    if not message.text:
        await answer(message, "Отправьте, пожалуйста, текстовое сообщение.")
        return
    sent = await send_bot_message(
        OWNER_ID,
        "📨 <b>Обращение к владельцу</b>\n"
        f"От: {escape(u.full_name)} (@{escape(u.username or 'нет')})\n"
        f"ID: <code>{u.id}</code>\n\n{escape(message.text)}"
    )
    with db() as c:
        c.execute("INSERT OR REPLACE INTO owner_messages(owner_message_id,user_id) VALUES(?,?)",
                  (sent.message_id, u.id))
    await state.clear()
    await answer(message, "✅ Сообщение отправлено владельцу.", reply_markup=main_menu())


# Ответ владельца на сообщение бота будет доставлен исходному пользователю.
@dp.message(F.from_user.id == OWNER_ID, F.reply_to_message)
async def owner_reply(message: Message):
    with db() as c:
        row = c.execute("SELECT user_id FROM owner_messages WHERE owner_message_id=?",
                        (message.reply_to_message.message_id,)).fetchone()
    if not row:
        return
    try:
        await bot.copy_message(row["user_id"], message.chat.id, message.message_id)
        await message.reply("✅ Ответ отправлен пользователю.")
    except Exception:
        await message.reply("Не удалось доставить ответ пользователю.")


def admin_kb():
    return kb([
        [("📊 Статистика", "admin_stats"), ("👥 Пользователи", "admin_users")],
        [("📢 Рассылка", "admin_broadcast")],
        [("🎟 Выдать токены", "admin_add_tokens"), ("🗑 Снять токены", "admin_remove_tokens")],
        [("📋 Последние заявки", "admin_orders")],
        [("👋🏻 В меню", "home")]
    ])


@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not owner_only(message.from_user.id):
        return
    await answer(message, 
        "⚙️ <b>Панель владельца — Hestle External</b>\n\n"
        "Выберите действие. Доступ разрешён только владельцу.",
        reply_markup=admin_kb()
    )


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if not owner_only(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    with db() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        refs = c.execute("SELECT COALESCE(SUM(referrals),0) n FROM users").fetchone()["n"]
        tokens = c.execute("SELECT COALESCE(SUM(tokens),0) n FROM users").fetchone()["n"]
        orders = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    await safe_edit(call,
        "📊 <b>Полная статистика</b>\n\n"
        f"👤 Пользователей: <b>{users}</b>\n"
        f"🔗 Всего приглашений: <b>{refs}</b>\n"
        f"🎟 Неиспользованных токенов: <b>{tokens}</b>\n"
        f"📝 Всего заявок: <b>{orders}</b>",
        admin_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_users")
async def admin_users(call: CallbackQuery):
    if not owner_only(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    with db() as c:
        rows = c.execute("SELECT user_id,username,first_name,referrals,tokens FROM users "
                         "ORDER BY joined_at DESC LIMIT 15").fetchall()
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    lines = [f"Всего пользователей: <b>{total}</b>\n\nПоследние 15:"]
    for r in rows:
        lines.append(
            f"• <code>{r['user_id']}</code> — {escape(r['first_name'] or '')} "
            f"@{escape(r['username'] or 'нет')} | реф.: {r['referrals']} | токены: {r['tokens']}"
        )
    await safe_edit(call, "\n".join(lines), admin_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_orders")
async def admin_orders(call: CallbackQuery):
    if not owner_only(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    with db() as c:
        rows = c.execute("SELECT id,user_id,prompt,created_at FROM orders ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        text = "Заявок пока нет."
    else:
        parts = ["📋 <b>Последние 10 заявок</b>\n"]
        for r in rows:
            excerpt = (r["prompt"] or "").replace("<", "&lt;").replace(">", "&gt;")
            if len(excerpt) > 350:
                excerpt = excerpt[:350] + "…"
            parts.append(f"#{r['id']} | <code>{r['user_id']}</code>\n{excerpt}")
        text = "\n\n".join(parts)
    await safe_edit(call, text, admin_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(call: CallbackQuery, state: FSMContext):
    if not owner_only(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    await state.set_state(Flow.broadcast)
    await answer(call.message, "📢 Отправьте текст рассылки одним сообщением:")
    await call.answer()


@dp.message(Flow.broadcast, F.from_user.id == OWNER_ID)
async def do_broadcast(message: Message, state: FSMContext):
    if not message.text:
        await answer(message, "Для рассылки отправьте текст.")
        return
    with db() as c:
        ids = [r["user_id"] for r in c.execute("SELECT user_id FROM users").fetchall()]
    ok = 0
    failed = 0
    for uid in ids:
        try:
            await send_bot_message(uid, message.text)
            ok += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
    await state.clear()
    await answer(message, f"📢 Рассылка завершена.\nУспешно: {ok}\nНе доставлено: {failed}",
                         reply_markup=admin_kb())


@dp.callback_query(F.data.in_({"admin_add_tokens", "admin_remove_tokens"}))
async def token_action(call: CallbackQuery, state: FSMContext):
    if not owner_only(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    if call.data == "admin_add_tokens":
        await state.set_state(Flow.add_tokens)
        await answer(call.message, "Введите ID пользователя и количество токенов через пробел.\nПример: <code>123456789 2</code>")
    else:
        await state.set_state(Flow.remove_tokens)
        await answer(call.message, "Введите ID пользователя и количество токенов для снятия через пробел.\nПример: <code>123456789 1</code>")
    await call.answer()


async def change_tokens(message: Message, state: FSMContext, delta):
    try:
        uid_s, amount_s = message.text.split()
        uid, amount = int(uid_s), int(amount_s)
        if amount < 1 or amount > 100000:
            raise ValueError()
    except Exception:
        await answer(message, "Неверный формат. Пример: <code>123456789 2</code>")
        return
    with db() as c:
        row = c.execute("SELECT tokens FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            await answer(message, "Пользователь не найден в базе бота.")
            return
        new_value = max(0, row["tokens"] + delta * amount)
        c.execute("UPDATE users SET tokens=? WHERE user_id=?", (new_value, uid))
    await state.clear()
    await answer(message, f"✅ Готово. Токенов у <code>{uid}</code>: <b>{new_value}</b>",
                         reply_markup=admin_kb())


@dp.message(Flow.add_tokens, F.from_user.id == OWNER_ID)
async def add_tokens(message: Message, state: FSMContext):
    await change_tokens(message, state, 1)


@dp.message(Flow.remove_tokens, F.from_user.id == OWNER_ID)
async def remove_tokens(message: Message, state: FSMContext):
    await change_tokens(message, state, -1)


@dp.callback_query()
async def unknown_callback(call: CallbackQuery):
    await call.answer()


async def main():
    if BOT_TOKEN == "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER" or ":" not in BOT_TOKEN:
        raise SystemExit("Открой bot.py и вставь токен BotFather в переменную BOT_TOKEN в самом начале.")
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Hestle External запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
