"""What every supplier client (FazerCards, WDG Zone) has in common.

The shop talks to whichever supplier is active through one small interface:
    offers(category) -> {"offers": [{"offer_id": "60_uc", "name": "60 UC", "price_usd": "0.8900"}]}
    validate_player(category, player_id) -> {"valid": bool, "player_name": str | None, "verified": bool}
    create_order(category, offer_id, player_id, idempotency_key) -> {"order": {"id": ...}}
    get_order(order_id) -> {"order": {"status": ...}}
    balance() -> {"balance": ..., "currency": ...}
    close()
plus the attributes `name` and `poll_interval` (seconds between status checks).
"""


class SupplierError(Exception):
    def __init__(self, status: int, body):
        super().__init__(f"supplier error {status}: {body}")
        self.status = status
        self.body = body
