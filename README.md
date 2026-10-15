# NexDonate — Telegram Mini App

PUBG Mobile UC do'koni. Oqim: paket → Player ID (avtomatik tekshiruv) → to'lov → UC yetkazish.

## Tuzilma

- `app/` — backend (FastAPI + aiogram + SQLite)
  - `api.py` — Mini App API, `fulfillment.py` — buyurtmani yetkazib beruvchi orqali bajarish (`supplier.py` umumiy interfeys, `fazercards.py` va `wdg.py` ikkita ulagich)
  - `bot_handlers.py` — `/start`, `/admin`, chekni tasdiqlash/rad etish, qayta urinish
- `webapp/` — Mini App (HTML/CSS/JS, build kerak emas), `webapp/assets/banner.png` — `/start` rasmi
- `scripts/check_supplier.py` — FazerCards'ni faqat o'qish bilan tekshirish (pul sarflamaydi)

## Mahalliy ishga tushirish

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # keyin .env ni to'ldiring
python run.py               # http://localhost:8000
```

`DEV_MODE=1` bo'lsa, ilovani oddiy brauzerda (Telegram'siz) ochib ko'rish mumkin.

## Telegram ichida ishga tushirish

Mini App uchun **HTTPS** manzil kerak. Mahalliy test uchun: `cloudflared tunnel --url http://localhost:8000`
(yoki ngrok), chiqqan `https://...` manzilni `.env` dagi `WEBAPP_URL` ga yozing va `python run.py` ni qayta ishga tushiring.
Bot menyu tugmasi va `/start` dagi "Ochish" tugmasi shu manzilga ulanadi.

## Rejimlar (.env)

| O'zgaruvchi | Qiymat | Ma'nosi |
|---|---|---|
| `SUPPLIER` | `fazercards` (standart) / `wdg` | UC qaysi yetkazib beruvchidan olinadi. `wdg` uchun `WDG_API_KEY` va `WDG_PUBG_CATEGORY_ID` kerak (bot'da admin `/supplier` buyrug'i kategoriyalarni ko'rsatadi). WDG'da Player ID tekshiruvi yo'q: FazerCards kaliti bo'lsa va rejasi faol bo'lsa, ID shu orqali tekshiriladi, aks holda ID "tekshirilmagan" deb ogohlantirish bilan qabul qilinadi |
| `FULFILLMENT_MODE` | `mock` | Supplier balansi sarflanmaydi, yetkazish simulyatsiya qilinadi |
| | `live` | Haqiqiy FazerCards buyurtmalari (balans sarflanadi) |
| `PAYMENT_PROVIDER` | `mock` | "Test to'lov" tugmasi to'lovni simulyatsiya qiladi |
| | `manual_card` | Mijoz kartaga o'tkazib chek yuklaydi, admin botda tasdiqlaydi |

`FULFILLMENT_MODE=live` ni faqat `PAYMENT_PROVIDER=manual_card` (yoki keyin ulanadigan haqiqiy to'lov) va `DEV_MODE=0`
bilan ishlatish mumkin — aks holda server ishga tushmaydi.

## Narx

`narx (so'm) = FazerCards narxi (USD) × USD_UZS_RATE × (1 + MARKUP_PERCENT/100)`, `ROUND_TO` ga yuqoriga yaxlitlanadi.
Kursni `.env` da qo'lda yangilab turing.

## Admin

`ADMIN_IDS` ga o'z Telegram ID'ngizni yozing (@userinfobot dan oling). Admin `/admin` orqali buyurtmalar statistikasi va
FazerCards balansini ko'radi; kelgan cheklarni ✅/❌ tugmalari bilan hal qiladi; bajarilmagan buyurtmani 🔁 bilan qayta ishga tushiradi.
