import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram.types import BotCommand, BotCommandScopeChat, MenuButtonWebApp, WebAppInfo
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api, click, db, fulfillment, notify
from .bot_handlers import dp
from .pricing import pubg_packs, uc_options
from .config import (
    ADMIN_IDS,
    CARD_NUMBER,
    CARD_RUB_NUMBER,
    CLICK_READY,
    DEV_MODE,
    FULFILLMENT_MODE,
    PAYMENT_PROVIDER,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse unsafe combinations: with real supplier orders, nobody may be able to "pay" without paying.
    if FULFILLMENT_MODE == "live" and (PAYMENT_PROVIDER == "mock" or DEV_MODE):
        raise RuntimeError(
            "Unsafe config: FULFILLMENT_MODE=live requires a real PAYMENT_PROVIDER (not mock) and DEV_MODE=0"
        )
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
            click.bot_username = (await notify.bot.get_me()).username  # for Click's "return to the bot" link
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
    try:
        yield
    finally:
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
