"""The bot's menu: the commands list in the language each person chose (not the Telegram interface language), and the
persistent button under the message field that leads to the Mini App."""
import hashlib
import json
import logging

from aiogram.types import BotCommand, BotCommandScopeChat

from . import notify

log = logging.getLogger("nexdonate.botmenu")

# What every customer sees. "admin" writes to the owner (the owner's own list has the admin panel under the same name).
PUBLIC = {
    "uz": [("start", "Boshlash"), ("lang", "Til"), ("currency", "So'm / Rubl"), ("admin", "Admin")],
    "ru": [("start", "Начать"), ("lang", "Язык"), ("currency", "Сум / Рубль"), ("admin", "Админ")],
}
# What the owner sees: the same, but "admin" is the panel, plus the owner's tools.
OWNER = {
    "uz": [("start", "Boshlash"), ("lang", "Til"), ("currency", "So'm / Rubl"), ("admin", "Admin panel"), ("users", "Foydalanuvchilar ro'yxati"),
           ("count", "Nechta odam bor"), ("supplier", "Yetkazib beruvchi holati"), ("id", "Mening Telegram ID'im")],
    "ru": [("start", "Начать"), ("lang", "Язык"), ("currency", "Сум / Рубль"), ("admin", "Админ-панель"), ("users", "Список пользователей"),
           ("count", "Сколько людей"), ("supplier", "Статус поставщика"), ("id", "Мой Telegram ID")],
}
# For people who have not chosen a language yet: Telegram's own language decides, and the default shows both.
DEFAULT_BOTH = [("start", "Boshlash / Начать"), ("lang", "Til / Язык"), ("currency", "So'm / Rubl · Сум / Рубль"), ("admin", "Admin / Админ")]


def version() -> str:
    """Changes whenever the lists change, so the startup refresh knows it has to run again."""
    return hashlib.sha1(json.dumps([PUBLIC, OWNER, DEFAULT_BOTH], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]


def commands(lang: str, owner: bool) -> list[BotCommand]:
    table = OWNER if owner else PUBLIC
    return [BotCommand(command=c, description=d) for c, d in table.get(lang, table["uz"])]


_done: dict[int, tuple[str, bool]] = {}  # chat -> (language, owner) the menu was last set for in this process


async def ensure_commands(tg_id: int, lang: str | None, owner: bool) -> None:
    """Sets this chat's own command list in the person's language. Cheap to call often: it only talks to Telegram when something changed."""
    if not notify.bot or lang not in PUBLIC:
        return
    if _done.get(tg_id) == (lang, owner):
        return
    try:
        await notify.bot.set_my_commands(commands(lang, owner), scope=BotCommandScopeChat(chat_id=tg_id))
        _done[tg_id] = (lang, owner)
    except Exception as e:  # blocked the bot, etc.: try again the next time they write
        log.warning("could not set the menu of %s: %s", tg_id, e)
