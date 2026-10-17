import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", encoding="utf-8-sig")  # tolerate a BOM from Windows editors


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes")


def _ids(name: str) -> list[int]:
    return [int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()]


BOT_TOKEN = "" if _bool("DISABLE_BOT") else os.getenv("BOT_TOKEN", "").strip()  # DISABLE_BOT=1 for test instances
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")  # normally read from Telegram at startup
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
if not WEBAPP_URL and os.getenv("RAILWAY_PUBLIC_DOMAIN"):  # Railway's generated https domain
    WEBAPP_URL = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"].strip()
ADMIN_IDS = _ids("ADMIN_IDS")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")

FAZERCARDS_API_KEY = os.getenv("FAZERCARDS_API_KEY", "").strip()
FAZERCARDS_BASE_URL = os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")
FULFILLMENT_MODE = os.getenv("FULFILLMENT_MODE", "mock").strip().lower()  # mock | live

# Which supplier sells us the UC: fazercards | wdg (WDG Zone, https://wdgzone.tech/docs/)
SUPPLIER = os.getenv("SUPPLIER", "fazercards").strip().lower()
if SUPPLIER not in ("fazercards", "wdg"):
    raise RuntimeError(f"SUPPLIER must be fazercards or wdg, not {SUPPLIER!r}")
WDG_API_KEY = os.getenv("WDG_API_KEY", "").strip()
WDG_BASE_URL = os.getenv("WDG_BASE_URL", "https://wdgzone.tech/api/v1").strip().rstrip("/")
WDG_DISCOVER = [w for w in os.getenv("WDG_DISCOVER", "").split(",") if w.strip()]  # catalogue search words, reported at startup
WDG_PUBG_CATEGORY_ID = os.getenv("WDG_PUBG_CATEGORY_ID", "").strip()  # the WDG category holding PUBG Mobile UC by Player ID

PUBG_ORDER_CATEGORY = "pubg_mobile_auto"
PUBG_VALIDATE_CATEGORY = "pubg_mobile"

USD_UZS_RATE = float(os.getenv("USD_UZS_RATE", "12800"))
USD_RUB_RATE = float(os.getenv("USD_RUB_RATE", "85.5"))
MARKUP_PERCENT = float(os.getenv("MARKUP_PERCENT", "12"))
ROUND_TO = int(os.getenv("ROUND_TO", "1000"))  # so'm
RUB_ROUND_TO = int(os.getenv("RUB_ROUND_TO", "1"))  # rubles
CURRENCIES = ("UZS", "RUB")

PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "mock").strip().lower()  # mock | manual_card | click (so'm payments)

# Click merchant account (issued by Click when the shop is connected): https://docs.click.uz/shop-api
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID", "").strip()
CLICK_MERCHANT_ID = os.getenv("CLICK_MERCHANT_ID", "").strip()
CLICK_MERCHANT_USER_ID = os.getenv("CLICK_MERCHANT_USER_ID", "").strip()
CLICK_SECRET_KEY = os.getenv("CLICK_SECRET_KEY", "").strip()
CLICK_READY = bool(CLICK_SERVICE_ID and CLICK_MERCHANT_ID and CLICK_SECRET_KEY)


def provider_for(currency: str) -> str:
    """Click only handles so'm. Rubles are paid by card transfer (or the demo button in mock mode)."""
    if currency == "RUB":
        return "mock" if PAYMENT_PROVIDER == "mock" else "manual_card"
    return PAYMENT_PROVIDER
CARD_NUMBER = os.getenv("CARD_NUMBER", "").strip()  # so'm card
CARD_HOLDER = os.getenv("CARD_HOLDER", "").strip()
CARD_BANK = os.getenv("CARD_BANK", "").strip()  # e.g. "Humo · Hamkorbank", shown under the card number
CARD_RUB_NUMBER = os.getenv("CARD_RUB_NUMBER", "").strip()  # ruble card
CARD_RUB_HOLDER = os.getenv("CARD_RUB_HOLDER", "").strip()
CARD_RUB_BANK = os.getenv("CARD_RUB_BANK", "").strip()
PAYMENT_TTL_MIN = 30
# Secret that lets the Click link report money that arrived on the card (POST /api/incoming). Empty = endpoint off.
INCOMING_SECRET = os.getenv("INCOMING_SECRET", "").strip()
AUTO_MATCH_WINDOW_MIN = int(os.getenv("AUTO_MATCH_WINDOW_MIN", "45"))

DEV_MODE = _bool("DEV_MODE")
DEV_USER_ID = int(os.getenv("DEV_USER_ID", "1"))

# Where the SQLite database and uploaded receipts live. On Railway attach a Volume: its mount path is
# exposed as RAILWAY_VOLUME_MOUNT_PATH, so data survives redeploys without any extra setting.
DATA_DIR = Path(os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or BASE_DIR / "data")
RECEIPTS_DIR = DATA_DIR / "receipts"
DB_PATH = Path(os.getenv("DB_PATH") or DATA_DIR / "nexdonate.db")
WEBAPP_DIR = BASE_DIR / "webapp"

DATA_DIR.mkdir(parents=True, exist_ok=True)
RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
