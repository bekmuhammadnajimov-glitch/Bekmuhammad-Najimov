"""WDG Zone as the UC supplier (docs: https://wdgzone.tech/docs/, base https://wdgzone.tech/api/v1).

Speaks the small interface in supplier.py, so pricing, orders and fulfillment do not care which supplier is active.
WDG identifies products by numeric id, so the adapter turns them into the same "<uc>_uc" offer ids FazerCards uses
and keeps the id mapping itself. WDG has no Player-ID lookup: an optional `id_checker` (a FazerCards client) can
still verify IDs; without one the ID is accepted as "unverified" and the app warns the customer.
"""
import asyncio
import html
import logging
import re
import time
import uuid
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
            timeout=30,
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
        if len(amounts) != 1:
            log.warning("skipping product %r: cannot read one UC amount from the name", name)
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
        m = re.fullmatch(r"(\d+)_uc", offer_id)
        if not m:
            raise WdgError(404, f"unsupported offer {offer_id}")
        product_id = await self._product_for(int(m.group(1)))
        key = (idempotency_key or uuid.uuid4().hex)[:100]  # the same key on a retry returns the original order
        body = await self._request(
            "POST",
            f"/products/{product_id}/purchase",
            headers={"X-Idempotency-Key": key},
            json={"quantity": 1, "player_id": str(player_id)},
        )
        data = self._data(body) or {}
        if data.get("order_id") is None:
            raise WdgError(502, body)
        log.info("WDG order %s placed for product %s (total %s)", data["order_id"], product_id, data.get("total"))
        return {"order": {"id": str(data["order_id"]), "status": str(data.get("status", "")).lower()}}

    async def get_order(self, order_id: str) -> dict:
        data = self._data(await self._request("GET", f"/orders/{order_id}")) or {}
        return {"order": {"status": str(data.get("status", "")).lower(), "raw": data}}

    # ------------------------------------------------------------------ diagnostics (/supplier)
    async def _categories(self, parent_id=None) -> list[dict]:
        params = {"parent_id": parent_id} if parent_id is not None else None
        return self._rows(await self._request("GET", "/catalog/topup/categories", params=params), "categories", "items")

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
            return [f"❌ WDG'ga ulanib bo'lmadi: {html.escape(type(e).__name__)}: {html.escape(str(e))[:250]}"]
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
        targets = [category] if category else [self._category] if self._category else [c.get("id") for c in hits if not c.get("has_children")][:4]
        for cid in targets:
            try:
                out += await self._summary(cid)
            except Exception as e:
                out.append(f"⚠️ kategoriya {cid} mahsulotlarini o'qib bo'lmadi: {html.escape(str(e))[:200]}")
        if not self._category:
            out.append("⚠️ WDG_PUBG_CATEGORY_ID o'rnatilmagan: do'kon yopiq. Kerakli id'ni Railway'ga qo'ying.")
        return out
