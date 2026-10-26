import html
import logging
import time
from urllib.parse import quote

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from . import db
from .config import ADMIN_IDS, BOT_TOKEN, BOT_USERNAME, SUPPORT_USERNAME
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
    if g and g.delivery == "code":
        return "kod"
    if g and g.recipient == "username":
        return f"@{pid}"
    nick = order.get("nick")
    return f"{html.escape(nick)} (<code>{pid}</code>)" if nick else f"<code>{pid}</code>"


def _redeem_line(order: dict, lang: str) -> str:
    g = REGISTRY.get(order.get("game"))
    if not (g and g.redeem_url):
        return ""
    return {"uz": f"Kodni shu yerda faollashtiring: {g.redeem_url}\n", "ru": f"Активируйте код здесь: {g.redeem_url}\n"}[lang]


def recipient_line(order: dict, lang: str) -> str:
    """The "who is it for" line of a message, labelled for the game: Telegram, Account or Player ID."""
    g = REGISTRY.get(order.get("game"))
    if g and g.delivery == "code":
        return {"uz": "Yetkazish: <b>kod shu chatga yuboriladi</b>", "ru": "Доставка: <b>код придёт в этот чат</b>"}[lang]
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
    "completed_code": {
        "uz": "✅ <b>Buyurtma yetkazildi!</b>\n{title}\n\n🔑 Kodingiz:\n{codes}\n{redeem}\nBuyurtma <code>{code}</code>\n\nKodni hech kimga bermang. Xaridingiz uchun rahmat! 🙌",
        "ru": "✅ <b>Заказ доставлен!</b>\n{title}\n\n🔑 Ваш код:\n{codes}\n{redeem}\nЗаказ <code>{code}</code>\n\nНикому не передавайте код. Спасибо за покупку! 🙌",
    },
    "wallet_short": {
        "uz": "⚠️ <b>Hozir buyurtmani bajarib bo'lmaydi</b>\nHozir bot <b>{title}</b> tashlab bera olmaydi. «Admin» tugmasini bosing: xabar o'zi yoziladi, shunchaki yuboring. Admin buyurtmangizni qo'lda bajaradi 👇",
        "ru": "⚠️ <b>Сейчас заказ выполнить нельзя</b>\nСейчас бот не может отправить <b>{title}</b>. Нажмите «Админ»: сообщение напишется само, просто отправьте его. Админ выполнит заказ вручную 👇",
    },
    "failed": {
        "uz": "❗️ <b>Buyurtma bajarilmadi</b>\nBuyurtma <code>{code}</code> bo'yicha texnik muammo yuz berdi. Administrator buni ko'rib chiqadi — iltimos, qo'llab-quvvatlashga yozing.",
        "ru": "❗️ <b>Заказ не выполнен</b>\nПо заказу <code>{code}</code> возникла техническая проблема. Администратор разберётся — пожалуйста, напишите в поддержку.",
    },
}


async def _lang(tg_id: int) -> str:
    row = await db.fetchone("SELECT lang FROM users WHERE tg_id = ?", (tg_id,))
    return (row or {}).get("lang") or "uz"


async def send_user(order: dict, key: str, codes: list[str] | None = None) -> None:
    if not bot:
        return
    lang = await _lang(order["tg_id"])
    text = MSG[key][lang].format(
        code=db.code(order["id"]),
        title=html.escape(order.get("offer_name") or ""),
        who=recipient(order),
        codes="\n".join(f"<code>{html.escape(c)}</code>" for c in (codes or [])),
        redeem=_redeem_line(order, lang),
    )
    try:
        await bot.send_message(order["tg_id"], text)
    except Exception as e:  # user blocked the bot, etc.
        log.warning("send_user failed for %s: %s", order["tg_id"], e)


async def support_username() -> str:
    """Whom customers write to: SUPPORT_USERNAME, else the Telegram username of the first admin (they have used the bot, so it is on file)."""
    if SUPPORT_USERNAME:
        return SUPPORT_USERNAME
    for admin in ADMIN_IDS:
        row = await db.fetchone("SELECT username FROM users WHERE tg_id = ?", (admin,))
        if row and row["username"]:
            return row["username"]
    return ""


_short_sent: dict[int, float] = {}  # tg_id -> when a customer was last told the wallet cannot cover their order


def _want_text(lang: str, game: str, title: str, recipient: str) -> str:
    """What the customer's first message to the admin says: what they wanted to buy, typed for them."""
    who = f" ID: {recipient}" if recipient else ""
    if lang == "ru":
        return f"Здравствуйте! Хочу купить {game}: {title}. Бот сейчас не смог отправить.{who}"
    return f"Salom! {game}: {title} olmoqchiman. Bot hozir yubora olmadi.{who}"


async def wallet_short(tg_id: int, title: str, game: str = "", recipient: str = "") -> None:
    """The supplier wallet cannot pay for what the customer wants: instead of payment details the bot sends them a button that opens
    the admin's chat with the request already typed (what, how much, for which ID). The owner hears who was turned away."""
    now = time.time()
    if now - _short_sent.get(tg_id, 0) < 15:
        return
    _short_sent[tg_id] = now
    await admin_event("wallet_refused", f"🚫 <b>Mijozga hamyon yetmadi, admin bilan bog'lanishi aytildi</b>\n{html.escape(title)}", tg_id=tg_id)
    if not bot:
        return
    lang = await _lang(tg_id)
    kb, admin = None, await support_username()
    if admin:  # opens the owner's Telegram profile
        link = f"https://t.me/{admin}?text={quote(_want_text(lang, game or 'NexDonate', title, recipient))}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text={"uz": "💬 Admin", "ru": "💬 Админ"}[lang], url=link)]])
    try:
        await bot.send_message(tg_id, MSG["wallet_short"][lang].format(title=html.escape(title)), reply_markup=kb)
    except Exception as e:  # user blocked the bot, etc.
        log.warning("wallet_short failed for %s: %s", tg_id, e)


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