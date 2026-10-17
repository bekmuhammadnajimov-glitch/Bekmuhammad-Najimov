import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager

from aiogram.types import BotCommand, BotCommandScopeChat, MenuButtonWebApp, WebAppInfo
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api, click, db, fulfillment, notify
from .bot_handlers import dp
from .games import REGISTRY
from .pricing import game_packs, pubg_packs, uc_options
from .config import (
    ADMIN_IDS,
    CARD_NUMBER,
    CARD_RUB_NUMBER,
    CLICK_READY,
    DEV_MODE,
    FULFILLMENT_MODE,
    PAYMENT_PROVIDER,
    SUPPLIER,
    WDG_API_KEY,
    WDG_DISCOVER,
    WDG_DUMP,
    WDG_PUBG_CATEGORY_ID,
    WEBAPP_DIR,
    WEBAPP_URL,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nexdonate")


async def _warm_catalog() -> None:
    """Fill the price cache at startup so the first user does not wait for the supplier."""
    try:
        packs = await pubg_packs(fulfillment.supplier)
        await asyncio.to_thread(uc_options, packs)  # builds the exact-amount table once
    except Exception as e:
        log.warning("could not pre-load the catalog: %s", e)
    for g in REGISTRY.values():
        if g.enabled and hasattr(fulfillment.supplier, "packs"):
            try:
                await game_packs(fulfillment.supplier, g, g.default_region)
            except Exception as e:
                log.warning("could not pre-load %s: %s", g.id, e)


async def _log_supplier_report() -> None:
    """Writes the supplier health report to the log at startup, so a deploy shows right away whether WDG is reachable."""
    try:
        for line in await fulfillment.supplier.probe():
            log.info("supplier: %s", re.sub(r"<[^>]+>", "", line))
        for g in REGISTRY.values():  # what each game's pack parsing produced, to spot a changed product name at a glance
            if g.enabled and hasattr(fulfillment.supplier, "packs"):
                try:
                    got = await game_packs(fulfillment.supplier, g, g.default_region)
                    log.info("game %s (%s): %s", g.id, g.default_region or "-", ", ".join(f"{p['section'] + ':' if p['section'] else ''}{p['label'] or p['amount']}=${p['cost_usd']}" for p in got))
                except Exception as e:
                    log.warning("game %s: %s", g.id, e)
        if WDG_DUMP and hasattr(fulfillment.supplier, "dump"):
            for line in await fulfillment.supplier.dump(WDG_DUMP):
                log.info("dump: %s", re.sub(r"<[^>]+>", "", line))
        if WDG_DISCOVER and hasattr(fulfillment.supplier, "discover"):
            for line in await fulfillment.supplier.discover(WDG_DISCOVER):
                log.info("discover: %s", re.sub(r"<[^>]+>", "", line).replace("\n", " ¦ "))
    except Exception as e:
        log.warning("supplier report failed: %s", e)


async def _supplier_monitor(interval: float = 60, rounds: int | None = None) -> None:
    """Tells the admins when the supplier stops answering and when it answers again.

    While it is down the shop refuses new orders by itself (the wallet check fails), so nobody pays for something we
    cannot deliver; this only makes sure the owner knows. On recovery the catalogs are loaded again and the report is
    written to the log (including any WDG_DISCOVER search that could not run while the supplier was away).
    """
    supplier = fulfillment.supplier
    fails, down_since = 0, None
    n = 0
    while rounds is None or n < rounds:
        n += 1
        await asyncio.sleep(interval)
        try:
            await asyncio.wait_for(supplier.balance(), 45)
            ok = True
        except Exception:
            ok = False
        if ok:
            fails = 0
            if down_since is not None:
                minutes = max(1, int((time.time() - down_since) / 60))
                down_since = None
                log.warning("%s answers again after about %d min", supplier.name, minutes)
                await notify.admin_event("supplier_up", f"✅ <b>{supplier.name} yana ishlayapti</b> (taxminan {minutes} daqiqa to'xtab turdi). Buyurtmalar qayta ochildi.")
                await _warm_catalog()
                if hasattr(supplier, "probe"):
                    await _log_supplier_report()
        else:
            fails += 1
            if fails == 3 and down_since is None:  # three minutes in a row, not a blip
                down_since = time.time() - 3 * interval
                log.error("%s does not answer", supplier.name)
                await notify.admin_event(
                    "supplier_down",
                    f"🚨 <b>{supplier.name} javob bermayapti</b> (3 daqiqadan beri).\n"
                    "Yangi buyurtmalar o'z-o'zidan to'xtatildi, mijoz to'lab qolmaydi. Tiklanganda xabar beraman.",
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if SUPPLIER == "wdg":
        if not WDG_API_KEY:
            raise RuntimeError("SUPPLIER=wdg requires WDG_API_KEY")
        if not WDG_PUBG_CATEGORY_ID:
            log.warning("SUPPLIER=wdg but WDG_PUBG_CATEGORY_ID is not set: the UC catalog stays empty until it is (see /supplier)")
    # Refuse unsafe combinations: with real supplier orders, nobody may be able to "pay" without paying.
    if FULFILLMENT_MODE == "live" and (PAYMENT_PROVIDER == "mock" or DEV_MODE):
        raise RuntimeError(
            "Unsafe config: FULFILLMENT_MODE=live requires a real PAYMENT_PROVIDER (not mock) and DEV_MODE=0"
        )
    if FULFILLMENT_MODE != "live" and PAYMENT_PROVIDER != "mock":
        log.warning("REAL payments are on but FULFILLMENT_MODE=%s: customers can pay and the UC will NOT really be delivered", FULFILLMENT_MODE)
    if PAYMENT_PROVIDER == "click" and not CLICK_READY:
        log.warning("PAYMENT_PROVIDER=click but CLICK_SERVICE_ID / CLICK_MERCHANT_ID / CLICK_SECRET_KEY are not all set: so'm payments will be unavailable")
    if PAYMENT_PROVIDER == "manual_card" and not ((CARD_NUMBER or CARD_RUB_NUMBER) and ADMIN_IDS):
        log.warning("PAYMENT_PROVIDER=manual_card needs a card (CARD_NUMBER / CARD_RUB_NUMBER) and ADMIN_IDS, otherwise receipts cannot be approved")
    await db.init()
    polling = None
    if notify.bot:
        if WEBAPP_URL:
            try:
                await notify.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="NexDonate", web_app=WebAppInfo(url=WEBAPP_URL))
                )
            except Exception as e:
                log.warning("could not set menu button: %s", e)
        try:
            click.bot_username = notify.bot_username = (await notify.bot.get_me()).username  # for Click's return link and the pay links
        except Exception as e:
            log.warning("could not read the bot username: %s", e)
        try:  # the "/" menu everyone sees, in the user's own Telegram language
            for code, start_d, settings_d in (("ru", "Начать", "Язык и валюта"), ("uz", "Boshlash", "Til va valyuta")):
                await notify.bot.set_my_commands(
                    [BotCommand(command="start", description=start_d), BotCommand(command="settings", description=settings_d)],
                    language_code=code,
                )
            await notify.bot.set_my_commands(
                [BotCommand(command="start", description="Boshlash / Начать"), BotCommand(command="settings", description="Til va valyuta / Язык и валюта")]
            )
        except Exception as e:
            log.warning("could not set the command menu: %s", e)
        for admin_id in ADMIN_IDS:  # the "/" command menu shows these only in the admin's own chat
            try:
                await notify.bot.set_my_commands(
                    [
                        BotCommand(command="start", description="Boshlash"),
                        BotCommand(command="settings", description="Til va valyuta"),
                        BotCommand(command="admin", description="Admin panel"),
                        BotCommand(command="users", description="Foydalanuvchilar ro'yxati"),
                        BotCommand(command="count", description="Nechta odam bor"),
                        BotCommand(command="supplier", description="Yetkazib beruvchi holati"),
                        BotCommand(command="id", description="Mening Telegram ID'im"),
                    ],
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
            except Exception as e:
                log.warning("could not set admin commands for %s: %s", admin_id, e)
        polling = asyncio.create_task(dp.start_polling(notify.bot, handle_signals=False))
    else:
        log.warning("BOT_TOKEN is empty: the bot is off and only DEV_MODE requests can authenticate")
    if DEV_MODE:
        log.warning("DEV_MODE is on: requests without Telegram initData are accepted. Never run like this in production.")
    await fulfillment.resume()
    warm = asyncio.create_task(_warm_catalog())
    monitor = asyncio.create_task(_supplier_monitor())
    report = asyncio.create_task(_log_supplier_report()) if hasattr(fulfillment.supplier, "probe") else None  # noqa: F841
    try:
        yield
    finally:
        monitor.cancel()
        if polling:
            polling.cancel()
        if notify.bot:
            await notify.bot.session.close()
        await fulfillment.supplier.close()
        await db.close()


app = FastAPI(title="NexDonate", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(api.router)
app.include_router(click.router)  # POST /api/click: Click's Prepare/Complete callbacks (no Telegram auth, signed instead)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")
