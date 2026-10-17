import html
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from . import db
from .config import ADMIN_IDS, BOT_TOKEN, BOT_USERNAME
from .games import REGISTRY

log = logging.getLogger("nexdonate.notify")
bot_username: str | None = BOT_USERNAME or None  # filled at startup; builds the t.me/<bot>?start=pay_<order> links

bot: Bot | None = (
    Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if BOT_TOKEN else None
)

def recipient(order: dict) -> str:
    """Who the order is for, as HTML: @username for Telegram Stars/Premium, "Nick (ID)" or just the ID for games."""
    pid = html.escape(order["player_id"])
    g = REGISTRY.get(order.get("game"))
    if g and g.recipient == "username":
        return f"@{pid}"
    nick = order.get("nick")
    return f"{html.escape(nick)} (<code>{pid}</code>)" if nick else f"<code>{pid}</code>"


def recipient_line(order: dict, lang: str) -> str:
    """The "who is it for" line of a message, labelled for the game: Telegram, Account or Player ID."""
    g = REGISTRY.get(order.get("game"))
    if g and g.recipient == "username":
        return f"Telegram: <b>{recipient(order)}</b>"
    label = {"uz": "Akkaunt", "ru": "Аккаунт"}[lang] if order.get("nick") else "Player ID"
    return f"{label}: <b>{recipient(order)}</b>"


MSG = {
    "payment_confirmed": {
        "uz": "💳 <b>To'lov tasdiqlandi</b>\nBuyurtma <code>{code}</code> bajarilmoqda — {title} yuborilmoqda.",
        "ru": "💳 <b>Оплата подтверждена</b>\nЗаказ <code>{code}</code> выполняется — отправляем {title}.",
    },
    "payment_rejected": {
        "uz": "⚠️ <b>Chek qabul qilinmadi</b>\nBuyurtma <code>{code}</code> uchun to'lov tasdiqlanmadi. To'g'ri chek skrinshotini shu chatga qayta yuboring yoki admin bilan bog'laning.",
        "ru": "⚠️ <b>Чек не принят</b>\nОплата заказа <code>{code}</code> не подтверждена. Отправьте корректный скриншот чека сюда ещё раз или напишите админу.",
    },
    "completed": {
        "uz": "✅ <b>Buyurtma yetkazildi!</b>\n{title} → <b>{who}</b>\nBuyurtma <code>{code}</code>\n\nXaridingiz uchun rahmat! 🙌",
        "ru": "✅ <b>Заказ доставлен!</b>\n{title} → <b>{who}</b>\nЗаказ <code>{code}</code>\n\nСпасибо за покупку! 🙌",
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
        who=recipient(order),
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
        f"{recipient_line(order, 'uz')}\n"
        f"Summa: <b>{order['amount']:,}</b> {'₽' if order.get('currency') == 'RUB' else 'so' + chr(39) + 'm'}".replace(",", " ")
        + f"\nMijoz: <code>{order['tg_id']}</code>"
    )


async def log_event(kind: str, text: str, order_id: str | None = None, tg_id: int | None = None) -> None:
    try:
        await db.execute(
            "INSERT INTO events (ts, kind, order_id, tg_id, text) VALUES (?,?,?,?,?)", (db.now(), kind, order_id, tg_id, text)
        )
    except Exception as e:  # never let logging break a payment
        log.warning("could not store event: %s", e)


async def describe_user(tg_id: int) -> str:
    u = await db.fetchone("SELECT first_name, last_name, username FROM users WHERE tg_id = ?", (tg_id,))
    if not u:
        return f"<code>{tg_id}</code>"
    name = html.escape(" ".join(p for p in (u["first_name"], u["last_name"]) if p) or "—")
    nick = f" @{html.escape(u['username'])}" if u["username"] else ""
    return f'<a href="tg://user?id={tg_id}">{name}</a>{nick} · <code>{tg_id}</code>'


async def _send_admins(text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    if not bot:
        return
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text, reply_markup=kb, disable_web_page_preview=True)
        except Exception as e:
            log.warning("could not message admin %s: %s", admin, e)


async def admin_event(kind: str, text: str, order: dict | None = None, tg_id: int | None = None) -> None:
    """Everything that happens is stored in the events log and pushed to the admins, so the owner sees it live."""
    order_id = order["id"] if order else None
    tg_id = tg_id or (order["tg_id"] if order else None)
    await log_event(kind, text, order_id, tg_id)
    who = f"\n👤 {await describe_user(tg_id)}" if tg_id else ""
    await _send_admins(f"{text}{who}")


async def alert_admins(order: dict, title: str, retry_button: bool = False, extra: str = "") -> None:
    kb = None
    if retry_button:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔁 Qayta urinish", callback_data=f"retry:{order['id']}")]]
        )
    text = _admin_order_text(order, title) + (f"\n\n<code>{html.escape(extra)[:600]}</code>" if extra else "")
    await log_event("alert", f"{title} {db.code(order['id'])} {extra[:200]}", order["id"], order["tg_id"])
    await _send_admins(text, kb)


async def admin_receipt(order: dict, path: str, note: str = "", buttons: bool = True) -> None:
    """The customer's receipt screenshot, with the full order details, straight to the admin (approve / reject right there)."""
    if not bot:
        return
    kb = None
    if buttons:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"pay:ok:{order['id']}"),
                    InlineKeyboardButton(text="❌ Rad etish", callback_data=f"pay:no:{order['id']}"),
                ]
            ]
        )
    taken = ""
    if order.get("receipt_at") and order.get("pay_opened_at"):
        taken = f"\n⏱ Karta ko'rsatilgandan chek yuborilgunicha: <b>{order['receipt_at'] - order['pay_opened_at']} soniya</b>"
    caption = (
        _admin_order_text(order, "🧾 <b>Yangi to'lov cheki</b>")
        + f"\n👤 {await describe_user(order['tg_id'])}{taken}"
        + (f"\n\n{note}" if note else "")
    )
    await log_event("receipt", f"Chek keldi {db.code(order['id'])} {note[:160]}", order["id"], order["tg_id"])
    for admin in ADMIN_IDS:
        try:
            try:
                await bot.send_photo(admin, FSInputFile(path), caption=caption[:1024], reply_markup=kb)
            except Exception:
                await bot.send_document(admin, FSInputFile(path), caption=caption[:1024], reply_markup=kb)
        except Exception as e:
            log.warning("admin_receipt failed for %s: %s", admin, e)