"""Each customer's own balance in the shop (so'm).

The rules, enforced here and nowhere else:
- a balance never goes below zero: a purchase takes the money with one conditional UPDATE inside a transaction, together with the order
  row and the ledger row, so either all of it happens or none of it;
- every change leaves a row in wallet_tx, one per order and kind (UNIQUE), so an order can be credited, charged or refunded only once;
- a top-up is an order of the game "topup" (it goes through the same card / receipt / admin-approval path as before) and becomes balance
  only here, when it is approved.
"""
import logging

from . import db

log = logging.getLogger("nexdonate.wallet")


class Insufficient(Exception):
    """The buyer's balance is lower than the price."""

    def __init__(self, balance: int, need: int):
        super().__init__(f"balance {balance} < {need}")
        self.balance, self.need = balance, need

    @property
    def missing(self) -> int:
        return max(0, self.need - self.balance)


def is_topup(order: dict | None) -> bool:
    return bool(order) and order.get("game") == "topup"


async def balance_of(tg_id: int) -> int:
    row = await db.fetchone("SELECT balance FROM users WHERE tg_id = ?", (tg_id,))
    return int((row or {}).get("balance") or 0)


async def total_balances() -> int:
    row = await db.fetchone("SELECT COALESCE(SUM(balance), 0) AS s FROM users")
    return int(row["s"])


async def pay_order(fields: dict) -> tuple[str, int, bool]:
    """Take fields["amount"] from the buyer's balance and store the order as paid, in one transaction.

    Returns (order id, balance after, created). created is False when the same client_key was already used by this buyer: the earlier
    order is returned and nothing is charged again (a network retry, a double tap).
    Raises Insufficient when the balance is too low; nothing has changed then.
    """
    tg_id, amount = fields["tg_id"], int(fields["amount"])
    if amount <= 0:
        raise ValueError("amount must be positive")
    async with db.transaction() as tx:
        if fields.get("client_key"):
            old = await tx.fetchone("SELECT id FROM orders WHERE tg_id = ? AND client_key = ?", (tg_id, fields["client_key"]))
            if old:
                me = await tx.fetchone("SELECT balance FROM users WHERE tg_id = ?", (tg_id,))
                return old["id"], int(me["balance"]), False
        changed = await tx.execute("UPDATE users SET balance = balance - ? WHERE tg_id = ? AND balance >= ?", (amount, tg_id, amount))
        me = await tx.fetchone("SELECT balance FROM users WHERE tg_id = ?", (tg_id,))
        if changed != 1:
            raise Insufficient(int((me or {}).get("balance") or 0), amount)
        cols = list(fields)
        await tx.execute(f"INSERT INTO orders ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(fields[c] for c in cols))
        await tx.execute(
            "INSERT INTO wallet_tx (tg_id, kind, amount, balance_after, order_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
            (tg_id, "purchase", -amount, int(me["balance"]), fields["id"], fields.get("offer_name") or "", db.now()),
        )
    return fields["id"], int(me["balance"]), True


async def credit_topup(order_id: str, note: str = "") -> tuple[dict, int] | None:
    """The money of a top-up order has arrived (approved by the admin, matched on the card, paid by Click): add it to the balance.
    Returns (the order, balance after), or None when this order is not a top-up or was already credited."""
    async with db.transaction() as tx:
        order = await tx.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
        if not is_topup(order) or order["status"] in ("completed", "refunded"):
            return None
        amount = int(order["amount"])
        await tx.execute("UPDATE orders SET status = 'completed', error = NULL, updated_at = ? WHERE id = ?", (db.now(), order_id))
        if await tx.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amount, order["tg_id"])) != 1:
            raise RuntimeError(f"top-up {order_id}: the customer {order['tg_id']} has no row")  # rolls everything back, money is not lost
        me = await tx.fetchone("SELECT balance FROM users WHERE tg_id = ?", (order["tg_id"],))
        await tx.execute(
            "INSERT INTO wallet_tx (tg_id, kind, amount, balance_after, order_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
            (order["tg_id"], "topup", amount, int(me["balance"]), order_id, note, db.now()),
        )
    order["status"] = "completed"
    return order, int(me["balance"])


async def refund_order(order_id: str, note: str = "") -> tuple[dict, int] | None:
    """A purchase that failed goes back to the buyer's balance. Only a failed purchase can be refunded, and only once.
    Returns (the order, balance after), or None."""
    async with db.transaction() as tx:
        order = await tx.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
        if not order or is_topup(order) or order["status"] != "failed":
            return None
        amount = int(order["amount"])
        await tx.execute("UPDATE orders SET status = 'refunded', updated_at = ? WHERE id = ?", (db.now(), order_id))
        if await tx.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amount, order["tg_id"])) != 1:
            raise RuntimeError(f"refund {order_id}: the customer {order['tg_id']} has no row")
        me = await tx.fetchone("SELECT balance FROM users WHERE tg_id = ?", (order["tg_id"],))
        await tx.execute(
            "INSERT INTO wallet_tx (tg_id, kind, amount, balance_after, order_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
            (order["tg_id"], "refund", amount, int(me["balance"]), order_id, note, db.now()),
        )
    order["status"] = "refunded"
    return order, int(me["balance"])


async def history(tg_id: int, limit: int = 30) -> list[dict]:
    """The customer's latest balance movements, newest first, with the order each one belongs to."""
    rows = await db.fetchall(
        """SELECT w.id, w.kind, w.amount, w.balance_after, w.order_id, w.created_at,
                  o.game, o.offer_id, o.offer_name, o.uc, o.section
           FROM wallet_tx w LEFT JOIN orders o ON o.id = w.order_id
           WHERE w.tg_id = ? ORDER BY w.id DESC LIMIT ?""",
        (tg_id, limit),
    )
    for r in rows:
        r["code"] = db.code(r["order_id"]) if r["order_id"] else ""
    return rows
