import html

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from . import db, fulfillment, notify
from .config import ADMIN_IDS, FULFILLMENT_MODE, PAYMENT_PROVIDER, WEBAPP_DIR, WEBAPP_URL

dp = Dispatcher()

TEXT = {
    "uz": {
        "welcome": "UC tanlang, ID kiriting, to'lang — qolganini biz bajaramiz.",
        "open": "🚀 Ochish",
    },
    "ru": {
        "welcome": "Выберите UC, введите ID, оплатите — остальное мы сделаем сами.",
        "open": "🚀 Открыть",
    },
}


@dp.message(CommandStart())
async def start(message: Message) -> None:
    lang = "ru" if (message.from_user.language_code or "").startswith("ru") else "uz"
    row = await db.fetchone("SELECT lang FROM users WHERE tg_id = ?", (message.from_user.id,))
    if row and row["lang"]:
        lang = row["lang"]
    t = TEXT[lang]
    kb = None
    if WEBAPP_URL:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t["open"], web_app=WebAppInfo(url=WEBAPP_URL))]]
        )
    banner = WEBAPP_DIR / "assets" / f"banner_{lang}.png"
    if banner.exists():
        await message.answer_photo(FSInputFile(banner), caption=t["welcome"], reply_markup=kb)
    else:
        await message.answer(t["welcome"], reply_markup=kb)


@dp.message(Command("id"))
async def my_id(message: Message) -> None:
    # handy for setting ADMIN_IDS: the numeric id is what the bot needs, a @username can change
    await message.answer(f"Telegram ID: <code>{message.from_user.id}</code>")


@dp.message(Command("admin"))
async def admin(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    rows = await db.fetchall(
        "SELECT status, currency, COUNT(*) AS n, COALESCE(SUM(amount),0) AS s FROM orders GROUP BY status, currency"
    )
    lines = [
        f"• {r['status']}: <b>{r['n']}</b> ({r['s']:,} {'₽' if r['currency'] == 'RUB' else 'so' + chr(39) + 'm'})".replace(",", " ")
        for r in rows
    ] or ["Buyurtmalar yo'q"]
    try:
        bal = await fulfillment.supplier.balance()
        supplier = f"{bal.get('balance')} {bal.get('currency')}"
    except Exception as e:
        supplier = f"xato: {html.escape(str(e))[:120]}"
    await message.answer(
        "<b>Admin panel</b>\n"
        f"Rejim: fulfillment=<code>{FULFILLMENT_MODE}</code>, to'lov=<code>{PAYMENT_PROVIDER}</code>\n"
        f"FazerCards balans: <b>{supplier}</b>\n\n" + "\n".join(lines)
    )


@dp.callback_query(F.data.startswith("pay:"))
async def payment_decision(cb: CallbackQuery) -> None:
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    _, action, order_id = cb.data.split(":", 2)
    order = await db.get_order(order_id)
    if not order or order["status"] != "payment_review":
        return await cb.answer("Bu buyurtma allaqachon ko'rib chiqilgan", show_alert=True)

    if action == "ok":
        await db.update_order(order_id, status="paid")
        fulfillment.start(order_id)
        await notify.send_user(order, "payment_confirmed")
        mark = f"✅ Tasdiqlandi ({html.escape(cb.from_user.full_name)})"
    else:
        # give the customer another try with a fresh window
        await db.update_order(order_id, status="awaiting_payment", expires_at=db.now() + 15 * 60)
        await notify.send_user(order, "payment_rejected")
        mark = f"❌ Rad etildi ({html.escape(cb.from_user.full_name)})"

    base = cb.message.caption or cb.message.text or ""
    try:
        if cb.message.caption is not None:
            await cb.message.edit_caption(caption=f"{html.escape(base)}\n\n{mark}")
        else:
            await cb.message.edit_text(f"{html.escape(base)}\n\n{mark}")
    except Exception:
        pass
    await cb.answer("Bajarildi")


@dp.callback_query(F.data.startswith("retry:"))
async def retry_order(cb: CallbackQuery) -> None:
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    order_id = cb.data.split(":", 1)[1]
    order = await db.get_order(order_id)
    if not order or order["status"] != "failed":
        return await cb.answer("Qayta urinib bo'lmaydi", show_alert=True)
    await db.update_order(order_id, status="paid", error=None)
    fulfillment.start(order_id)
    await cb.answer("Qayta ishga tushirildi")
