"""Click (https://click.uz) SHOP-API: the two callbacks Click sends while a customer pays, plus the payment link.

Flow: the customer opens the link -> Click calls PREPARE (we check the order and the exact amount) ->
the customer confirms in Click -> Click calls COMPLETE (we mark the order paid and start delivery).
Docs: https://docs.click.uz/shop-api/requests  and  https://docs.click.uz/shop-api/errors
"""
import asyncio
import hashlib
import hmac
import logging
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Request

from . import db, fulfillment, notify
from .config import CLICK_MERCHANT_ID, CLICK_MERCHANT_USER_ID, CLICK_READY, CLICK_SECRET_KEY, CLICK_SERVICE_ID, WEBAPP_URL

log = logging.getLogger("nexdonate.click")
router = APIRouter()

PAY_URL = "https://my.click.uz/services/pay/"
PREPARED_TTL = 15 * 60  # an unfinished prepare blocks a second payment for this long

# error codes the merchant returns (docs: Shop API -> Errors)
OK, E_SIGN, E_AMOUNT, E_ACTION, E_PAID, E_ORDER, E_TX, E_UPDATE, E_REQUEST, E_CANCELLED = 0, -1, -2, -3, -4, -5, -6, -7, -8, -9
NOTES = {
    OK: "Success", E_SIGN: "SIGN CHECK FAILED!", E_AMOUNT: "Incorrect parameter amount", E_ACTION: "Action not found",
    E_PAID: "Already paid", E_ORDER: "Order does not exist", E_TX: "Transaction does not exist",
    E_UPDATE: "Failed to update order", E_REQUEST: "Error in request from click", E_CANCELLED: "Transaction cancelled",
}

_lock = asyncio.Lock()  # Click may retry; handle one callback at a time so an order is never paid twice
bot_username: str | None = None  # filled at startup, used for the return link


def payment_url(order: dict) -> str:
    params = {
        "service_id": CLICK_SERVICE_ID,
        "merchant_id": CLICK_MERCHANT_ID,
        "amount": f"{order['amount']:.2f}",
        "transaction_param": order["id"],
    }
    if CLICK_MERCHANT_USER_ID:
        params["merchant_user_id"] = CLICK_MERCHANT_USER_ID
    back = f"https://t.me/{bot_username}" if bot_username else WEBAPP_URL
    if back:
        params["return_url"] = back
    return f"{PAY_URL}?{urlencode(params)}"


def _sign(click_trans_id, service_id, merchant_trans_id, amount, action, sign_time, prepare_id=None) -> str:
    # prepare : click_trans_id + service_id + SECRET + merchant_trans_id + amount + action + sign_time
    # complete: click_trans_id + service_id + SECRET + merchant_trans_id + merchant_prepare_id + amount + action + sign_time
    parts = [click_trans_id, service_id, CLICK_SECRET_KEY, merchant_trans_id]
    if prepare_id is not None:
        parts.append(prepare_id)
    parts += [amount, action, sign_time]
    return hashlib.md5("".join(str(p) for p in parts).encode()).hexdigest()


def _reply(base: dict, code: int, note: str | None = None, **extra) -> dict:
    return {**base, **extra, "error": code, "error_note": note or NOTES.get(code, "")}


@router.post("/api/click")
async def click_callback(request: Request):
    form = {k: str(v) for k, v in (await request.form()).items()}
    base = {"click_trans_id": form.get("click_trans_id"), "merchant_trans_id": form.get("merchant_trans_id")}
    if not CLICK_READY:
        return _reply(base, E_REQUEST, "Click is not configured")
    try:
        click_trans_id = int(form["click_trans_id"])
        service_id = form["service_id"]
        merchant_trans_id = form["merchant_trans_id"]
        amount_raw = form["amount"]
        action = int(form["action"])
        sign_time = form["sign_time"]
        sign_string = form["sign_string"]
        click_error = int(form.get("error", "0"))
        paid_amount = Decimal(amount_raw)
    except (KeyError, ValueError, InvalidOperation):
        return _reply(base, E_REQUEST)

    prepare_raw = form.get("merchant_prepare_id", "") if action == 1 else None
    expected = _sign(form["click_trans_id"], service_id, merchant_trans_id, amount_raw, action, sign_time, prepare_raw)
    if service_id != CLICK_SERVICE_ID or not hmac.compare_digest(expected, sign_string.lower()):
        log.warning("click: bad signature for trans %s", click_trans_id)
        return _reply(base, E_SIGN)
    if action not in (0, 1):
        return _reply(base, E_ACTION)

    async with _lock:
        order = await db.get_order(merchant_trans_id)
        if not order or order["currency"] != "UZS":
            return _reply(base, E_ORDER)
        if paid_amount != Decimal(order["amount"]):
            log.warning("click: amount %s != order %s for %s", paid_amount, order["amount"], order["id"])
            return _reply(base, E_AMOUNT)
        if action == 0:
            return await _prepare(base, order, click_trans_id)
        return await _complete(base, order, click_trans_id, prepare_raw, click_error)


async def _prepare(base: dict, order: dict, click_trans_id: int) -> dict:
    existing = await db.fetchone("SELECT * FROM click_tx WHERE click_trans_id = ?", (click_trans_id,))
    if existing:  # Click retried the same prepare
        if existing["status"] == "completed":
            return _reply(base, E_PAID)
        if existing["status"] == "cancelled":
            return _reply(base, E_CANCELLED)
        return _reply(base, OK, merchant_prepare_id=existing["id"])

    if order["status"] in ("paid", "processing", "completed"):
        return _reply(base, E_PAID)
    if order["status"] != "awaiting_payment" or order["expires_at"] < db.now():
        return _reply(base, E_ORDER, "Order is no longer payable")

    # reserve the order: refuse a second parallel payment until the first one finishes or goes stale
    now = db.now()
    await db.execute(
        "UPDATE click_tx SET status = 'cancelled', updated_at = ? WHERE order_id = ? AND status = 'prepared' AND created_at < ?",
        (now, order["id"], now - PREPARED_TTL),
    )
    busy = await db.fetchone("SELECT id FROM click_tx WHERE order_id = ? AND status = 'prepared'", (order["id"],))
    if busy:
        return _reply(base, E_UPDATE, "Another payment for this order is in progress")

    tx_id = await db.execute(
        "INSERT INTO click_tx (click_trans_id, order_id, amount, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (click_trans_id, order["id"], str(order["amount"]), "prepared", now, now),
    )
    # keep the order alive while the customer confirms in the Click app
    await db.update_order(order["id"], expires_at=max(order["expires_at"], now + 10 * 60))
    return _reply(base, OK, merchant_prepare_id=tx_id)


async def _complete(base: dict, order: dict, click_trans_id: int, prepare_raw: str, click_error: int) -> dict:
    tx = await db.fetchone("SELECT * FROM click_tx WHERE click_trans_id = ?", (click_trans_id,))
    if not tx or tx["order_id"] != order["id"] or str(tx["id"]) != str(prepare_raw):
        return _reply(base, E_TX)
    if tx["status"] == "completed":
        return _reply(base, E_PAID)
    if tx["status"] == "cancelled":
        return _reply(base, E_CANCELLED)

    if click_error < 0:  # Click could not take the money: release the reservation
        await db.execute("UPDATE click_tx SET status = 'cancelled', updated_at = ? WHERE id = ?", (db.now(), tx["id"]))
        return _reply(base, E_CANCELLED)

    if order["status"] in ("paid", "processing", "completed"):
        return _reply(base, E_PAID)

    # The money has been taken: mark the order paid whatever its state was (even if it just expired) and deliver.
    await db.execute("UPDATE click_tx SET status = 'completed', updated_at = ? WHERE id = ?", (db.now(), tx["id"]))
    await db.update_order(order["id"], status="paid", error=None)
    fresh = await db.get_order(order["id"])
    await notify.send_user(fresh, "payment_confirmed")
    await notify.alert_admins(fresh, "💸 <b>Click to'lovi qabul qilindi</b>")
    fulfillment.start(order["id"])
    log.info("click: order %s paid (trans %s)", order["id"], click_trans_id)
    return _reply(base, OK, merchant_confirm_id=tx["id"])
