"""Turns a paid order into delivered UC: places the supplier order and polls it to completion."""
import asyncio
import json
import logging
import time

import httpx

from . import db, notify
from .config import FAZERCARDS_API_KEY, FAZERCARDS_BASE_URL, FULFILLMENT_MODE, PUBG_ORDER_CATEGORY
from .fazercards import FazerCards, FazerCardsError

log = logging.getLogger("nexdonate.fulfillment")

supplier = FazerCards(FAZERCARDS_API_KEY, FAZERCARDS_BASE_URL)

# In mock mode nothing is bought from the supplier: make that impossible to miss in the admin feed.
_MOCK_TAG = "" if FULFILLMENT_MODE == "live" else "\n⚠️ <b>MOCK rejim: UC haqiqatda yuborilmadi</b> (FULFILLMENT_MODE=live qiling)"

# The supplier docs only show processing/completed/created; the failure names are a best guess.
_DONE = {"completed", "complete", "done", "success", "succeeded", "delivered"}
_FAIL = {"failed", "fail", "error", "canceled", "cancelled", "refunded", "rejected"}
_POLL_EVERY = 5
_POLL_LIMIT = 20 * 60

_running: set[str] = set()
_tasks: set[asyncio.Task] = set()


def start(order_id: str) -> None:
    """Fire-and-forget; safe to call twice for the same order."""
    if order_id in _running:
        return
    _running.add(order_id)
    task = asyncio.create_task(_run(order_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def resume() -> None:
    rows = await db.fetchall("SELECT id FROM orders WHERE status IN ('paid', 'processing')")
    for r in rows:
        start(r["id"])


async def _run(order_id: str) -> None:
    try:
        order = await db.get_order(order_id)
        if not order or order["status"] not in ("paid", "processing"):
            return
        await db.update_order(order_id, status="processing", error=None)
        await notify.admin_event("delivery", f"⚙️ <b>UC yuborilmoqda</b> · {db.code(order_id)} · {order['offer_name']} → {order['nick'] or order['player_id']}{_MOCK_TAG}", order)
        if FULFILLMENT_MODE == "live":
            await _live(order)
        else:
            await asyncio.sleep(4)
            await _complete(order, "mock")
    except Exception as e:
        log.exception("fulfillment crashed for %s", order_id)
        order = await db.get_order(order_id)
        if order:
            await _fail(order, f"internal error: {e}")
    finally:
        _running.discard(order_id)


def _units(order: dict) -> list[str]:
    """One supplier order per pack: a custom amount like 385 UC becomes [325_uc, 60_uc]."""
    if order.get("items"):
        try:
            return [it["offer_id"] for it in json.loads(order["items"]) for _ in range(int(it.get("qty", 1)))]
        except (ValueError, KeyError):
            pass
    return [order["offer_id"]]


def _load_state(raw: str | None) -> tuple[list[str], int]:
    """supplier_order_id holds {"ids": [one supplier order per pack], "gen": retries after a supplier-side failure}."""
    if not raw:
        return [], 0
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            return list(d.get("ids", [])), int(d.get("gen", 0))
        except ValueError:
            return [], 0
    if raw.startswith("["):
        try:
            return list(json.loads(raw)), 0
        except ValueError:
            return [], 0
    return [raw], 0  # orders created before multi-pack support stored a single plain id


def _dump_state(ids: list[str], gen: int) -> str:
    return json.dumps({"ids": ids, "gen": gen})


async def _create_unit(order: dict, n: int, offer_id: str, gen: int) -> str | None:
    last_error = ""
    for attempt in range(4):
        try:
            # Stable per-pack idempotency key: a crash/retry can never buy the same pack twice.
            # After the supplier itself failed a pack, `gen` changes the key so the retry is a fresh purchase.
            key = order["id"] if n == 0 else f"{order['id']}-{n}"
            if gen:
                key += f"-g{gen}"
            res = await supplier.create_order(PUBG_ORDER_CATEGORY, offer_id, order["player_id"], idempotency_key=key)
            return res["order"]["id"]
        except FazerCardsError as e:
            last_error = str(e)
            if e.status < 500 and e.status != 429:
                break
        except (httpx.HTTPError, KeyError) as e:
            last_error = f"{type(e).__name__}: {e}"
        await asyncio.sleep(3 * (attempt + 1))
    await _fail(order, f"pack {n + 1}: {last_error}")
    return None


async def _wait_unit(order: dict, n: int, supplier_id: str) -> bool | None:
    """True = delivered, False = failed, None = still unfinished after the poll limit."""
    deadline = time.time() + _POLL_LIMIT
    while time.time() < deadline:
        try:
            data = await supplier.get_order(supplier_id)
            status = str(data.get("order", data).get("status", "")).lower()
        except (FazerCardsError, httpx.HTTPError) as e:
            log.warning("poll error for %s: %s", order["id"], e)
            status = ""
        if status in _DONE:
            return True
        if status in _FAIL:
            log.warning("order %s pack %s ended as %s", order["id"], n + 1, status)
            return False
        await asyncio.sleep(_POLL_EVERY)
    return None


async def _live(order: dict) -> None:
    units = _units(order)
    ids, gen = _load_state(order["supplier_order_id"])
    for n, offer_id in enumerate(units):
        if n >= len(ids):
            sid = await _create_unit(order, n, offer_id, gen)
            if sid is None:
                return  # _create_unit already failed the order
            ids.append(sid)
            await db.update_order(order["id"], supplier_order_id=_dump_state(ids, gen))
        done = await _wait_unit(order, n, ids[n])
        if done is None:
            # Not final after 20 min. It may still be delivered, so do not mark it failed.
            await notify.alert_admins(
                order, "⏳ <b>Buyurtma uzoq kutmoqda</b>", extra=f"supplier order {ids[n]} (pack {n + 1}/{len(units)}): tekshiring"
            )
            return
        if not done:
            # forget the failed pack so an admin retry buys it again under a fresh key
            await db.update_order(order["id"], supplier_order_id=_dump_state(ids[:n], gen + 1))
            return await _fail(order, f"pack {n + 1}/{len(units)} failed at the supplier (already delivered: {n})")
    await _complete(order, None)


async def _complete(order: dict, supplier_id: str | None) -> None:
    fields = {"status": "completed"}
    if supplier_id:
        fields["supplier_order_id"] = supplier_id
    await db.update_order(order["id"], **fields)
    await notify.send_user(order, "completed")
    await notify.admin_event("completed", f"🎉 <b>Buyurtma yetkazildi</b> · {db.code(order['id'])} · {order['offer_name']} → {order['nick'] or order['player_id']}{_MOCK_TAG}", order)


async def _fail(order: dict, reason: str) -> None:
    await db.update_order(order["id"], status="failed", error=reason[:500])
    await notify.send_user(order, "failed")
    await notify.alert_admins(order, "🚨 <b>Buyurtma bajarilmadi</b>", retry_button=True, extra=reason)
