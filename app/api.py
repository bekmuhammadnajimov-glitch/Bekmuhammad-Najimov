import asyncio
import hmac
import html
import json
import re
import time
import uuid
from collections import defaultdict, deque
from decimal import Decimal

from fastapi.responses import JSONResponse
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from . import click, db, fulfillment, notify, payments, wallet
from .auth import current_user
from .config import (
    CARD_NUMBER,
    CLICK_READY,
    CURRENCIES,
    FULFILLMENT_MODE,
    INCOMING_SECRET,
    PAYMENT_TTL_MIN,
    PUBG_VALIDATE_CATEGORY,
    PAYMENT_PROVIDER,
    TOPUP_MAX,
    TOPUP_MIN,
)
from .games import REGISTRY
from .pricing import game_packs, peek_game_packs, pubg_packs, quote_uc, uc_options, uzs_price
from .supplier import SupplierError

router = APIRouter(prefix="/api")

GAMES = [
    {"id": "pubg", "name": "PUBG Mobile", "currency": "UC", "active": True},
    {"id": "mobile_legends", "name": "Mobile Legends", "currency": "Diamonds", "active": False},
    {"id": "codm", "name": "Call of Duty Mobile", "currency": "CP", "active": False},
    {"id": "genshin", "name": "Genshin Impact", "currency": "Crystals", "active": False},
    {"id": "honor_of_kings", "name": "Honor of Kings", "currency": "Tokens", "active": False},
    {"id": "steam", "name": "Steam", "currency": "", "active": False},
    {"id": "clash_royale", "name": "Clash Royale", "currency": "Gems", "active": False},
    {"id": "clash_of_clans", "name": "Clash of Clans", "currency": "Gems", "active": False},
    {"id": "brawl_stars", "name": "Brawl Stars", "currency": "Gems", "active": False},
    {"id": "efootball", "name": "eFootball", "currency": "Coins", "active": False},
    {"id": "dream_league", "name": "Dream League Soccer", "currency": "Coins", "active": False},
]

_ID_RE = re.compile(r"\d{5,14}")


def _game(game_id: str):
    """A registry game that is on sale right now (it needs a supplier that can list packs), else 404."""
    g = REGISTRY.get(game_id)
    if not g or not g.enabled or not hasattr(fulfillment.supplier, "packs"):
        raise HTTPException(404, "not_found")
    return g


def _games_view(from_price) -> list[dict]:
    out = [
        {**g, "kind": "id", "from_price": from_price if g["id"] == "pubg" else None,
         }
        for g in GAMES
    ]
    for g in REGISTRY.values():
        active = g.enabled and hasattr(fulfillment.supplier, "packs")
        cached = [p for p in peek_game_packs(g, g.default_region) or [] if not p["section"]] if active else None  # the home screen never waits for the supplier
        out.append({
            "id": g.id, "name": g.name, "currency": g.unit, "unit": g.unit, "kind": "code" if g.delivery == "code" else g.recipient,
            "redeem_url": g.redeem_url, "active": active,
            "regions": list(g.regions), "default_region": g.default_region,
            "from_price": min((p["price"] for p in cached), default=None) if cached else None,
        })
    return out
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
    return f"{amount:,} so'm".replace(",", " ")


def _payment_view(order: dict) -> dict:
    provider = PAYMENT_PROVIDER
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
        number = CARD_NUMBER
        username = notify.bot_username
        return {
            "provider": "bot",
            "available": bool(number and username),  # false until the card is set
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


def _delivery_of(order: dict) -> list[str] | None:
    """The codes of a completed code order. _public is only ever built for the order's own customer."""
    if order["status"] != "completed" or not order.get("delivery"):
        return None
    try:
        return json.loads(order["delivery"])
    except ValueError:
        return None


def _public(order: dict) -> dict:
    return {
        "id": order["id"],
        "code": db.code(order["id"]),
        "game": order["game"],
        "uc": order["uc"],
        "offer_id": order["offer_id"],
        "offer_name": order["offer_name"],
        "section": order.get("section") or "",
        "items": _items_of(order),
        "player_id": order["player_id"],
        "nick": order["nick"],
        "amount": order["amount"],
        "currency": order["currency"],
        "status": order["status"],
        "delivery": _delivery_of(order),
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
    row = await db.fetchone(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS spent FROM orders WHERE tg_id = ? AND status = 'completed' AND currency = 'UZS' AND game != 'topup'",
        (tg_id,),
    )
    return {"orders": row["n"], "spent": {"UZS": row["spent"]}}


# ---------------------------------------------------------------- top donors
_LB_DAYS = {"week": 7, "month": 30, "3m": 90, "6m": 182, "year": 365}
_lb: dict = {"at": 0.0, "rows": {}}


def _lb_name(row: dict) -> str:
    """First name and the first letter of the last one: a customer's full name is not for everybody to read."""
    first, last = (row["first_name"] or "").strip(), (row["last_name"] or "").strip()
    return f"{first} {last[0]}." if first and last else first


async def _leaderboard(tg_id: int) -> dict:
    """The ten customers with the most completed purchases in each period (ranked by what the orders cost us, so
    customers compare), shown at our prices. One minute cache, shared by everybody."""
    if time.time() - _lb["at"] > 60:
        now = db.now()
        _lb["rows"] = {
            key: await db.fetchall(
                """SELECT o.tg_id, SUM(CAST(o.cost_usd AS REAL)) AS usd, COUNT(*) AS n,
                          u.first_name, u.last_name, u.photo_url
                   FROM orders o LEFT JOIN users u ON u.tg_id = o.tg_id
                   WHERE o.status = 'completed' AND o.game != 'topup' AND o.created_at >= ?
                   GROUP BY o.tg_id HAVING usd > 0 ORDER BY usd DESC, n DESC, o.tg_id LIMIT 10""",
                (now - days * 86400,),
            )
            for key, days in _LB_DAYS.items()
        }
        _lb["at"] = time.time()
    return {
        key: [
            {
                "name": _lb_name(r), "photo_url": r["photo_url"], "orders": r["n"],
                "price": uzs_price(str(r["usd"])), "me": r["tg_id"] == tg_id,
            }
            for r in rows
        ]
        for key, rows in _lb["rows"].items()
    }


# ---------------------------------------------------------------- bootstrap
@router.get("/bootstrap")
async def bootstrap(user: dict = Depends(current_user)):
    await _expire_stale()
    from_price = None
    try:
        # never let a slow supplier hold up the home screen
        packs = await asyncio.wait_for(pubg_packs(fulfillment.supplier), timeout=2.5)
        uc = [p for p in packs if p["section"] == "uc"]
        from_price = min((p["price"] for p in uc), default=None)
    except Exception:
        pass
    ids = await db.fetchall(
        "SELECT id, game, player_id, nick FROM saved_ids WHERE tg_id = ? ORDER BY id DESC", (user["tg_id"],)
    )
    return {
        "user": {
            "id": user["tg_id"],
            "first_name": user["first_name"],
            "last_name": user["last_name"],
            "username": user["username"],
            "photo_url": user["photo_url"],
            "lang": user["lang"],
            "setup_done": bool(user["setup_done"]),
            "balance": int(user.get("balance") or 0),
        },
        "stats": await _stats(user["tg_id"]),
        "games": _games_view(from_price),
        "saved_ids": ids,
        "leaderboard": await _leaderboard(user["tg_id"]),
        "support_username": await notify.support_username(),
        "bot_url": f"https://t.me/{notify.bot_username}" if notify.bot_username else "",
        "payment_provider": {"UZS": PAYMENT_PROVIDER},
    }


class LangIn(BaseModel):
    lang: str


@router.post("/lang")
async def set_lang(body: LangIn, user: dict = Depends(current_user)):
    if body.lang not in ("uz", "ru"):
        raise HTTPException(400, "bad_lang")
    await db.execute("UPDATE users SET lang = ? WHERE tg_id = ?", (body.lang, user["tg_id"]))
    return {"ok": True}


class SetupIn(BaseModel):
    lang: str


@router.post("/setup")
async def finish_setup(body: SetupIn, user: dict = Depends(current_user)):
    """First-run screen of the Mini App: saves the language (so'm is the only currency)."""
    if body.lang not in ("uz", "ru"):
        raise HTTPException(400, "bad_lang")
    await db.execute("UPDATE users SET lang = ?, currency = 'UZS', setup_done = 1 WHERE tg_id = ?", (body.lang, user["tg_id"]))
    return {"ok": True}


# ---------------------------------------------------------------- catalog
@router.get("/games/{game}/packs")
async def packs(game: str, region: str = "", user: dict = Depends(current_user)):
    if game != "pubg":
        g = _game(game)
        if not g.has_regions:
            region = ""
        elif region not in g.categories:
            raise HTTPException(422, "bad_region")
        try:
            items = await game_packs(fulfillment.supplier, g, region)
        except Exception:
            raise HTTPException(503, "catalog_unavailable")
        return {
            "region": region,
            "packs": [{k: p[k] for k in ("offer_id", "amount", "unit", "name", "section", "label", "price")} for p in items],
        }
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
        g = _game(body.game)
        _rate_limit(user["tg_id"])
        # No supplier can look these recipients up: only the format is checked, the customer types it twice
        return {"valid": g.normalize(body.player_id) is not None, "nick": None, "verified": False}
    _rate_limit(user["tg_id"])
    ok, nick, verified = await _validate(body.player_id)
    return {"valid": ok, "nick": nick if ok else None, "verified": verified}


# ---------------------------------------------------------------- orders
class OrderIn(BaseModel):
    game: str
    offer_id: str | None = None
    custom_uc: int | None = None
    player_id: str = ""
    confirm_id: str | None = None  # the ID typed a second time; required when the supplier cannot look IDs up
    region: str = ""  # the account's game server, for games that have one (Free Fire)
    save_id: bool = False
    client_key: str | None = None  # one per press of "pay": a retry or a double tap returns the same order instead of charging twice


async def _resolve_purchase(body: OrderIn, g) -> dict:
    """What is being bought, rebuilt server-side from the catalog (the client only names it): contents, title, price, supplier cost."""
    section = ""
    if g is not None:
        region = body.region if g.has_regions else ""
        if g.has_regions and region not in g.categories:
            raise HTTPException(422, "bad_region")
        try:
            catalog = await game_packs(fulfillment.supplier, g, region)
        except Exception:
            raise HTTPException(503, "catalog_unavailable")
        pack = next((p for p in catalog if p["offer_id"] == body.offer_id), None)
        if not pack:
            raise HTTPException(404, "pack_not_found")
        section = pack["section"]
        order_items = pack.get("items") or [{"offer_id": pack["offer_id"], "name": pack["name"], "uc": pack["amount"], "qty": 1}]
        title, uc_total, price, cost_usd = pack["name"], pack["amount"], uzs_price(pack["cost_usd"]), pack["cost_usd"]
        first_offer = order_items[0]["offer_id"]
    else:
        try:
            items = await pubg_packs(fulfillment.supplier)
        except Exception:
            raise HTTPException(503, "catalog_unavailable")
        if body.custom_uc:
            q = quote_uc(items, body.custom_uc)
            if not q["ok"]:
                raise HTTPException(422, "bad_amount")
            order_items = q["items"]
            title, uc_total, price, cost_usd = f"{q['uc']} UC", q["uc"], uzs_price(q["cost_usd"]), q["cost_usd"]
            first_offer = order_items[0]["offer_id"]
        else:
            pack = next((p for p in items if p["offer_id"] == body.offer_id and p["section"] == "uc"), None)
            if not pack:
                raise HTTPException(404, "pack_not_found")
            order_items = [{"offer_id": pack["offer_id"], "name": pack["name"], "uc": pack["uc"], "qty": 1}]
            title, uc_total, price, cost_usd = pack["name"], pack["uc"], uzs_price(pack["cost_usd"]), pack["cost_usd"]
            first_offer = pack["offer_id"]
    return {"section": section, "order_items": order_items, "title": title, "uc_total": uc_total, "price": price, "cost_usd": cost_usd, "first_offer": first_offer}


_seen_events: dict[tuple, float] = {}  # (customer, what) -> when the owner was last told, so one customer pressing around does not flood him


async def _tell_owner_once(key: tuple, seconds: int, kind: str, text: str, tg_id: int) -> None:
    now = time.time()
    if len(_seen_events) > 5000:
        _seen_events.clear()
    if now - _seen_events.get(key, 0) < seconds:
        return
    _seen_events[key] = now
    await notify.admin_event(kind, text, tg_id=tg_id)


def _what(g, r: dict) -> str:
    return f"{g.name if g is not None else 'PUBG Mobile'}: <b>{html.escape(r['title'])}</b>"


async def _balance_short_event(user: dict, g, r: dict, balance: int) -> None:
    """Somebody wanted to buy but the balance is not enough: the owner hears who, what, how much is missing."""
    await _tell_owner_once(
        (user["tg_id"], "short", g.id if g else "pubg", r["first_offer"], r["uc_total"]), 600, "balance_short",
        f"💸 <b>Balans yetmadi</b>\n{_what(g, r)}\nNarxi: {_money(r['price'], 'UZS')} · balansi: {_money(balance, 'UZS')} · "
        f"yetmaydi: <b>{_money(r['price'] - balance, 'UZS')}</b>\nMijozga balansni to'ldirish taklif qilindi.",
        user["tg_id"],
    )


async def _supplier_short_event(user: dict, g, r: dict) -> None:
    await _tell_owner_once(
        (user["tg_id"], "supplier", g.id if g else "pubg", r["first_offer"]), 120, "wallet_refused",
        f"🚫 <b>Xarid bajarilmadi: yetkazib beruvchi hamyoni yetarli emas</b>\n{_what(g, r)} · {_money(r['price'], 'UZS')}\n"
        "Mijozning balansidan hech narsa yechilmadi. Unga «admin bilan bog'laning» deb aytildi.",
        user["tg_id"],
    )


def _insufficient(e: "wallet.Insufficient") -> JSONResponse:
    return JSONResponse(status_code=402, content={"detail": "insufficient_balance", "balance": e.balance, "need": e.need, "missing": e.missing})


@router.post("/orders/check")
async def check_order(body: OrderIn, user: dict = Depends(current_user)):
    """The price of what the customer is about to buy against their balance. If the balance is short, the owner is told."""
    g = None if body.game == "pubg" else _game(body.game)
    _rate_limit(user["tg_id"], limit=30)
    r = await _resolve_purchase(body, g)
    balance = await wallet.balance_of(user["tg_id"])
    if balance < r["price"]:
        await _balance_short_event(user, g, r, balance)
    return {"price": r["price"], "balance": balance, "enough": balance >= r["price"], "missing": max(0, r["price"] - balance)}


@router.post("/orders")
async def create_order(body: OrderIn, user: dict = Depends(current_user)):
    """Buy from the balance: the price is taken at once and the delivery starts. No money in the balance, no order."""
    g = None if body.game == "pubg" else _game(body.game)  # None = PUBG UC, which has its own pack logic
    _rate_limit(user["tg_id"], limit=10)
    await _expire_stale()
    r = await _resolve_purchase(body, g)
    price, cost_usd = r["price"], r["cost_usd"]
    try:
        if g is not None:
            pid = g.normalize(body.player_id)
            if pid is None:
                raise HTTPException(422, "invalid_id")
            nick = None
            if not g.same(body.confirm_id or "", pid):
                raise HTTPException(422, "id_mismatch")
            if g.delivery == "code":
                body.save_id = False  # nothing to remember: the code goes to whoever pays
        else:
            pid = body.player_id.strip()
            ok, nick, verified = await _validate(pid)  # the price and the ID are always checked server-side
            if not ok:
                raise HTTPException(422, "invalid_id")
            if not verified and (body.confirm_id or "").strip() != pid:
                raise HTTPException(422, "id_mismatch")

        have = await wallet.balance_of(user["tg_id"])
        if have < price:  # the cheap check first; the real one is made inside the transaction below
            raise wallet.Insufficient(have, price)

        if not await _wallet_covers(cost_usd):  # the supplier could not pay for it: nothing is taken from the customer
            await _supplier_short_event(user, g, r)
            raise HTTPException(503, "unavailable")

        now = db.now()
        fields = {
            "id": uuid.uuid4().hex, "tg_id": user["tg_id"], "game": body.game, "offer_id": r["first_offer"], "offer_name": r["title"],
            "uc": r["uc_total"], "player_id": pid, "nick": nick, "amount": price, "currency": "UZS", "cost_usd": cost_usd,
            "status": "paid", "payment_provider": "balance", "created_at": now, "updated_at": now, "expires_at": now,
            "items": json.dumps(r["order_items"]), "section": r["section"], "client_key": (body.client_key or "")[:64] or None,
        }
        order_id, balance, created = await wallet.pay_order(fields)
    except wallet.Insufficient as e:
        await _balance_short_event(user, g, r, e.balance)
        return _insufficient(e)

    if created:
        if body.save_id:
            await db.execute(
                "INSERT INTO saved_ids (tg_id, game, player_id, nick) VALUES (?,?,?,?) "
                "ON CONFLICT (tg_id, game, player_id) DO UPDATE SET nick = excluded.nick",
                (user["tg_id"], body.game, pid, nick),
            )
        order = await db.get_order(order_id)
        await notify.admin_event(
            "order_created",
            f"🛒 <b>Yangi buyurtma</b> · {db.code(order_id)}\n{r['title']} · {_money(price, 'UZS')}\n"
            + (
                "Kod buyurtmasi (kod yetkazilgach ilovada chiqadi)"
                if g is not None and g.delivery == "code"
                else f"{'Username' if g is not None and g.recipient == 'username' else 'ID'}: <code>{'@' if g is not None and g.recipient == 'username' else ''}{pid}</code> · {nick or '—'}"
            )
            + f"\nBalansdan to'landi · qolgan balans: {_money(balance, 'UZS')}",
            order,
        )
        fulfillment.start(order_id)
    return {**_public(await db.get_order(order_id)), "balance": balance}


# ---------------------------------------------------------------- balance
class TopupIn(BaseModel):
    amount: int


@router.get("/balance")
async def get_balance(user: dict = Depends(current_user)):
    """The customer's balance, what happened to it, and the top-ups that are still waiting for money or for the owner's check."""
    await _expire_stale()
    waiting = await db.fetchall(
        "SELECT * FROM orders WHERE tg_id = ? AND game = 'topup' AND status IN ('awaiting_payment', 'payment_review') ORDER BY created_at DESC, rowid DESC LIMIT 3",
        (user["tg_id"],),
    )
    return {
        "balance": await wallet.balance_of(user["tg_id"]),
        "history": await wallet.history(user["tg_id"], 100),
        "pending": [_public(o) for o in waiting],
        "min": TOPUP_MIN, "max": TOPUP_MAX,
    }


@router.post("/topups")
async def create_topup(body: TopupIn, user: dict = Depends(current_user)):
    """Ask to top up the balance: an order of the game "topup" that is paid by card and checked by the owner, like any payment."""
    if not (TOPUP_MIN <= body.amount <= TOPUP_MAX):
        raise HTTPException(422, "bad_topup")
    _rate_limit(user["tg_id"], limit=10)
    await _expire_stale()
    same = await db.fetchone(
        "SELECT * FROM orders WHERE tg_id = ? AND game = 'topup' AND status = 'awaiting_payment' AND amount = ? ORDER BY created_at DESC LIMIT 1",
        (user["tg_id"], body.amount),
    )
    if same:  # pressed twice: the same request, not a second one
        return _public(same)
    pending = await db.fetchone(
        "SELECT COUNT(*) AS n FROM orders WHERE tg_id = ? AND game = 'topup' AND status IN ('awaiting_payment', 'payment_review')", (user["tg_id"],)
    )
    if pending["n"] >= 3:
        raise HTTPException(429, "too_many_pending")
    order_id = uuid.uuid4().hex
    now = db.now()
    await db.execute(
        """INSERT INTO orders (id, tg_id, game, offer_id, offer_name, uc, player_id, nick, amount, currency, cost_usd,
                               status, payment_provider, created_at, updated_at, expires_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (order_id, user["tg_id"], "topup", "topup", "Balansni to'ldirish", None, "", None, body.amount, "UZS", None,
         "awaiting_payment", PAYMENT_PROVIDER, now, now, now + PAYMENT_TTL_MIN * 60),
    )
    order = await db.get_order(order_id)
    await notify.admin_event(
        "topup_created",
        f"➕ <b>Balansni to'ldirish so'rovi</b> · {db.code(order_id)}\nSumma: <b>{_money(body.amount, 'UZS')}</b>\n"
        "Mijoz botga yo'naltirildi: karta ko'rsatiladi, chek kutilmoqda.",
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
    if PAYMENT_PROVIDER != "mock":
        raise HTTPException(404, "not_found")
    if order["status"] != "awaiting_payment":
        raise HTTPException(409, "cannot_pay")
    await payments.confirm_order(order_id, "demo")
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
