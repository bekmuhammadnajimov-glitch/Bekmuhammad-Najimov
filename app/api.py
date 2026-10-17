import asyncio
import hmac
import json
import re
import time
import uuid
from collections import defaultdict, deque
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from . import click, db, fulfillment, notify, payments
from .auth import current_user
from .config import (
    CARD_NUMBER,
    CARD_RUB_NUMBER,
    CLICK_READY,
    CURRENCIES,
    FULFILLMENT_MODE,
    INCOMING_SECRET,
    PAYMENT_TTL_MIN,
    PUBG_VALIDATE_CATEGORY,
    SUPPORT_USERNAME,
    provider_for,
)
from .pricing import price_in, pubg_packs, quote_uc, uc_options
from .supplier import SupplierError

router = APIRouter(prefix="/api")

GAMES = [
    {"id": "pubg", "name": "PUBG Mobile", "currency": "UC", "active": True},
    {"id": "free_fire", "name": "Free Fire", "currency": "Diamonds", "active": False},
    {"id": "mobile_legends", "name": "Mobile Legends", "currency": "Diamonds", "active": False},
    {"id": "codm", "name": "Call of Duty Mobile", "currency": "CP", "active": False},
    {"id": "genshin", "name": "Genshin Impact", "currency": "Crystals", "active": False},
    {"id": "honor_of_kings", "name": "Honor of Kings", "currency": "Tokens", "active": False},
    {"id": "steam", "name": "Steam", "currency": "", "active": False},
]

_ID_RE = re.compile(r"\d{5,14}")
_hits: dict[int, deque] = defaultdict(deque)


def _rate_limit(tg_id: int, limit: int = 20, window: int = 60) -> None:
    q = _hits[tg_id]
    now = time.time()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "rate_limited")
    q.append(now)


_wallet = {"at": 0.0, "balance": None, "alerted": 0.0}


async def _wallet_covers(cost_usd) -> bool:
    """Live mode: never take an order the supplier wallet could not pay for, counting orders that are already waiting."""
    if FULFILLMENT_MODE != "live":
        return True
    now = time.time()
    if now - _wallet["at"] > 30:
        try:
            _wallet["balance"] = Decimal(str((await fulfillment.supplier.balance())["balance"]))
        except Exception:
            _wallet["balance"] = None
        _wallet["at"] = now
    balance = _wallet["balance"]
    waiting = await db.fetchall("SELECT cost_usd FROM orders WHERE status IN ('awaiting_payment', 'payment_review', 'paid')")
    need = sum((Decimal(str(r["cost_usd"] or 0)) for r in waiting), Decimal(0)) + Decimal(str(cost_usd))
    if balance is not None and balance >= need:
        return True
    if now - _wallet["alerted"] > 1800:  # the owner hears about it, but not on every attempt
        _wallet["alerted"] = now
        shown = "o'qib bo'lmadi" if balance is None else f"{balance}"
        await notify.admin_event(
            "wallet_low",
            f"⚠️ <b>Yetkazib beruvchi hamyoni yetarli emas</b> ({fulfillment.supplier.name})\n"
            f"Balans: {shown} USD · kerak: {need} USD\nBuyurtmalar to'xtatildi. Hamyonni to'ldiring.",
        )
    return False


def _money(amount: int, currency: str) -> str:
    return f"{amount:,} {'₽' if currency == 'RUB' else 'so' + chr(39) + 'm'}".replace(",", " ")


def _payment_view(order: dict) -> dict:
    provider = provider_for(order["currency"])
    if provider == "click":
        return {
            "provider": "click",
            "available": CLICK_READY,  # false until the Click merchant keys are set
            "url": click.payment_url(order) if CLICK_READY else "",
            "amount": order["amount"],
            "currency": order["currency"],
        }
    if provider == "manual_card":
        # Card transfer: the card number, exact amount and the receipt step all live in the bot chat.
        number = CARD_RUB_NUMBER if order["currency"] == "RUB" else CARD_NUMBER
        username = notify.bot_username
        return {
            "provider": "bot",
            "available": bool(number and username),  # false until a card is set for this currency
            "bot_url": f"https://t.me/{username}?start=pay_{order['id']}" if username else "",
            "amount": order["amount"],
            "currency": order["currency"],
        }
    return {"provider": "mock"}


def _items_of(order: dict) -> list[dict]:
    if order.get("items"):
        try:
            return json.loads(order["items"])
        except ValueError:
            pass
    return [{"offer_id": order["offer_id"], "name": order["offer_name"], "uc": order["uc"], "qty": 1}]


def _public(order: dict) -> dict:
    return {
        "id": order["id"],
        "code": db.code(order["id"]),
        "game": order["game"],
        "uc": order["uc"],
        "offer_id": order["offer_id"],
        "offer_name": order["offer_name"],
        "items": _items_of(order),
        "player_id": order["player_id"],
        "nick": order["nick"],
        "amount": order["amount"],
        "currency": order["currency"],
        "status": order["status"],
        "created_at": order["created_at"],
        "expires_at": order["expires_at"],
        "payment": _payment_view(order) if order["status"] == "awaiting_payment" else None,
    }


async def _expire_stale() -> None:
    await db.execute(
        "UPDATE orders SET status = 'expired', updated_at = ? WHERE status = 'awaiting_payment' AND expires_at < ?",
        (db.now(), db.now()),
    )


async def _own_order(order_id: str, user: dict) -> dict:
    await _expire_stale()
    order = await db.get_order(order_id)
    if not order or order["tg_id"] != user["tg_id"]:
        raise HTTPException(404, "not_found")
    return order


async def _stats(tg_id: int) -> dict:
    rows = await db.fetchall(
        "SELECT currency, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS spent FROM orders "
        "WHERE tg_id = ? AND status = 'completed' GROUP BY currency",
        (tg_id,),
    )
    spent = {c: 0 for c in CURRENCIES}
    for r in rows:
        spent[r["currency"]] = r["spent"]
    return {"orders": sum(r["n"] for r in rows), "spent": spent}


# ---------------------------------------------------------------- bootstrap
@router.get("/bootstrap")
async def bootstrap(user: dict = Depends(current_user)):
    await _expire_stale()
    from_price = from_price_rub = None
    try:
        # never let a slow supplier hold up the home screen
        packs = await asyncio.wait_for(pubg_packs(fulfillment.supplier), timeout=2.5)
        uc = [p for p in packs if p["section"] == "uc"]
        from_price = min((p["price"] for p in uc), default=None)
        from_price_rub = min((p["price_rub"] for p in uc), default=None)
    except Exception:
        pass
    ids = await db.fetchall(
        "SELECT id, game, player_id, nick FROM saved_ids WHERE tg_id = ? ORDER BY id DESC", (user["tg_id"],)
    )
    recent = await db.fetchall(
        "SELECT * FROM orders WHERE tg_id = ? ORDER BY created_at DESC LIMIT 3", (user["tg_id"],)
    )
    return {
        "user": {
            "id": user["tg_id"],
            "first_name": user["first_name"],
            "last_name": user["last_name"],
            "username": user["username"],
            "photo_url": user["photo_url"],
            "lang": user["lang"],
            "currency": user["currency"],
            "setup_done": bool(user["setup_done"]),
        },
        "stats": await _stats(user["tg_id"]),
        "games": [
            {**g, "from_price": from_price if g["id"] == "pubg" else None,
             "from_price_rub": from_price_rub if g["id"] == "pubg" else None}
            for g in GAMES
        ],
        "saved_ids": ids,
        "recent_orders": [_public(o) for o in recent],
        "support_username": SUPPORT_USERNAME,
        "payment_provider": {c: provider_for(c) for c in CURRENCIES},
    }


class LangIn(BaseModel):
    lang: str


@router.post("/lang")
async def set_lang(body: LangIn, user: dict = Depends(current_user)):
    if body.lang not in ("uz", "ru"):
        raise HTTPException(400, "bad_lang")
    await db.execute("UPDATE users SET lang = ? WHERE tg_id = ?", (body.lang, user["tg_id"]))
    return {"ok": True}


class CurrencyIn(BaseModel):
    currency: str


@router.post("/currency")
async def set_currency(body: CurrencyIn, user: dict = Depends(current_user)):
    if body.currency not in CURRENCIES:
        raise HTTPException(400, "bad_currency")
    await db.execute("UPDATE users SET currency = ? WHERE tg_id = ?", (body.currency, user["tg_id"]))
    return {"ok": True}


class SetupIn(BaseModel):
    lang: str
    currency: str


@router.post("/setup")
async def finish_setup(body: SetupIn, user: dict = Depends(current_user)):
    """First-run screen of the Mini App: saves the language and the currency in one go."""
    if body.lang not in ("uz", "ru"):
        raise HTTPException(400, "bad_lang")
    if body.currency not in CURRENCIES:
        raise HTTPException(400, "bad_currency")
    await db.execute(
        "UPDATE users SET lang = ?, currency = ?, setup_done = 1 WHERE tg_id = ?", (body.lang, body.currency, user["tg_id"])
    )
    return {"ok": True}


# ---------------------------------------------------------------- catalog
@router.get("/games/{game}/packs")
async def packs(game: str, user: dict = Depends(current_user)):
    if game != "pubg":
        raise HTTPException(404, "not_found")
    try:
        items = await pubg_packs(fulfillment.supplier)
    except Exception:
        raise HTTPException(503, "catalog_unavailable")
    return {
        "packs": [],  # only UC is sold for now; Prime, Elite Pass, bundles and WOW are intentionally hidden
        "uc_options": uc_options(items),
    }


class QuoteIn(BaseModel):
    game: str
    uc: int


@router.post("/quote")
async def quote(body: QuoteIn, user: dict = Depends(current_user)):
    """Cheapest exact combination of UC packs for a free-typed amount (no supplier call, uses the cached catalog)."""
    if body.game != "pubg":
        raise HTTPException(404, "not_found")
    _rate_limit(user["tg_id"], limit=60)
    try:
        catalog = await pubg_packs(fulfillment.supplier)
    except Exception:
        raise HTTPException(503, "catalog_unavailable")
    q = quote_uc(catalog, body.uc)
    if q["ok"]:
        q.pop("cost_usd")  # supplier cost is not for customers
    return q


class ValidateIn(BaseModel):
    game: str
    player_id: str


async def _validate(player_id: str) -> tuple[bool, str | None, bool]:
    """(is it a real account, nickname, was it actually checked). Unchecked IDs only happen with a supplier that cannot look them up."""
    pid = player_id.strip()
    if not _ID_RE.fullmatch(pid):
        return False, None, True
    # The supplier's ID check intermittently answers 502, so retry a few times before giving up.
    res = None
    for attempt in range(4):
        try:
            res = await fulfillment.supplier.validate_player(PUBG_VALIDATE_CATEGORY, pid)
            break
        except SupplierError as e:
            if e.status < 500:
                raise HTTPException(503, "validation_unavailable")
        except Exception:
            pass
        await asyncio.sleep(0.5 * (attempt + 1))
    if res is None:
        raise HTTPException(503, "validation_unavailable")
    return bool(res.get("valid")), res.get("player_name"), bool(res.get("verified", True))


@router.post("/validate")
async def validate(body: ValidateIn, user: dict = Depends(current_user)):
    if body.game != "pubg":
        raise HTTPException(404, "not_found")
    _rate_limit(user["tg_id"])
    ok, nick, verified = await _validate(body.player_id)
    return {"valid": ok, "nick": nick if ok else None, "verified": verified}


# ---------------------------------------------------------------- orders
class OrderIn(BaseModel):
    game: str
    offer_id: str | None = None
    custom_uc: int | None = None
    player_id: str
    confirm_id: str | None = None  # the ID typed a second time; required when the supplier cannot look IDs up
    save_id: bool = False
    currency: str = "UZS"


@router.post("/orders")
async def create_order(body: OrderIn, user: dict = Depends(current_user)):
    if body.game != "pubg":
        raise HTTPException(404, "not_found")
    if body.currency not in CURRENCIES:
        raise HTTPException(422, "bad_currency")
    _rate_limit(user["tg_id"], limit=10)
    await _expire_stale()

    pending = await db.fetchone(
        "SELECT COUNT(*) AS n FROM orders WHERE tg_id = ? AND status IN ('awaiting_payment', 'payment_review')",
        (user["tg_id"],),
    )
    if pending["n"] >= 3:
        raise HTTPException(429, "too_many_pending")

    try:
        items = await pubg_packs(fulfillment.supplier)
    except Exception:
        raise HTTPException(503, "catalog_unavailable")
    # Price, contents and totals are always rebuilt server-side; the client only names what it wants.
    if body.custom_uc:
        q = quote_uc(items, body.custom_uc)
        if not q["ok"]:
            raise HTTPException(422, "bad_amount")
        order_items = q["items"]
        title, uc_total, price, cost_usd = f"{q['uc']} UC", q["uc"], price_in(q["cost_usd"], body.currency), q["cost_usd"]
        first_offer = order_items[0]["offer_id"]
    else:
        pack = next((p for p in items if p["offer_id"] == body.offer_id and p["section"] == "uc"), None)
        if not pack:
            raise HTTPException(404, "pack_not_found")
        order_items = [{"offer_id": pack["offer_id"], "name": pack["name"], "uc": pack["uc"], "qty": 1}]
        title, uc_total, price, cost_usd = pack["name"], pack["uc"], price_in(pack["cost_usd"], body.currency), pack["cost_usd"]
        first_offer = pack["offer_id"]

    if not await _wallet_covers(cost_usd):
        raise HTTPException(503, "unavailable")

    pid = body.player_id.strip()
    ok, nick, verified = await _validate(pid)  # the price and the ID are always checked server-side
    if not ok:
        raise HTTPException(422, "invalid_id")
    if not verified and (body.confirm_id or "").strip() != pid:
        raise HTTPException(422, "id_mismatch")

    order_id = uuid.uuid4().hex
    now = db.now()
    await db.execute(
        """INSERT INTO orders (id, tg_id, game, offer_id, offer_name, uc, player_id, nick, amount, currency, cost_usd,
                               status, payment_provider, created_at, updated_at, expires_at, items)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            order_id, user["tg_id"], "pubg", first_offer, title, uc_total, pid, nick,
            price, body.currency, cost_usd, "awaiting_payment", provider_for(body.currency), now, now,
            now + PAYMENT_TTL_MIN * 60, json.dumps(order_items),
        ),
    )
    if body.save_id:
        await db.execute(
            "INSERT INTO saved_ids (tg_id, game, player_id, nick) VALUES (?,?,?,?) "
            "ON CONFLICT (tg_id, game, player_id) DO UPDATE SET nick = excluded.nick",
            (user["tg_id"], "pubg", pid, nick),
        )
    order = await db.get_order(order_id)
    await notify.admin_event(
        "order_created",
        f"🛒 <b>Yangi buyurtma</b> · {db.code(order_id)}\n{title} · {_money(price, body.currency)}\n"
        f"ID: <code>{pid}</code> · {nick or '—'}\nTo'lov: botda kutilmoqda",
        order,
    )
    return _public(order)


@router.get("/orders")
async def list_orders(user: dict = Depends(current_user)):
    await _expire_stale()
    rows = await db.fetchall(
        "SELECT * FROM orders WHERE tg_id = ? ORDER BY created_at DESC LIMIT 50", (user["tg_id"],)
    )
    return {"orders": [_public(o) for o in rows]}


@router.get("/orders/{order_id}")
async def get_order(order_id: str, user: dict = Depends(current_user)):
    return _public(await _own_order(order_id, user))


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, user: dict = Depends(current_user)):
    order = await _own_order(order_id, user)
    if order["status"] != "awaiting_payment":
        raise HTTPException(409, "cannot_cancel")
    await db.update_order(order_id, status="cancelled")
    order = await db.get_order(order_id)
    await notify.admin_event("cancelled", f"🚫 <b>Mijoz buyurtmani bekor qildi</b> · {db.code(order_id)}", order)
    return _public(order)


@router.post("/orders/{order_id}/mock-pay")
async def mock_pay(order_id: str, user: dict = Depends(current_user)):
    order = await _own_order(order_id, user)
    if provider_for(order["currency"]) != "mock":
        raise HTTPException(404, "not_found")
    if order["status"] != "awaiting_payment":
        raise HTTPException(409, "cannot_pay")
    await db.update_order(order_id, status="paid")
    fulfillment.start(order_id)
    return _public(await db.get_order(order_id))


class IncomingIn(BaseModel):
    ref: str
    amount: int
    currency: str = "UZS"
    paid_at: int | None = None
    payer: str = ""
    source: str = "click"


@router.post("/incoming")
async def incoming(body: IncomingIn, x_webhook_secret: str = Header(default="")):
    """The Click link reports money that really arrived on the card; the matching waiting order is confirmed on the spot."""
    if not INCOMING_SECRET:
        raise HTTPException(404, "not_found")
    if not hmac.compare_digest(x_webhook_secret, INCOMING_SECRET):
        raise HTTPException(401, "unauthorized")
    if body.amount <= 0 or body.currency not in CURRENCIES or not body.ref.strip():
        raise HTTPException(422, "bad_payment")
    iid = await payments.record_incoming(body.ref.strip(), body.amount, body.currency, body.paid_at, body.payer, body.source)
    if iid is None:
        return {"ok": True, "duplicate": True}
    return {"ok": True, "result": await payments.match_incoming(iid)}

# ---------------------------------------------------------------- saved ids
@router.delete("/ids/{saved_id}")
async def delete_saved(saved_id: int, user: dict = Depends(current_user)):
    await db.execute("DELETE FROM saved_ids WHERE id = ? AND tg_id = ?", (saved_id, user["tg_id"]))
    return {"ok": True}
