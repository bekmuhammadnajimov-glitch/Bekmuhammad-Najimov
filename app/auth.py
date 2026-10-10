import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from . import db
from .config import BOT_TOKEN, DEV_MODE, DEV_USER_ID

MAX_AGE = 24 * 3600


def verify_init_data(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData (HMAC-SHA256) and return the user dict."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        params = dict(parse_qsl(init_data, keep_blank_values=True))
        received = params.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        if time.time() - int(params.get("auth_date", "0")) > MAX_AGE:
            return None
        return json.loads(params["user"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


async def current_user(x_init_data: str = Header(default="")) -> dict:
    tg = verify_init_data(x_init_data)
    if tg is None and DEV_MODE and not x_init_data:
        tg = {"id": DEV_USER_ID, "first_name": "Dev", "last_name": "User", "username": "dev_user", "language_code": "uz"}
    if tg is None:
        raise HTTPException(401, "unauthorized")

    existing = await db.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg["id"],))
    if existing is None:
        lang = "ru" if (tg.get("language_code") or "").startswith("ru") else "uz"
        await db.execute(
            "INSERT INTO users (tg_id, first_name, last_name, username, photo_url, lang, created_at) VALUES (?,?,?,?,?,?,?)",
            (tg["id"], tg.get("first_name"), tg.get("last_name"), tg.get("username"), tg.get("photo_url"), lang, db.now()),
        )
    else:
        await db.execute(
            "UPDATE users SET first_name=?, last_name=?, username=?, photo_url=? WHERE tg_id=?",
            (tg.get("first_name"), tg.get("last_name"), tg.get("username"), tg.get("photo_url"), tg["id"]),
        )
    return await db.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg["id"],))
