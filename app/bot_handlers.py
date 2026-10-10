import html
from datetime import datetime, timedelta, timezone

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
from .config import ADMIN_IDS, FULFILLMENT_MODE, PAYMENT_PROVIDER, SUPPORT_USERNAME, WEBAPP_DIR, WEBAPP_URL

dp = Dispatcher()

TEXT = {
    "uz": {
        "welcome": "UC tanlang, ID kiriting, to'lang — qolganini biz bajaramiz.",
        "open": "🚀 Ochish",
        "admin": "💬 Admin",
    },
    "ru": {
        "welcome": "Выберите UC, введите ID, оплатите — остальное мы сделаем сами.",
        "open": "🚀 Открыть",
        "admin": "💬 Админ",
    },
}


@dp.message(CommandStart())
async def start(message: Message) -> None:
    lang = "ru" if (message.from_user.language_code or "").startswith("ru") else "uz"
    row = await db.fetchone("SELECT lang FROM users WHERE tg_id = ?", (message.from_user.id,))
    if row and row["lang"]:
        lang = row["lang"]
    t = TEXT[lang]
    buttons = []
    if WEBAPP_URL:
        buttons.append(InlineKeyboardButton(text=t["open"], web_app=WebAppInfo(url=WEBAPP_URL)))
    if SUPPORT_USERNAME:  # opens the owner's Telegram profile so customers can write to them
        buttons.append(InlineKeyboardButton(text=t["admin"], url=f"https://t.me/{SUPPORT_USERNAME}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
    banner = WEBAPP_DIR / "assets" / f"banner_{lang}.png"
    if banner.exists():
        await message.answer_photo(FSInputFile(banner), caption=t["welcome"], reply_markup=kb)
    else:
        await message.answer(t["welcome"], reply_markup=kb)


@dp.message(Command("id"))
async def my_id(message: Message) -> None:
    # handy for setting ADMIN_IDS: the numeric id is what the bot needs, a @username can change
    await message.answer(f"Telegram ID: <code>{message.from_user.id}</code>")


STATUS_ICON = {
    "awaiting_payment": "🕓", "payment_review": "🧾", "paid": "💳", "processing": "⚙️",
    "completed": "✅", "failed": "❗️", "cancelled": "🚫", "expired": "⌛️",
}
STATUS_UZ = {
    "awaiting_payment": "to'lov kutilmoqda", "payment_review": "chek tekshiruvda", "paid": "to'langan",
    "processing": "yuborilmoqda", "completed": "yetkazilgan", "failed": "xatolik",
    "cancelled": "bekor qilingan", "expired": "muddati o'tgan",
}


def _money(amount: int, currency: str) -> str:
    return f"{amount:,} {'₽' if currency == 'RUB' else 'so' + chr(39) + 'm'}".replace(",", " ")


def _admin_kb(extra: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔄 Yangilash", callback_data="adm:home"),
            InlineKeyboardButton(text="📋 Buyurtmalar", callback_data="adm:orders"),
        ],
        [InlineKeyboardButton(text="🧾 Tekshiruvdagi cheklar", callback_data="adm:receipts")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=(extra or []) + rows)


async def _panel_text() -> str:
    rows = await db.fetchall(
        "SELECT status, currency, COUNT(*) AS n, COALESCE(SUM(amount),0) AS s FROM orders GROUP BY status, currency ORDER BY status"
    )
    lines = [
        f"{STATUS_ICON.get(r['status'], '•')} {STATUS_UZ.get(r['status'], r['status'])}: <b>{r['n']}</b> · {_money(r['s'], r['currency'])}"
        for r in rows
    ] or ["Hozircha buyurtmalar yo'q"]
    users = (await db.fetchone("SELECT COUNT(*) AS n FROM users"))["n"]
    try:
        bal = await fulfillment.supplier.balance()
        supplier = f"{bal.get('balance')} {bal.get('currency')}"
    except Exception as e:
        supplier = f"xato: {html.escape(str(e))[:100]}"
    return (
        "🛠 <b>Admin panel</b>\n"
        f"Yetkazish: <code>{FULFILLMENT_MODE}</code> · to'lov: <code>{PAYMENT_PROVIDER}</code>\n"
        f"FazerCards balans: <b>{supplier}</b>\n"
        f"Foydalanuvchilar: <b>{users}</b>\n\n" + "\n".join(lines)
    )


async def _orders_text() -> str:
    rows = await db.fetchall(
        "SELECT id, offer_name, amount, currency, status, created_at FROM orders ORDER BY created_at DESC LIMIT 10"
    )
    if not rows:
        return "📋 <b>Oxirgi buyurtmalar</b>\n\nHozircha yo'q"
    out = ["📋 <b>Oxirgi 10 ta buyurtma</b>\n"]
    for r in rows:
        when = datetime.fromtimestamp(r["created_at"], timezone(timedelta(hours=5))).strftime("%d.%m %H:%M")
        out.append(
            f"{STATUS_ICON.get(r['status'], '•')} <code>{db.code(r['id'])}</code> · {html.escape(r['offer_name'] or '')} · "
            f"{_money(r['amount'], r['currency'])} · {when}"
        )
    return "\n".join(out)


@dp.message(Command("admin"))
async def admin(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(await _panel_text(), reply_markup=_admin_kb())


@dp.callback_query(F.data.startswith("adm:"))
async def admin_cb(cb: CallbackQuery) -> None:
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    action = cb.data.split(":", 1)[1]

    if action == "new":  # pressed under the /start banner: open the panel as a new message
        await cb.message.answer(await _panel_text(), reply_markup=_admin_kb())
        return await cb.answer()

    if action == "receipts":
        pending = await db.fetchall("SELECT * FROM orders WHERE status = 'payment_review' ORDER BY created_at")
        if not pending:
            return await cb.answer("Tekshiruvda chek yo'q", show_alert=True)
        for order in pending:
            if order["receipt_file"]:
                await notify.admin_receipt(order, order["receipt_file"])
        return await cb.answer(f"{len(pending)} ta chek yuborildi")

    text = await (_orders_text() if action == "orders" else _panel_text())
    try:
        await cb.message.edit_text(text, reply_markup=_admin_kb())
    except Exception:  # "message is not modified" when nothing changed
        pass
    await cb.answer("Yangilandi" if action == "home" else None)

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
