"""WDG Zone as the UC supplier (docs: https://wdgzone.tech/docs/, base https://wdgzone.tech/api/v1).

Speaks the small interface in supplier.py, so pricing, orders and fulfillment do not care which supplier is active.
WDG identifies products by numeric id, so the adapter turns them into the same "<uc>_uc" offer ids FazerCards uses
and keeps the id mapping itself. WDG has no Player-ID lookup: an optional `id_checker` (a FazerCards client) can
still verify IDs; without one the ID is accepted as "unverified" and the app warns the customer.
"""
import asyncio
import html
import json
import logging
import re
import socket
import time
import uuid
from urllib.parse import urlparse
from decimal import Decimal, InvalidOperation

import httpx

from .supplier import SupplierError

log = logging.getLogger("nexdonate.wdg")

_UC_RE = re.compile(r"(\d[\d.,]*)\s*UC\b", re.I)
# One UC costs about $0.0146 wholesale. A price far outside this band is a placeholder (their public catalogue lists
# some entries at $999) or a different product, and must never reach customers.
_MIN_PER_UC, _MAX_PER_UC = Decimal("0.005"), Decimal("0.05")
_PAGE = 100


class WdgError(SupplierError):
    pass


class _Gate:
    """Keeps us under WDG's per-key rate limit (60 requests/min) by spacing requests out."""

    def __init__(self, per_minute: float):
        self._gap = 60.0 / per_minute
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = time.monotonic() + self._gap


class Wdg:
    name = "WDG Zone"
    poll_interval = 8  # 60 req/min is shared by every open order, so poll slower than FazerCards

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://wdgzone.tech/api/v1",
        pubg_category_id: str = "",
        id_checker=None,
        *,
        per_minute: float = 50,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(30, connect=8),  # a supplier that is down should fail fast, not hold every request for 30 s
            transport=transport,
        )
        self._category = str(pubg_category_id or "").strip()
        self._checker = id_checker
        self._checker_off_until = 0.0
        self._gate = _Gate(per_minute)
        self._pid: dict[int, int | str] = {}  # UC amount -> WDG product id, refreshed with every offers() call

    async def close(self) -> None:
        await self._http.aclose()
        if self._checker is not None:
            await self._checker.close()

    # ------------------------------------------------------------------ transport
    async def _request(self, method: str, path: str, *, params=None, json=None, headers=None) -> dict:
        await self._gate.wait()
        resp = await self._http.request(method, path, params=params, json=json, headers=headers)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code >= 400 or (isinstance(body, dict) and body.get("success") is False):
            raise WdgError(resp.status_code if resp.status_code >= 400 else 422, body)
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _data(body: dict):
        return body.get("data") if isinstance(body, dict) else None

    @classmethod
    def _rows(cls, body: dict, *keys: str) -> list[dict]:
        """WDG wraps lists as {"data": [...]} or {"data": {"<key>": [...]}}; accept both."""
        data = cls._data(body)
        if isinstance(data, dict):
            for k in keys:
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    # ------------------------------------------------------------------ interface
    async def balance(self) -> dict:
        d = self._data(await self._request("GET", "/me")) or {}
        return {"balance": str(d.get("balance")), "currency": d.get("currency", "USD")}

    async def _products(self, category_id: str) -> list[dict]:
        out: list[dict] = []
        for page in range(1, 21):
            body = await self._request(
                "GET", "/products", params={"category_id": category_id, "in_stock": 1, "limit": _PAGE, "page": page}
            )
            rows = self._rows(body, "items", "products")
            out.extend(r for r in rows if "id" in r)
            if len(rows) < _PAGE:
                break
        if not out:  # the docs also show products being listed through the category tree
            try:
                body = await self._request("GET", f"/catalog/topup/categories/{category_id}/products", params={"limit": _PAGE})
                out = [r for r in self._rows(body, "items", "products") if "id" in r]
            except WdgError as e:
                if e.status != 404:
                    raise
        return out

    @staticmethod
    def _uc_product(p: dict) -> tuple[int, Decimal] | None:
        """(UC amount, cost in USD) when this product is a plain PUBG UC top-up we can sell, else None."""
        if p.get("available") is False or p.get("stock") == 0 or p.get("requires_player_id") is False:
            return None  # sold out, or a voucher code rather than a top-up by Player ID
        name = str(p.get("name", ""))
        amounts = {int(re.sub(r"[.,]", "", m)) for m in _UC_RE.findall(name)}
        if not amounts:
            return None  # Prime, Elite Pass, WOW Coins...: not UC, and the category lists plenty of them
        if len(amounts) != 1:
            log.warning("skipping product %r: more than one UC amount in the name", name)
            return None
        uc = amounts.pop()
        try:
            cost = Decimal(str(p["price"]))
        except (KeyError, InvalidOperation):
            log.warning("skipping product %r: no usable price", name)
            return None
        if uc <= 0 or not (_MIN_PER_UC <= cost / uc <= _MAX_PER_UC):
            log.warning("skipping product %r: $%s for %s UC looks wrong", name, cost, uc)
            return None
        return uc, cost

    @classmethod
    def _best_by_uc(cls, products: list[dict]) -> dict[int, tuple[Decimal, int | str]]:
        """UC amount -> (cheapest cost, product id) over the products we can actually sell."""
        best: dict[int, tuple[Decimal, int | str]] = {}
        for p in products:
            parsed = cls._uc_product(p)
            if parsed and (parsed[0] not in best or parsed[1] < best[parsed[0]][0]):
                best[parsed[0]] = (parsed[1], p["id"])
        return best

    async def offers(self, category_id: str = "") -> dict:
        if not self._category:
            raise WdgError(0, "WDG_PUBG_CATEGORY_ID is not set (the /supplier admin command lists the PUBG categories)")
        best = self._best_by_uc(await self._products(self._category))
        if not best:
            raise WdgError(404, f"no purchasable UC products in WDG category {self._category}")
        self._pid = {uc: pid for uc, (_, pid) in best.items()}
        return {
            "ok": True,
            "offers": [
                {"offer_id": f"{uc}_uc", "name": f"{uc} UC", "price_usd": str(cost.quantize(Decimal("0.0001")))}
                for uc, (cost, _) in sorted(best.items())
            ],
        }

    @staticmethod
    def _pack_of(game, p: dict) -> tuple[int, Decimal] | None:
        """(amount, cost in USD) when product `p` is one of `game`'s packs we can sell, else None."""
        stock = p.get("stock")
        if p.get("available") is False or (isinstance(stock, (int, float)) and stock < max(game.min_stock, 1)):
            return None
        needs_id = p.get("requires_player_id")
        if game.delivery == "code":
            if needs_id:  # a top-up by Player ID, not a code
                return None
        elif needs_id is False:  # a voucher code, not a top-up
            return None
        m = game.pick.match(str(p.get("name", "")).strip())
        if not m:
            return None  # weekly cards, bundles, passes...: other products in the same category
        amount = int(m.group(1))
        if game.allow and amount not in game.allow:
            return None
        try:
            cost = Decimal(str(p["price"]))
        except (KeyError, InvalidOperation):
            return None
        low, high = game.band
        if amount <= 0 or not (low <= cost / amount <= high):
            log.warning("skipping %s product %r: $%s for %s looks wrong", game.id, p.get("name"), cost, amount)
            return None
        return amount, cost

    async def packs(self, game, region: str = "") -> list[dict]:
        """The packs `game` sells in `region`: [{"offer_id": WDG product id, "amount", "cost_usd"}], cheapest product per amount."""
        category = game.categories.get(region)
        if category is None:
            raise WdgError(404, f"{game.id} has no region {region!r}")
        best: dict[int, tuple[Decimal, int | str]] = {}
        for cid in category if isinstance(category, (tuple, list)) else (category,):  # several bundles are merged
            for p in await self._products(str(cid)):
                parsed = self._pack_of(game, p)
                if parsed and (parsed[0] not in best or parsed[1] < best[parsed[0]][0]):
                    best[parsed[0]] = (parsed[1], p["id"])
        if not best:
            raise WdgError(404, f"no purchasable {game.id} packs in WDG category {category}")
        return [
            {"offer_id": str(pid), "amount": amount, "cost_usd": str(cost.quantize(Decimal("0.0001")))}
            for amount, (cost, pid) in sorted(best.items())
        ]

    async def validate_player(self, category_id: str, player_id: str) -> dict:
        if self._checker is not None and time.time() >= self._checker_off_until:
            for attempt in range(3):
                try:
                    res = await self._checker.validate_player(category_id, player_id)
                    return {"valid": bool(res.get("valid")), "player_name": res.get("player_name"), "verified": True}
                except SupplierError as e:
                    if e.status >= 500:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    if e.status in (401, 402, 403):  # e.g. the FazerCards subscription lapsed: stop asking for a while
                        self._checker_off_until = time.time() + 600
                    log.warning("ID check unavailable (%s): %s", e.status, e.body)
                    break
                except httpx.HTTPError as e:
                    log.warning("ID check failed: %s", e)
                    await asyncio.sleep(0.5 * (attempt + 1))
        return {"valid": True, "player_name": None, "verified": False}

    async def _product_for(self, uc: int) -> int | str:
        if uc not in self._pid:
            await self.offers()
        if uc not in self._pid:
            raise WdgError(404, f"no WDG product for {uc} UC")
        return self._pid[uc]

    async def create_order(
        self, category_id: str, offer_id: str, player_id: str, idempotency_key: str | None = None
    ) -> dict:
        if str(offer_id).isdigit():  # a WDG product id: every pack except PUBG UC, which is mapped from "<uc>_uc"
            product_id = int(offer_id)
        else:
            m = re.fullmatch(r"(\d+)_uc", str(offer_id))
            if not m:
                raise WdgError(404, f"unsupported offer {offer_id}")
            product_id = await self._product_for(int(m.group(1)))
        key = (idempotency_key or uuid.uuid4().hex)[:100]  # the same key on a retry returns the original order
        body = await self._request(
            "POST",
            f"/products/{product_id}/purchase",
            headers={"X-Idempotency-Key": key},
            json={"quantity": 1, **({"player_id": str(player_id)} if player_id else {})},  # a voucher has nobody to credit
        )
        data = self._data(body) or {}
        if data.get("order_id") is None:
            raise WdgError(502, body)
        log.info("WDG order %s placed for product %s (total %s)", data["order_id"], product_id, data.get("total"))
        return {"order": {"id": str(data["order_id"]), "status": str(data.get("status", "")).lower(), "items": self._codes(data)}}

    @staticmethod
    def _codes(data) -> list[str]:
        """The codes inside a purchase / delivery payload: a list of strings, or of objects holding a code, pin or key."""
        items: list = []
        if isinstance(data, dict):
            for k in ("delivery_items", "items", "codes", "delivery"):
                if isinstance(data.get(k), list):
                    items = data[k]
                    break
        elif isinstance(data, list):
            items = data
        out: list[str] = []
        for it in items:
            if isinstance(it, str):
                out.append(it.strip())
            elif isinstance(it, dict):
                for k in ("code", "pin", "key", "serial", "value", "card_number"):
                    if it.get(k):
                        out.append(str(it[k]).strip())
                        break
                else:
                    out.append(" · ".join(str(v) for v in it.values() if isinstance(v, (str, int)))[:200])
        return [c for c in out if c]

    async def delivery(self, order_id: str) -> list[str]:
        """The codes of a finished order (idempotent: asking twice returns the same codes). Empty while it is not ready."""
        return self._codes(self._data(await self._request("GET", f"/orders/{order_id}/delivery")))

    async def get_order(self, order_id: str) -> dict:
        data = self._data(await self._request("GET", f"/orders/{order_id}")) or {}
        return {"order": {"status": str(data.get("status", "")).lower(), "raw": data}}

    # ------------------------------------------------------------------ diagnostics (/supplier)
    async def net_check(self) -> str:
        """Why can we not reach WDG? DNS answers and a plain TCP connect to port 443 on each address (IPv4 and IPv6)."""
        host = urlparse(str(self._http.base_url)).hostname
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except Exception as e:
            return f"DNS {host}: {type(e).__name__} {e}"
        parts = []
        for ip in dict.fromkeys(i[4][0] for i in infos):
            t0 = time.monotonic()
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(ip, 443), 6)
                w.close()
                parts.append(f"{ip} ok {int((time.monotonic() - t0) * 1000)}ms")
            except asyncio.TimeoutError:
                parts.append(f"{ip} timeout")
            except Exception as e:
                parts.append(f"{ip} {type(e).__name__}")
        return f"{host} -> " + " | ".join(parts)

    async def _categories(self, parent_id=None, ctype: str = "topup") -> list[dict]:
        params = {"parent_id": parent_id} if parent_id is not None else None
        return self._rows(await self._request("GET", f"/catalog/{ctype}/categories", params=params), "categories", "items")

    async def discover(self, words: list[str]) -> list[str]:
        """Searches the whole catalogue (top up, voucher, manual) for categories whose name contains one of `words`,
        walks below them and summarises the products of every leaf. Used to find where a game or service lives."""
        words = [w.strip() for w in words if w.strip()]
        if not words:
            return ["Qidiruv so'zi berilmagan"]
        rx = re.compile("|".join(re.escape(w) for w in words), re.I)
        out: list[str] = [f"🔎 Katalog qidiruvi: {html.escape(', '.join(words))}"]
        for ctype in ("topup", "voucher", "manual"):
            try:
                roots = await self._categories(None, ctype)
            except Exception as e:
                out.append(f"⚠️ [{ctype}] o'qib bo'lmadi: {html.escape(str(e))[:160]}")
                continue
            hits = [c for c in roots if rx.search(str(c.get("name", "")))]
            out.append(f"[{ctype}] {len(roots)} ta bosh kategoriya, mos kelgani: {len(hits)}")
            path: dict = {c.get("id"): str(c.get("name")) for c in hits}
            queue, leaves, calls = list(hits), [], 0
            while queue:
                c = queue.pop(0)
                if c.get("has_children") and calls < 40:
                    calls += 1
                    try:
                        kids = await self._categories(c.get("id"), ctype)
                    except Exception:
                        kids = []
                    for k in kids:
                        path[k.get("id")] = f"{path.get(c.get('id'), '?')} › {k.get('name')}"
                    queue += kids
                else:
                    leaves.append(c)
            for c in leaves[:45]:
                cid = c.get("id")
                try:
                    rows = await self._products(str(cid)) if ctype == "topup" else await self._type_products(str(cid), ctype)
                except Exception as e:
                    out.append(f"• {ctype} {cid} {html.escape(path.get(cid, ''))}: ⚠️ {html.escape(str(e))[:120]}")
                    continue
                needs = ",".join(sorted({str(p.get("requires_player_id")) for p in rows})) or "-"
                sample = " | ".join(f"{html.escape(str(p.get('name')))[:34]} ${p.get('price')}" for p in rows[:8])
                out.append(f"• {ctype} <code>{cid}</code> {html.escape(path.get(cid, ''))}: {len(rows)} mahsulot, ID={needs}\n   {sample}")
        return out

    async def dump(self, category_ids: list[str]) -> list[str]:
        """Every product of the given categories with the flags that matter (price, Player ID needed, stock, type)."""
        out: list[str] = []
        for cid in category_ids:
            try:
                rows = await self._products(str(cid).strip())
            except Exception as e:
                out.append(f"== category {cid}: {html.escape(str(e))[:160]}")
                continue
            out.append(f"== category {cid}: {len(rows)} products")
            if rows:  # one product as the supplier sends it, to see every field it carries
                out.append("   raw: " + html.escape(json.dumps(rows[0], ensure_ascii=False))[:700])
            for p in rows[:80]:
                out.append(
                    f"   {p.get('id')} | {html.escape(str(p.get('name')))[:60]} | ${p.get('price')} | type={p.get('type')} "
                    f"| needs_id={p.get('requires_player_id')} | stock={p.get('stock')} | available={p.get('available')}"
                )
        return out

    async def _type_products(self, category_id: str, ctype: str) -> list[dict]:
        body = await self._request(
            "GET", "/products", params={"category_id": category_id, "type": ctype, "limit": _PAGE, "page": 1}
        )
        return [r for r in self._rows(body, "items", "products") if "id" in r]

    async def _summary(self, category_id) -> list[str]:
        rows = await self._products(str(category_id))
        good = sorted(self._best_by_uc(rows).items())
        out = [f"Kategoriya <code>{category_id}</code>: {len(rows)} mahsulot, sotiladigan UC: <b>{len(good)}</b>"]
        out += [f"• {uc} UC · ${cost:.3f}" for uc, (cost, _) in good[:12]]
        readable = {p["id"] for p in rows if self._uc_product(p)}
        odd = [p for p in rows if p["id"] not in readable][:6]
        if odd:
            out.append(f"O'tkazib yuborilganlar ({len(rows) - len(good)} ta: tugagan, kod/voucher, takroriy yoki narxi shubhali), namunalar:")
            out += [f"· {html.escape(str(p.get('name')))[:60]} · ${p.get('price')}" for p in odd]
        return out

    async def _compact(self, category_id) -> list[str]:
        rows = await self._products(str(category_id))
        good = sorted(self._best_by_uc(rows).items())
        needs_id = ",".join(sorted({str(p.get("requires_player_id")) for p in rows})) or "-"
        prices = " · ".join(f"{uc}=${cost:.3f}" for uc, (cost, _) in good) or "UC yo'q"
        return [f"• <code>{category_id}</code>: {len(rows)} mahsulot, ID kerak={needs_id} · {prices}"]

    async def probe(self, category: str | None = None) -> list[str]:
        """Human-readable health report: can we reach WDG, what is the balance, where is PUBG, do its products parse.

        `category` looks inside one category without switching the shop to it; with none given, the configured category
        is reported, or (when nothing is configured yet) every PUBG candidate found.
        """
        out: list[str] = []
        try:
            d = self._data(await self._request("GET", "/me")) or {}
            out.append(f"✅ WDG ulandi: {html.escape(str(d.get('name')))} · balans <b>{d.get('balance')} {d.get('currency')}</b>")
        except Exception as e:
            return [
                f"❌ WDG'ga ulanib bo'lmadi: {html.escape(type(e).__name__)}: {html.escape(str(e))[:250]}",
                f"tarmoq: {html.escape(await self.net_check())}",
            ]
        hits: list[dict] = []
        try:
            hits = [c for c in await self._categories() if re.search(r"pubg", str(c.get("name", "")), re.I)]
            for c in list(hits):
                if c.get("has_children"):
                    hits += [s for s in await self._categories(c.get("id")) if s not in hits]
            out.append("PUBG kategoriyalari (topup):")
            out += [
                f"• id <code>{c.get('id')}</code> · {html.escape(str(c.get('name')))} · {c.get('in_stock_products', '?')}/{c.get('product_count', '?')} mahsulot"
                for c in hits[:12]
            ] or ["• topilmadi"]
        except Exception as e:
            out.append(f"⚠️ kategoriyalarni o'qib bo'lmadi: {html.escape(str(e))[:200]}")
        if category or self._category:
            targets, compact = [category or self._category], False
        else:  # nothing chosen yet: one line per candidate so every PUBG category can be compared
            targets, compact = [c.get("id") for c in hits if not c.get("has_children")][:12], True
        for cid in targets:
            try:
                out += await (self._compact(cid) if compact else self._summary(cid))
            except Exception as e:
                out.append(f"⚠️ kategoriya {cid} mahsulotlarini o'qib bo'lmadi: {html.escape(str(e))[:200]}")
        if not self._category:
            out.append("⚠️ WDG_PUBG_CATEGORY_ID o'rnatilmagan: do'kon yopiq. Kerakli id'ni Railway'ga qo'ying.")
        return out
