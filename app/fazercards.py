import uuid

import httpx


class FazerCardsError(Exception):
    def __init__(self, status: int, body: dict | str):
        super().__init__(f"FazerCards API error {status}: {body}")
        self.status = status
        self.body = body


class FazerCards:
    def __init__(self, api_key: str, base_url: str = "https://api.fzr.cards/api/v2"):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=30,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = await self._http.request(method, path, **kwargs)
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        if resp.status_code >= 400:
            raise FazerCardsError(resp.status_code, data)
        return data

    async def balance(self) -> dict:
        return await self._request("GET", "/balance")

    async def categories(self, limit: int = 50, cursor: str | None = None) -> dict:
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/topups", params=params)

    async def all_categories(self) -> list[dict]:
        items: list[dict] = []
        cursor = None
        while True:
            page = await self.categories(limit=200, cursor=cursor)
            items.extend(page["items"])
            meta = page.get("meta", {})
            if not meta.get("has_more"):
                return items
            cursor = meta["next_cursor"]

    async def offers(self, category_id: str) -> dict:
        return await self._request("GET", "/topups/offers", params={"category_id": category_id})

    async def validate_player(self, category_id: str, player_id: str) -> dict:
        return await self._request(
            "POST",
            "/topups/validate-id",
            json={"category_id": category_id, "fields": {"player_id": player_id}},
        )

    async def create_order(
        self,
        category_id: str,
        offer_id: str,
        player_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        # Same key on retry returns the original order instead of charging twice.
        key = idempotency_key or str(uuid.uuid4())
        return await self._request(
            "POST",
            "/topups/order",
            headers={"Idempotency-Key": key},
            json={
                "category_id": category_id,
                "offer_id": offer_id,
                "fields": {"player_id": player_id},
            },
        )

    async def get_order(self, order_id: str) -> dict:
        return await self._request("GET", f"/orders/{order_id}")
