import asyncio
import time
from contextlib import asynccontextmanager

import aiosqlite

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    first_name TEXT, last_name TEXT, username TEXT, photo_url TEXT,
    lang TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS saved_ids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL, game TEXT NOT NULL, player_id TEXT NOT NULL, nick TEXT,
    UNIQUE (tg_id, game, player_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    tg_id INTEGER NOT NULL,
    game TEXT NOT NULL,
    offer_id TEXT NOT NULL, offer_name TEXT, uc INTEGER,
    player_id TEXT NOT NULL, nick TEXT,
    amount INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'UZS', cost_usd TEXT,
    status TEXT NOT NULL,
    payment_provider TEXT, receipt_file TEXT,
    supplier_order_id TEXT, error TEXT,
    created_at INTEGER, updated_at INTEGER, expires_at INTEGER
);
CREATE TABLE IF NOT EXISTS click_tx (
    id INTEGER PRIMARY KEY AUTOINCREMENT,          -- sent to Click as merchant_prepare_id
    click_trans_id INTEGER NOT NULL UNIQUE,
    order_id TEXT NOT NULL,
    amount TEXT NOT NULL,
    status TEXT NOT NULL,                          -- prepared | completed | cancelled
    created_at INTEGER, updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_click_order ON click_tx (order_id);
-- everything that happens, newest last: the admin panel shows it and every entry is also pushed to the admins
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL,
    order_id TEXT, tg_id INTEGER, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
-- money that really arrived on the card (fed by the Click link later); an order is auto-confirmed only against these rows
CREATE TABLE IF NOT EXISTS incoming_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT NOT NULL UNIQUE, amount INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'UZS',
    paid_at INTEGER NOT NULL, payer TEXT, source TEXT, raw TEXT, order_id TEXT, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incoming_open ON incoming_payments (order_id, amount);
CREATE INDEX IF NOT EXISTS idx_click_order2 ON click_tx (order_id);
-- every change of a customer's balance, newest last: top-ups, purchases from the balance, refunds. One row per order and kind, so
-- an order can never be credited, charged or refunded twice.
CREATE TABLE IF NOT EXISTS wallet_tx (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL, kind TEXT NOT NULL,
    amount INTEGER NOT NULL, balance_after INTEGER NOT NULL, order_id TEXT, note TEXT, created_at INTEGER NOT NULL,
    UNIQUE (order_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_wallet_user ON wallet_tx (tg_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders (tg_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
"""

_db: aiosqlite.Connection | None = None
# One connection is shared by everybody. Every statement takes this lock, and money moves inside transaction() which holds it for the
# whole operation, so a half-finished payment can never be committed by somebody else's statement.
_lock = asyncio.Lock()


async def init() -> None:
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    # lightweight migration: orders gained a JSON list of purchased items
    async with _db.execute("PRAGMA table_info(orders)") as cur:
        cols = {r["name"] for r in await cur.fetchall()}
    for col, ddl in (("receipt_at", "INTEGER"), ("pay_opened_at", "INTEGER")):
        if col not in cols:
            await _db.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")
    if "items" not in cols:
        await _db.execute("ALTER TABLE orders ADD COLUMN items TEXT")
    if "delivery" not in cols:  # codes handed to the customer (Roblox...), JSON list
        await _db.execute("ALTER TABLE orders ADD COLUMN delivery TEXT")
    if "section" not in cols:  # which list of the game the pack came from ("" = the main one), see games.Extra
        await _db.execute("ALTER TABLE orders ADD COLUMN section TEXT")
    async with _db.execute("PRAGMA table_info(users)") as cur:
        ucols = {r["name"] for r in await cur.fetchall()}
    if "last_seen" not in ucols:
        await _db.execute("ALTER TABLE users ADD COLUMN last_seen INTEGER")
    # first-run choices: language + payment currency; setup_done = 1 once the user has picked both
    if "currency" not in ucols:
        await _db.execute("ALTER TABLE users ADD COLUMN currency TEXT")
    if "setup_done" not in ucols:
        await _db.execute("ALTER TABLE users ADD COLUMN setup_done INTEGER NOT NULL DEFAULT 0")
    # roubles were removed: everybody pays in so'm, nobody is left waiting at the old currency question,
    # and an unpaid order written in roubles can no longer be paid (its number would be read as so'm)
    await _db.execute("UPDATE users SET currency = 'UZS' WHERE currency IS NOT 'UZS'")
    await _db.execute("UPDATE users SET setup_done = 1 WHERE lang IS NOT NULL AND setup_done = 0")
    await _db.execute("UPDATE orders SET status = 'cancelled', updated_at = ? WHERE currency = 'RUB' AND status = 'awaiting_payment'", (now(),))
    if "balance" not in ucols:  # the customer's own money in the shop, in so'm (see wallet.py)
        await _db.execute("ALTER TABLE users ADD COLUMN balance INTEGER NOT NULL DEFAULT 0")
    if "client_key" not in cols:  # one key per press of "pay": a network retry returns the same order instead of charging twice
        await _db.execute("ALTER TABLE orders ADD COLUMN client_key TEXT")
    await _db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_key ON orders (tg_id, client_key) WHERE client_key IS NOT NULL")
    if "kb_lang" not in ucols:  # the language of the app button under the message field this person was last sent (see botmenu / bot_handlers)
        await _db.execute("ALTER TABLE users ADD COLUMN kb_lang TEXT")
    # amounts can now be in so'm or rubles: `amount` + `currency` replace `amount_uzs`
    if "amount_uzs" in cols:
        await _db.execute("ALTER TABLE orders RENAME COLUMN amount_uzs TO amount")
    if "currency" not in cols:
        await _db.execute("ALTER TABLE orders ADD COLUMN currency TEXT NOT NULL DEFAULT 'UZS'")
    await _db.commit()


async def close() -> None:
    if _db:
        await _db.close()


async def fetchone(sql: str, args: tuple = ()) -> dict | None:
    async with _lock:
        async with _db.execute(sql, args) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def fetchall(sql: str, args: tuple = ()) -> list[dict]:
    async with _lock:
        async with _db.execute(sql, args) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def execute(sql: str, args: tuple = ()) -> int:
    async with _lock:
        async with _db.execute(sql, args) as cur:
            last = cur.lastrowid
        await _db.commit()
    return last


class Tx:
    """The statements of one transaction (see transaction()). Use ONLY these inside the block: db.fetchone / db.execute would wait for the
    lock that the block itself holds."""

    def __init__(self, conn: aiosqlite.Connection):
        self._c = conn

    async def fetchone(self, sql: str, args: tuple = ()) -> dict | None:
        async with self._c.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def execute(self, sql: str, args: tuple = ()) -> int:
        """Returns the number of rows changed."""
        async with self._c.execute(sql, args) as cur:
            return cur.rowcount


@asynccontextmanager
async def transaction():
    """All or nothing. Leaving the block normally commits; an exception rolls everything back."""
    async with _lock:
        await _db.execute("BEGIN IMMEDIATE")
        try:
            yield Tx(_db)
        except BaseException:
            await _db.rollback()
            raise
        else:
            await _db.commit()


def now() -> int:
    return int(time.time())


async def touch_user(tg_user) -> bool:
    """Record anyone who talks to the bot (not only those who open the Mini App) and note when they were last seen."""
    lang = "ru" if (tg_user.language_code or "").startswith("ru") else "uz"
    is_new = (await fetchone("SELECT 1 AS x FROM users WHERE tg_id = ?", (tg_user.id,))) is None
    await execute(
        """INSERT INTO users (tg_id, first_name, last_name, username, lang, created_at, last_seen)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT (tg_id) DO UPDATE SET first_name = excluded.first_name, last_name = excluded.last_name,
                                             username = excluded.username, last_seen = excluded.last_seen""",
        (tg_user.id, tg_user.first_name, tg_user.last_name, tg_user.username, lang, now(), now()),
    )
    return is_new


def code(order_id: str) -> str:
    return "NX-" + order_id[:6].upper()


async def update_order(order_id: str, **fields) -> None:
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    await execute(f"UPDATE orders SET {cols} WHERE id = ?", (*fields.values(), order_id))


async def get_order(order_id: str) -> dict | None:
    return await fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
