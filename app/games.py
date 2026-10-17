"""Games and services sold as fixed packs from a supplier catalogue.

PUBG Mobile UC is not here: it has its own logic (exact amounts built from several packs, see pricing.py).
Everything below is bought as one supplier product per pack, so the pack's offer id is the supplier's product id.
"""
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal

from .config import DISABLED_GAMES

_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
_ID_RE = re.compile(r"\d{5,20}")


@dataclass(frozen=True)
class Game:
    id: str
    name: str
    unit: str  # the app translates it: "stars", "diamonds", "months"
    recipient: str  # "id": a numeric player ID, "username": a Telegram @username
    categories: dict  # region code -> WDG category id; {"": id} when the game has no regions
    pick: re.Pattern  # matches a product name; group 1 is the amount
    band: tuple  # (low, high): believable USD cost per unit. A price outside it is a placeholder or another product
    label: str  # how the pack is named in orders and in the bot, e.g. "{amount} Stars"
    default_region: str = ""
    enabled: bool = True  # set false by DISABLED_GAMES
    regions: tuple = field(default=())  # order shown in the app

    @property
    def has_regions(self) -> bool:
        return bool(self.regions)

    def title(self, amount: int, region: str = "") -> str:
        return self.label.format(amount=amount, region=region)

    def normalize(self, raw: str) -> str | None:
        """The recipient as it is stored and sent to the supplier, or None when it cannot be one."""
        value = (raw or "").strip()
        if self.recipient == "username":
            value = value.lstrip("@")
            return value if _USERNAME_RE.fullmatch(value) else None
        return value if _ID_RE.fullmatch(value) else None

    def same(self, a: str, b: str) -> bool:
        """Do two entries of the recipient name the same one? Telegram usernames are not case sensitive."""
        x, y = self.normalize(a), self.normalize(b)
        return x is not None and y is not None and (x.lower() == y.lower() if self.recipient == "username" else x == y)


_FF = {  # Free Fire: one WDG category per game server; the account's server decides which one must be paid
    "CIS": 14681, "EU": 14682, "MENA": 14685, "ID": 14683, "TH": 14690, "VN": 14692, "SG": 14689,
    "MY/SG": 14686, "PH": 14687, "PK": 14688, "BD": 14679, "BR": 14680, "LATAM": 14684, "TW": 14691,
}

REGISTRY: dict[str, Game] = {
    g.id: replace(g, enabled=g.id not in DISABLED_GAMES)
    for g in (
        Game(
            id="tg_stars", name="Telegram Stars", unit="stars", recipient="username", categories={"": 9},
            pick=re.compile(r"^(\d+)\s+Telegram Stars$", re.I), band=(Decimal("0.008"), Decimal("0.03")),
            label="{amount} Stars",
        ),
        Game(
            id="tg_premium", name="Telegram Premium", unit="months", recipient="username", categories={"": 9},
            pick=re.compile(r"^Telegram Premium\s+(\d+)\s+Months?$", re.I), band=(Decimal("1.5"), Decimal("6")),
            label="Telegram Premium · {amount} oy",
        ),
        Game(
            id="free_fire", name="Free Fire", unit="diamonds", recipient="id", categories=_FF,
            pick=re.compile(r"^(\d+)\s+Diamonds$", re.I), band=(Decimal("0.003"), Decimal("0.03")),
            label="{amount} Diamonds · {region}", default_region="CIS", regions=tuple(_FF),
        ),
    )
}
