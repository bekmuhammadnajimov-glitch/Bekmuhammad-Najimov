import html
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from . import db
from .config import ADMIN_IDS, BOT_TOKEN

log = logging.getLogger("nexdonate.notify")

bot: Bot | None = (
    Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if BOT_TOKEN else None
)

MSG = {
    "payment_confirmed": {
        "uz": "💳 <b>To'lov tasdiqlandi</b>\nBuyurtma <code>{code}</code> bajarilmoqda — {title} yuborilmoqda.",
        "ru": "💳 <b>Оплата подтверждена</b>\nЗаказ <code>{code}</code> выполняется — отправляем {title}.",
    },
    "payment_rejected": {
        "uz": "⚠️ <b>Chek qabul qilinmadi</b>\nBuyurtma <code>{code}</code> uchun to'lov tasdiqlanmadi. To'g'ri chekni qayta yuklang yoki qo'llab-quvvatlashga yozing.",
        "ru": "⚠️ <b>Чек не принят</b>\nОплата заказа <code>{code}</code> не подтверждена. Загрузите корректный чек ещё раз или напишите в поддержку.",
    },
    "completed": {
        "uz": "✅ <b>Buyurtma yetkazildi!</b>\n{title} → <b>{nick}</b> (<code>{pid}</code>)\nBuyurtma <code>{code}</code>\n\nO'yindan zavqlaning! 🎮",
        "ru": "✅ <b>Заказ доставлен!</b>\n{title} → <b>{nick}</b> (<code>{pid}</code>)\nЗаказ <code>{code}</code>\n\nПриятной игры! 🎮",
    },
    "failed": {
        "uz": "❗️ <b>Buyurtma bajarilmadi</b>\nBuyurtma <code>{code}</code> bo'yicha texnik muammo yuz berdi. Administrator buni ko'rib chiqadi — iltimos, qo'llab-quvvatlashga yozing.",
        "ru": "❗️ <b>Заказ не выполнен</b>\nПо заказу <code>{code}</code> возникла техническая проблема. Администратор разберётся — пожалуйста, напишите в поддержку.",
    },
}


async def _lang(tg_id: int) -> str:
    row = await db.fetchone("SELECT lang FROM users WHERE tg_id = ?", (tg_id,))
    return (row or {}).get("lang") or "uz"


async def send_user(order: dict, key: str) -> None:
    if not bot:
        return
    lang = await _lang(order["tg_id"])
    text = MSG[key][lang].format(
        code=db.code(order["id"]),
        title=html.escape(order.get("offer_name") or ""),
        nick=html.escape(order.get("nick") or "—"),
        pid=html.escape(order["player_id"]),
    )
    try:
        await bot.send_message(order["tg_id"], text)
    except Exception as e:  # user blocked the bot, etc.
        log.warning("send_user failed for %s: %s", order["tg_id"], e)


def _admin_order_text(order: dict, title: str) -> str:
    return (
        f"{title}\n"
        f"<b>{db.code(order['id'])}</b>\n"
        f"Mahsulot: <b>{html.escape(order.get('offer_name') or '')}</b>\n"
        f"ID: <code>{html.escape(order['player_id'])}</code> · {html.escape(order.get('nick') or '—')}\n"
        f"Summa: <b>{order['amount']:,}</b> {'₽' if order.get('currency') == 'RUB' else 'so' + chr(39) + 'm'}".replace(",", " ")
        + f"\nMijoz: <code>{order['tg_id']}</code>"
    )


async def alert_admins(order: dict, title: str, retry_button: bool = False, extra: str = "") -> None:
    if not bot:
        return
    kb = None
    if retry_button:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔁 Qayta urinish", callback_data=f"retry:{order['id']}")]]
        )
    text = _admin_order_text(order, title) + (f"\n\n<code>{html.escape(extra)[:600]}</code>" if extra else "")
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text, reply_markup=kb)
        except Exception as e:
            log.warning("alert_admins failed for %s: %s", admin, e)


async def admin_receipt(order: dict, path: str) -> None:
    if not bot:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"pay:ok:{order['id']}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"pay:no:{order['id']}"),
            ]
        ]
    )
    caption = _admin_order_text(order, "🧾 <b>Yangi to'lov cheki</b>")
    for admin in ADMIN_IDS:
        try:
            try:
                await bot.send_photo(admin, FSInputFile(path), caption=caption, reply_markup=kb)
            except Exception:
                await bot.send_document(admin, FSInputFile(path), caption=caption, reply_markup=kb)
        except Exception as e:
            log.warning("admin_receipt failed for %s: %s", admin, e)
