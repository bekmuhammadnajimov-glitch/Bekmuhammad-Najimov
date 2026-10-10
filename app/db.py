import time

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
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders (tg_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
"""

_db: aiosqlite.Connection | None = None


async def init() -> None:
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    # lightweight migration: orders gained a JSON list of purchased items
    async with _db.execute("PRAGMA table_info(orders)") as cur:
        cols = {r["name"] for r in await cur.fetchall()}
    if "items" not in cols:
        await _db.execute("ALTER TABLE orders ADD COLUMN items TEXT")
    async with _db.execute("PRAGMA table_info(users)") as cur:
        ucols = {r["name"] for r in await cur.fetchall()}
    if "last_seen" not in ucols:
        await _db.execute("ALTER TABLE users ADD COLUMN last_seen INTEGER")
    # first-run choices: language + payment currency; setup_done = 1 once the user has picked both
    if "currency" not in ucols:
        await _db.execute("ALTER TABLE users ADD COLUMN currency TEXT")
    if "setup_done" not in ucols:
        await _db.execute("ALTER TABLE users ADD COLUMN setup_done INTEGER NOT NULL DEFAULT 0")
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
    async with _db.execute(sql, args) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def fetchall(sql: str, args: tuple = ()) -> list[dict]:
    async with _db.execute(sql, args) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def execute(sql: str, args: tuple = ()) -> int:
    async with _db.execute(sql, args) as cur:
        last = cur.lastrowid
    await _db.commit()
    return last


def now() -> int:
    return int(time.time())


async def touch_user(tg_user) -> None:
    """Record anyone who talks to the bot (not only those who open the Mini App) and note when they were last seen."""
    lang = "ru" if (tg_user.language_code or "").startswith("ru") else "uz"
    await execute(
        """INSERT INTO users (tg_id, first_name, last_name, username, lang, created_at, last_seen)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT (tg_id) DO UPDATE SET first_name = excluded.first_name, last_name = excluded.last_name,
                                             username = excluded.username, last_seen = excluded.last_seen""",
        (tg_user.id, tg_user.first_name, tg_user.last_name, tg_user.username, lang, now(), now()),
    )


def code(order_id: str) -> str:
    return "NX-" + order_id[:6].upper()


async def update_order(order_id: str, **fields) -> None:
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    await execute(f"UPDATE orders SET {cols} WHERE id = ?", (*fields.values(), order_id))


async def get_order(order_id: str) -> dict | None:
    return await fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
