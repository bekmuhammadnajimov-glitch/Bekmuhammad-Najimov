"""Confirming or rejecting a payment, and matching money that really arrived on the card to waiting orders.

An order is auto-confirmed ONLY against a row in `incoming_payments` (money that actually landed, reported by the
Click link). A screenshot alone is never trusted: it is shown to the admin, who decides.
"""
import logging
import time

from . import db, fulfillment, notify
from .config import AUTO_MATCH_WINDOW_MIN, PAYMENT_TTL_MIN

log = logging.getLogger("nexdonate.payments")
SKEW = 120  # seconds of clock difference we tolerate between the bank/Click and our server


def _fmt(amount: int, currency: str) -> str:
    return f"{amount:,} {'₽' if currency == 'RUB' else 'so' + chr(39) + 'm'}".replace(",", " ")


async def confirm_order(order_id: str, by: str, note: str = "") -> bool:
    """Mark paid and start delivery. Safe to call twice: only the first call wins."""
    order = await db.get_order(order_id)
    if not order or order["status"] not in ("awaiting_payment", "payment_review"):
        return False
    await db.update_order(order_id, status="paid", error=None)
    order = await db.get_order(order_id)
    await notify.send_user(order, "payment_confirmed")
    await notify.admin_event("paid", f"✅ <b>To'lov tasdiqlandi</b> · {db.code(order_id)} · {_fmt(order['amount'], order['currency'])}\nKim: {by}{(' · ' + note) if note else ''}", order)
    fulfillment.start(order_id)
    return True


async def reject_order(order_id: str, by: str) -> bool:
    order = await db.get_order(order_id)
    if not order or order["status"] != "payment_review":
        return False
    # the customer may try again with a correct receipt
    await db.update_order(order_id, status="awaiting_payment", expires_at=db.now() + 15 * 60)
    order = await db.get_order(order_id)
    await notify.send_user(order, "payment_rejected")
    await notify.admin_event("rejected", f"❌ <b>Chek rad etildi</b> · {db.code(order_id)}\nKim: {by}", order)
    return True


async def record_incoming(ref: str, amount: int, currency: str = "UZS", paid_at: int | None = None, payer: str = "", source: str = "click", raw: str = "") -> int | None:
    """Store a real credit. Returns its id, or None if this ref was already recorded (retries are harmless)."""
    existing = await db.fetchone("SELECT id FROM incoming_payments WHERE ref = ?", (ref,))
    if existing:
        return None
    return await db.execute(
        "INSERT INTO incoming_payments (ref, amount, currency, paid_at, payer, source, raw, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (ref, amount, currency, paid_at or db.now(), payer, source, raw[:2000], db.now()),
    )


def _seconds(order: dict, paid_at: int) -> str:
    start = order.get("pay_opened_at") or order["created_at"]
    return f"{max(0, paid_at - start)} soniyada"


async def match_incoming(incoming_id: int) -> str:
    """A credit just arrived: find the one order waiting for exactly this amount and confirm it."""
    pay = await db.fetchone("SELECT * FROM incoming_payments WHERE id = ?", (incoming_id,))
    if not pay or pay["order_id"]:
        return "skipped"
    cands = await db.fetchall(
        """SELECT * FROM orders WHERE status = 'payment_review' AND amount = ? AND currency = ?
           AND created_at <= ? ORDER BY receipt_at""",
        (pay["amount"], pay["currency"], pay["paid_at"] + SKEW),
    )
    where = f"{_fmt(pay['amount'], pay['currency'])} · {pay['payer'] or 'jo' + chr(39) + 'natuvchi noma' + chr(39) + 'lum'}"
    if not cands:
        await notify.admin_event("incoming", f"💰 <b>Kartaga pul tushdi</b>: {where}\nHali mos buyurtma yo'q (chek kutilmoqda).")
        return "no-order"
    if len(cands) > 1:
        await notify.admin_event("incoming", f"⚠️ <b>Pul tushdi, lekin {len(cands)} ta buyurtma bir xil summa kutyapti</b>: {where}\nAvtomatik tasdiqlanmadi, cheklarni o'zingiz tekshiring.")
        return "ambiguous"
    order = cands[0]
    await db.execute("UPDATE incoming_payments SET order_id = ? WHERE id = ? AND order_id IS NULL", (order["id"], incoming_id))
    ok = await confirm_order(order["id"], "avtomatik (kartaga tushgan pul)", f"{where} · {_seconds(order, pay['paid_at'])}")
    return "confirmed" if ok else "skipped"


async def try_auto_confirm(order: dict) -> str:
    """A receipt just arrived: if the matching money is already recorded on the card, confirm right away."""
    now = db.now()
    pays = await db.fetchall(
        """SELECT * FROM incoming_payments WHERE order_id IS NULL AND amount = ? AND currency = ?
           AND paid_at >= ? AND paid_at >= ? ORDER BY paid_at""",
        (order["amount"], order["currency"], order["created_at"] - SKEW, now - AUTO_MATCH_WINDOW_MIN * 60),
    )
    if not pays:
        return "none"
    others = await db.fetchone(
        "SELECT COUNT(*) AS n FROM orders WHERE status = 'payment_review' AND amount = ? AND currency = ? AND id != ?",
        (order["amount"], order["currency"], order["id"]),
    )
    if len(pays) > 1 or others["n"]:
        return "ambiguous"
    pay = pays[0]
    await db.execute("UPDATE incoming_payments SET order_id = ? WHERE id = ? AND order_id IS NULL", (order["id"], pay["id"]))
    ok = await confirm_order(order["id"], "avtomatik (kartaga tushgan pul)", f"{_fmt(pay['amount'], pay['currency'])} · {_seconds(order, pay['paid_at'])}")
    return "confirmed" if ok else "none"