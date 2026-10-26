import html
from datetime import datetime, timedelta, timezone

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from . import api, db, fulfillment, notify, payments
from .games import REGISTRY
from .config import (
    ADMIN_IDS,
    CARD_BANK,
    CARD_HOLDER,
    CARD_NUMBER,
    CARD_RUB_BANK,
    CARD_RUB_HOLDER,
    CARD_RUB_NUMBER,
    FULFILLMENT_MODE,
    PAYMENT_PROVIDER,
    PAYMENT_TTL_MIN,
    RECEIPTS_DIR,
    SUPPORT_USERNAME,
    WEBAPP_DIR,
    WEBAPP_URL,
)

dp = Dispatcher()

TEXT = {
    "uz": {
        "fallback": "Buyurtma berish uchun «Ochish» tugmasini bosing, savol bo'lsa admin bilan bog'laning 👇",
        "welcome": "Paketni tanlang, ID kiriting, to'lang — qolganini biz bajaramiz.",
        "cancel_btn": "❌ Bekor qilish",
        "pay_instr": "💳 <b>To'lov</b> · <code>{code}</code>\n\nMahsulot: <b>{title}</b>\n{recipient_line}\n\nTo'lov summasi: <b>{amount}</b>\n❗️ <b>Aynan shu summani</b> o'tkazing.\n\nKarta raqami (bosib nusxalang):\n<code>{card}</code>\n{card_info}\n⏳ {minutes} daqiqa ichida to'lang.\nTo'lagach <b>chek skrinshotini</b> shu chatga yuboring 👇",
        "pay_no_card": "Kechirasiz, bu valyutada to'lov hozircha ulanmagan. /settings orqali boshqa valyutani tanlang yoki admin bilan bog'laning.",
        "pay_expired": "⌛️ Bu buyurtmaning to'lov muddati tugagan. Ilovada yangi buyurtma bering 👇",
        "pay_done": "Bu buyurtma allaqachon to'langan yoki bekor qilingan.",
        "receipt_ok": "✅ <b>Chek qabul qilindi</b> · <code>{code}</code>\nTo'lov tekshirilmoqda, odatda bir necha daqiqa ketadi. Natijani shu yerga yozamiz.",
        "receipt_dup": "Bu buyurtma uchun chek allaqachon yuborilgan va tekshirilmoqda. Iltimos, kuting.",
        "receipt_none": "Sizda to'lov kutayotgan buyurtma yo'q. Avval ilovada buyurtma bering 👇",
        "receipt_img": "Iltimos, to'lov chekining rasmini (skrinshot) yuboring.",
        "order_cancelled": "🚫 Buyurtma bekor qilindi.",
        "open": "🚀 Ochish",
        "admin": "💬 Admin",
        "ask_currency": "💳 <b>To'lov valyutasini tanlang</b>\n\nSo'm — O'zbekiston kartalari uchun,\nrubl — Rossiya kartalari uchun.",
        "prefs": "⚙️ Til: <b>{lang}</b> · Valyuta: <b>{cur}</b>\nO'zgartirish: /settings",
        "lang_name": "O'zbekcha",
        "cur_name": {"UZS": "so'm", "RUB": "rubl"},
    },
    "ru": {
        "fallback": "Чтобы сделать заказ, нажмите «Открыть», а по вопросам напишите админу 👇",
        "welcome": "Выберите пакет, введите ID, оплатите — остальное мы сделаем сами.",
        "cancel_btn": "❌ Отменить",
        "pay_instr": "💳 <b>Оплата</b> · <code>{code}</code>\n\nТовар: <b>{title}</b>\n{recipient_line}\n\nСумма к оплате: <b>{amount}</b>\n❗️ Переведите <b>ровно эту сумму</b>.\n\nНомер карты (нажмите, чтобы скопировать):\n<code>{card}</code>\n{card_info}\n⏳ Оплатите в течение {minutes} минут.\nПосле оплаты отправьте <b>скриншот чека</b> в этот чат 👇",
        "pay_no_card": "Извините, оплата в этой валюте пока не подключена. Выберите другую валюту через /settings или напишите админу.",
        "pay_expired": "⌛️ Время оплаты этого заказа истекло. Оформите новый заказ в приложении 👇",
        "pay_done": "Этот заказ уже оплачен или отменён.",
        "receipt_ok": "✅ <b>Чек принят</b> · <code>{code}</code>\nОплата проверяется, обычно это занимает несколько минут. Результат напишем сюда.",
        "receipt_dup": "Чек по этому заказу уже отправлен и проверяется. Пожалуйста, подождите.",
        "receipt_none": "У вас нет заказа, ожидающего оплаты. Сначала оформите заказ в приложении 👇",
        "receipt_img": "Пожалуйста, отправьте изображение чека (скриншот).",
        "order_cancelled": "🚫 Заказ отменён.",
        "open": "🚀 Открыть",
        "admin": "💬 Админ",
        "ask_currency": "💳 <b>Выберите валюту оплаты</b>\n\nСум — для карт Узбекистана,\nрубль — для карт России.",
        "prefs": "⚙️ Язык: <b>{lang}</b> · Валюта: <b>{cur}</b>\nИзменить: /settings",
        "lang_name": "Русский",
        "cur_name": {"UZS": "сум", "RUB": "рубль"},
    },
}
ASK_LANGUAGE = "🌐 <b>Tilni tanlang / Выберите язык</b>"


async def _touch(tg_user) -> None:
    """Remember the person; tell the admin the first time somebody new shows up."""
    if await db.touch_user(tg_user):
        nick = f" @{html.escape(tg_user.username)}" if tg_user.username else ""
        await notify.admin_event("new_user", f"👤 <b>Yangi foydalanuvchi</b>{nick}", tg_id=tg_user.id)

async def _user_row(tg_id: int) -> dict | None:
    return await db.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))


async def _lang_of(message: Message) -> str:
    lang = "ru" if (message.from_user.language_code or "").startswith("ru") else "uz"
    row = await _user_row(message.from_user.id)
    return row["lang"] if row and row["lang"] else lang


def _main_kb(lang: str) -> InlineKeyboardMarkup | None:
    t = TEXT[lang]
    buttons = []
    if WEBAPP_URL:
        buttons.append(InlineKeyboardButton(text=t["open"], web_app=WebAppInfo(url=WEBAPP_URL)))
    if SUPPORT_USERNAME:  # opens the owner's Telegram profile so customers can write to them
        buttons.append(InlineKeyboardButton(text=t["admin"], url=f"https://t.me/{SUPPORT_USERNAME}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None


def _lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="ob:lang:uz"),
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="ob:lang:ru"),
            ]
        ]
    )


def _currency_kb(lang: str) -> InlineKeyboardMarkup:
    names = TEXT[lang]["cur_name"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"🇺🇿 {names['UZS'].capitalize()} (UZS)", callback_data="ob:cur:UZS"),
                InlineKeyboardButton(text=f"🇷🇺 {names['RUB'].capitalize()} (RUB)", callback_data="ob:cur:RUB"),
            ]
        ]
    )


async def _ask_language(message: Message) -> None:
    await message.answer(ASK_LANGUAGE, reply_markup=_lang_kb())


async def _send_welcome(target: Message, user_row: dict) -> None:
    """The normal start message (banner + Open/Admin buttons) in the user's language, with their saved choices."""
    lang = user_row["lang"] or "uz"
    t = TEXT[lang]
    caption = f"{t['welcome']}\n\n" + t["prefs"].format(lang=t["lang_name"], cur=t["cur_name"].get(user_row["currency"] or "UZS", "so'm"))
    banner = WEBAPP_DIR / "assets" / f"banner_{lang}.png"
    if banner.exists():
        await target.answer_photo(FSInputFile(banner), caption=caption, reply_markup=_main_kb(lang))
    else:
        await target.answer(caption, reply_markup=_main_kb(lang))


async def _onboard_or(message: Message, then) -> None:
    """New users (language/currency not chosen yet) are asked first; everyone else goes straight on."""
    await _touch(message.from_user)
    row = await _user_row(message.from_user.id)
    if not row["setup_done"]:
        return await _ask_language(message)
    await then(message, row)


@dp.message(CommandStart())
async def start(message: Message, command: CommandObject) -> None:
    args = command.args or ""
    if args.startswith("pay_"):  # the Mini App sends the customer here to pay for an order
        await _touch(message.from_user)
        return await _show_payment(message, args[4:])
    await _onboard_or(message, _send_welcome)


@dp.message(Command("settings"))
async def settings(message: Message) -> None:
    await _touch(message.from_user)
    await _ask_language(message)  # same two questions again: language, then currency


@dp.callback_query(F.data.startswith("ob:"))
async def onboarding_cb(cb: CallbackQuery) -> None:
    await _touch(cb.from_user)
    _, kind, value = cb.data.split(":", 2)
    if kind == "lang" and value in ("uz", "ru"):
        await db.execute("UPDATE users SET lang = ? WHERE tg_id = ?", (value, cb.from_user.id))
        await cb.message.edit_text(TEXT[value]["ask_currency"], reply_markup=_currency_kb(value))
        return await cb.answer()
    if kind == "cur" and value in ("UZS", "RUB"):
        await db.execute("UPDATE users SET currency = ?, setup_done = 1 WHERE tg_id = ?", (value, cb.from_user.id))
        row = await _user_row(cb.from_user.id)
        try:
            await cb.message.delete()
        except Exception:
            pass
        await _send_welcome(cb.message, row)
        return await cb.answer("✅")
    await cb.answer()

@dp.message(Command("id"))
async def my_id(message: Message) -> None:
    # handy for setting ADMIN_IDS: the numeric id is what the bot needs, a @username can change
    await message.answer(f"Telegram ID: <code>{message.from_user.id}</code>")


def _money(amount: int, currency: str) -> str:
    return f"{amount:,} {'₽' if currency == 'RUB' else 'so' + chr(39) + 'm'}".replace(",", " ")


def _card_for(currency: str) -> tuple[str, str]:
    if currency == "RUB":
        return CARD_RUB_NUMBER, " · ".join(p for p in (CARD_RUB_BANK, CARD_RUB_HOLDER) if p)
    return CARD_NUMBER, " · ".join(p for p in (CARD_BANK, CARD_HOLDER) if p)


async def _show_payment(message: Message, order_id: str) -> None:
    row = await _user_row(message.from_user.id)
    lang = (row or {}).get("lang") or "uz"
    t = TEXT[lang]
    order = await db.get_order(order_id)
    if not order or order["tg_id"] != message.from_user.id:
        return await message.answer(t["pay_done"])
    if order["status"] == "payment_review":
        return await message.answer(t["receipt_dup"])
    if order["status"] != "awaiting_payment":
        return await message.answer(t["pay_done"])
    if order["expires_at"] < db.now():
        return await message.answer(t["pay_expired"], reply_markup=_main_kb(lang))
    if not await api._wallet_covers(0):  # the supplier wallet is not what it was when the order was taken (this order counts as waiting)
        await db.update_order(order_id, status="cancelled")  # so it stops holding money back from others
        g = REGISTRY.get(order["game"])
        who = "" if (g and g.delivery == "code") else (("@" if g and g.recipient == "username" else "") + (order["player_id"] or ""))
        return await notify.wallet_short(message.from_user.id, order["offer_name"] or "", g.name if g else "PUBG Mobile", who)
    card, card_info = _card_for(order["currency"])
    if not card:
        await notify.admin_event("config", f"⚠️ <b>{order['currency']} uchun karta kiritilmagan</b>, mijoz to'lay olmadi · {db.code(order_id)}", order)
        return await message.answer(t["pay_no_card"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t["cancel_btn"], callback_data=f"ord:cancel:{order_id}")]])
    await message.answer(
        t["pay_instr"].format(
            code=db.code(order_id), title=html.escape(order["offer_name"] or ""), recipient_line=notify.recipient_line(order, lang),
            amount=_money(order["amount"], order["currency"]), card=html.escape(card),
            card_info=(html.escape(card_info) + "\n") if card_info else "", minutes=PAYMENT_TTL_MIN,
        ),
        reply_markup=kb,
    )
    if not order["pay_opened_at"]:
        await db.update_order(order_id, pay_opened_at=db.now())
        await notify.admin_event("pay_opened", f"➡️ <b>Mijoz botga o'tdi, karta ko'rsatildi</b> · {db.code(order_id)} · {_money(order['amount'], order['currency'])}", order)


@dp.callback_query(F.data.startswith("ord:cancel:"))
async def cancel_cb(cb: CallbackQuery) -> None:
    order = await db.get_order(cb.data.split(":", 2)[2])
    if not order or order["tg_id"] != cb.from_user.id or order["status"] != "awaiting_payment":
        return await cb.answer()
    await db.update_order(order["id"], status="cancelled")
    lang = ((await _user_row(cb.from_user.id)) or {}).get("lang") or "uz"
    try:
        await cb.message.edit_text(TEXT[lang]["order_cancelled"])
    except Exception:
        pass
    await notify.admin_event("cancelled", f"🚫 <b>Mijoz buyurtmani bekor qildi</b> · {db.code(order['id'])}", order)
    await cb.answer()


@dp.message(F.chat.type == "private", F.photo | F.document)
async def receipt(message: Message) -> None:
    """The customer's receipt screenshot: saved, forwarded to the admin with every detail, then checked."""
    await _touch(message.from_user)
    lang = ((await _user_row(message.from_user.id)) or {}).get("lang") or "uz"
    t = TEXT[lang]
    doc = message.document
    if doc and not (doc.mime_type or "").startswith("image/"):
        return await message.answer(t["receipt_img"])
    if doc and (doc.file_size or 0) > 10 * 1024 * 1024:
        return await message.answer(t["receipt_img"])
    now = db.now()
    order = await db.fetchone(
        "SELECT * FROM orders WHERE tg_id = ? AND status = 'awaiting_payment' AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
        (message.from_user.id, now),
    )
    if not order:
        waiting = await db.fetchone("SELECT 1 AS x FROM orders WHERE tg_id = ? AND status = 'payment_review'", (message.from_user.id,))
        return await message.answer(t["receipt_dup" if waiting else "receipt_none"], reply_markup=None if waiting else _main_kb(lang))
    path = RECEIPTS_DIR / f"{order['id']}.img"
    await message.bot.download(doc.file_id if doc else message.photo[-1].file_id, destination=path)
    await db.update_order(order["id"], status="payment_review", receipt_file=str(path), receipt_at=now)
    order = await db.get_order(order["id"])
    await message.answer(t["receipt_ok"].format(code=db.code(order["id"])))
    verdict = await payments.try_auto_confirm(order)
    note = {
        "confirmed": "✅ <b>Avtomatik tasdiqlandi</b>: kartaga shu summa tushgan.",
        "ambiguous": "⚠️ Bir nechta mos tushum/buyurtma bor, avtomatik tasdiqlanmadi. O'zingiz tekshiring.",
    }.get(verdict, "🔎 Avtomatik tekshiruv: kartaga mos tushum hozircha topilmadi. Chekni va kartani o'zingiz tekshiring.")
    await notify.admin_receipt(await db.get_order(order["id"]), str(path), note, buttons=verdict != "confirmed")

STATUS_ICON = {
    "awaiting_payment": "🕓", "payment_review": "🧾", "paid": "💳", "processing": "⚙️",
    "completed": "✅", "failed": "❗️", "cancelled": "🚫", "expired": "⌛️",
}
STATUS_UZ = {
    "awaiting_payment": "to'lov kutilmoqda", "payment_review": "chek tekshiruvda", "paid": "to'langan",
    "processing": "yuborilmoqda", "completed": "yetkazilgan", "failed": "xatolik",
    "cancelled": "bekor qilingan", "expired": "muddati o'tgan",
}




def _admin_kb(extra: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔄 Yangilash", callback_data="adm:home"),
            InlineKeyboardButton(text="📋 Buyurtmalar", callback_data="adm:orders"),
        ],
        [InlineKeyboardButton(text="🧾 Tekshiruvdagi cheklar", callback_data="adm:receipts")],
        [InlineKeyboardButton(text="📜 So'nggi voqealar", callback_data="adm:events")],
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
        f"{fulfillment.supplier.name} balans: <b>{supplier}</b>\n"
        f"Foydalanuvchilar: <b>{users}</b>\n\n" + "\n".join(lines)
    )


async def _events_text() -> str:
    rows = await db.fetchall("SELECT * FROM events ORDER BY id DESC LIMIT 15")
    if not rows:
        return "📜 <b>So'nggi voqealar</b>\n\nHozircha yo'q"
    out = ["📜 <b>So'nggi 15 ta voqea</b> (yangisi tepada)\n"]
    for r in rows:
        when = datetime.fromtimestamp(r["ts"], timezone(timedelta(hours=5))).strftime("%d.%m %H:%M:%S")
        out.append(f"<code>{when}</code> {r['text'].splitlines()[0]}")
    return "\n".join(out)


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


@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
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

    text = await {"orders": _orders_text, "events": _events_text}.get(action, _panel_text)()
    try:
        await cb.message.edit_text(text, reply_markup=_admin_kb())
    except Exception:  # "message is not modified" when nothing changed
        pass
    await cb.answer("Yangilandi" if action == "home" else None)

USERS_PER_PAGE = 20
TZ = timezone(timedelta(hours=5))  # Tashkent


def _person(u: dict) -> str:
    name = html.escape(" ".join(p for p in (u["first_name"], u["last_name"]) if p) or "—")
    nick = f"@{html.escape(u['username'])}" if u["username"] else "nik yo'q"
    return f'<a href="tg://user?id={u["tg_id"]}">{name}</a> · {nick}'


async def _users_page(page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    total = (await db.fetchone("SELECT COUNT(*) AS n FROM users"))["n"]
    pages = max(1, -(-total // USERS_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    rows = await db.fetchall(
        """SELECT u.*, (SELECT COUNT(*) FROM orders o WHERE o.tg_id = u.tg_id AND o.status = 'completed') AS bought
           FROM users u ORDER BY COALESCE(u.last_seen, u.created_at) DESC LIMIT ? OFFSET ?""",
        (USERS_PER_PAGE, page * USERS_PER_PAGE),
    )
    if not rows:
        return "👥 <b>Foydalanuvchilar</b>\n\nHozircha hech kim yo'q", None
    lines = [f"👥 <b>Foydalanuvchilar: {total}</b> · {page + 1}/{pages}-sahifa\n"]
    for n, u in enumerate(rows, start=page * USERS_PER_PAGE + 1):
        seen = datetime.fromtimestamp(u["last_seen"] or u["created_at"], TZ).strftime("%d.%m %H:%M")
        bought = f" · 🛒 {u['bought']}" if u["bought"] else ""
        lines.append(f"{n}. {_person(u)}\n    <code>{u['tg_id']}</code> · {seen}{bought}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Oldingi", callback_data=f"usr:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Keyingi ▶️", callback_data=f"usr:{page + 1}"))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None


@dp.message(Command("users"), F.from_user.id.in_(ADMIN_IDS))
async def users_cmd(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    text, kb = await _users_page(0)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("usr:"))
async def users_page_cb(cb: CallbackQuery) -> None:
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    text, kb = await _users_page(int(cb.data.split(":", 1)[1]))
    try:
        await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        pass
    await cb.answer()


@dp.message(Command("count"), F.from_user.id.in_(ADMIN_IDS))
async def count_cmd(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    day = 24 * 3600
    now = db.now()
    midnight = int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    one = lambda sql, *a: db.fetchone(sql, a)  # noqa: E731
    total = (await one("SELECT COUNT(*) AS n FROM users"))["n"]
    today = (await one("SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", midnight))["n"]
    week = (await one("SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", now - 7 * day))["n"]
    active = (await one("SELECT COUNT(*) AS n FROM users WHERE COALESCE(last_seen, created_at) >= ?", now - 7 * day))["n"]
    buyers = (await one("SELECT COUNT(DISTINCT tg_id) AS n FROM orders WHERE status = 'completed'"))["n"]
    await message.answer(
        "📊 <b>Bot foydalanuvchilari</b>\n\n"
        f"Jami: <b>{total}</b>\n"
        f"Bugun qo'shilgan: <b>{today}</b>\n"
        f"Oxirgi 7 kunda qo'shilgan: <b>{week}</b>\n"
        f"Oxirgi 7 kunda faol: <b>{active}</b>\n"
        f"Xarid qilganlar: <b>{buyers}</b>"
    )


@dp.message(Command("supplier"), F.from_user.id.in_(ADMIN_IDS))
async def supplier_cmd(message: Message, command: CommandObject) -> None:
    """Admin only: is the supplier reachable, what is the balance, are the UC products readable. `/supplier 22` looks inside category 22."""
    if message.from_user.id not in ADMIN_IDS:
        return
    args = (command.args or "").strip()
    if args.lower().startswith("find ") and hasattr(fulfillment.supplier, "discover"):
        wait = await message.answer("⏳ Katalog qidirilmoqda (1-2 daqiqa)…")
        found = await fulfillment.supplier.discover(args[5:].split(","))
        return await wait.edit_text("\n".join(found)[:3900])
    category = args or None
    wait = await message.answer("⏳ Yetkazib beruvchi tekshirilmoqda…")
    s = fulfillment.supplier
    try:
        if hasattr(s, "probe"):
            lines = await s.probe(category)
        else:
            bal = await s.balance()
            lines = [f"✅ {s.name}: balans <b>{bal.get('balance')} {bal.get('currency')}</b>"]
    except Exception as e:
        lines = [f"❌ {s.name}: {html.escape(str(e))[:300]}"]
    await wait.edit_text(f"🔌 <b>{s.name}</b>\n\n" + "\n".join(lines)[:3800])


@dp.callback_query(F.data.startswith("pay:"))
async def payment_decision(cb: CallbackQuery) -> None:
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Ruxsat yo'q", show_alert=True)
    _, action, order_id = cb.data.split(":", 2)
    order = await db.get_order(order_id)
    if not order or order["status"] != "payment_review":
        return await cb.answer("Bu buyurtma allaqachon ko'rib chiqilgan", show_alert=True)

    who = html.escape(cb.from_user.full_name)
    if action == "ok":
        done = await payments.confirm_order(order_id, f"admin {who}")
        mark = f"✅ Tasdiqlandi ({who})" if done else "⚠️ Allaqachon ko'rib chiqilgan"
    else:
        done = await payments.reject_order(order_id, f"admin {who}")
        mark = f"❌ Rad etildi ({who})" if done else "⚠️ Allaqachon ko'rib chiqilgan"
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


# Must stay the LAST handler: it answers anything the handlers above did not take (plain text, photos, stickers,
# unknown commands) in private chats with the same "Open" and "Admin" buttons.
@dp.message(F.chat.type == "private")
async def fallback(message: Message) -> None:
    async def reply(msg: Message, row: dict) -> None:
        lang = row["lang"] or "uz"
        await msg.answer(TEXT[lang]["fallback"], reply_markup=_main_kb(lang))

    await _onboard_or(message, reply)
