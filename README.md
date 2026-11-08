# Signal Relay — Multi-user copy-trading & signal platform for Freqtrade

**[فارسی پایین‌تر](#فارسی)**

One Freqtrade bot produces signals. Each user either **auto-trades** them on their own exchange account
(API key, no withdrawal rights) or receives them as **Telegram signals only**. The public home page shows the
bot's real, per-pair track record so users can build their own basket.

## Features
| Area | What it does |
|---|---|
| Public site | Landing page with verified bot performance per pair, spot/futures view, 30/90/365/all periods, equity curve, latest trades — in **Persian, English, German, Spanish** |
| Users | Sign-up, admin approval, subscription end date / trial days, own language, Telegram link with one click |
| Portfolios | Several per user: **exchange** (Binance, Bybit, OKX, Bitget, KuCoin, Gate, MEXC, HTX, BingX; spot or futures) or **signals-only** (no API) |
| Basket builder | Pick coins next to their track record; allocation **equal split** (Freqtrade `stake_amount: unlimited`), **weights**, or **custom amount per coin**; live preview |
| Risk | Exchange-side stop loss (survives server outages), daily loss limit, max open trades, admin caps on leverage/stake |
| Engine | Entry, DCA (same ratio as Freqtrade), partial exits (same %), full exit; real fills read from the exchange; exits always run even for paused users |
| Telegram | Per-user messages in their language; `/status /profit /daily /performance /balance /pause /resume /forceexit /lang`; daily report; subscription reminders; admin broadcast |
| Admin panel | Everything from the browser: formula, pairs, exchanges, Telegram token, webhook URL (regenerate), Freqtrade history import (REST API or `tradesv3.sqlite`), home-page texts per language, users, logs |
| Logs (SQLite) | Every webhook (incl. rejected), every exchange request/response per user, every Telegram message — browsable and filterable |

## Run
```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```
Open the site → the **first account you register becomes admin** → *Admin → Settings*:
1. Telegram bot token (from @BotFather) — the bot starts immediately.
2. Site URL, allowed exchanges & pairs (same as Freqtrade's `pair_whitelist`), formula.
3. Copy the webhook URL into Freqtrade (template: [`freqtrade/webhook-config.json`](freqtrade/webhook-config.json)).
4. Optional: *Admin → Performance data* → import the bot's earlier trades.

Data lives in `data/` (SQLite database + `secrets.json`). **Back up `data/secrets.json`** — without it the stored API keys cannot be decrypted.
Serve behind HTTPS (nginx/Caddy) and set `SECURE_COOKIES=true`. Use a single worker.

Docker:
```bash
docker build -t signal-relay . && docker run -d -p 8000:8000 -v $PWD/data:/app/data signal-relay
```

## Tests
```bash
pip install pytest && python -m pytest -q
```

---

<div dir="rtl">

## فارسی

یک ربات Freqtrade سیگنال تولید می‌کند. هر کاربر یا با کلید API صرافی خودش **معامله خودکار** می‌کند، یا فقط **سیگنال در تلگرام** می‌گیرد. صفحه اول سایت عملکرد واقعی ربات را به تفکیک هر ارز نشان می‌دهد تا کاربر بتواند سبد خودش را بسازد.

- **سایت چهارزبانه:** فارسی، انگلیسی، آلمانی، اسپانیایی (زبان تلگرام هر کاربر هم جدا).
- **سبدساز:** ارزها را کنار آمار عملکرد ربات انتخاب کنید و سرمایه را مساوی، وزنی یا با مبلغ دلخواه برای هر ارز تقسیم کنید.
- **فقط سیگنال:** برای کسانی که API ندارند؛ سیگنال ورود و خروج با مبلغ پیشنهادی و سود و زیان فرضی.
- **مدیریت ریسک:** حد ضرر روی خود صرافی، سقف ضرر روزانه، سقف تعداد معاملات.
- **همه‌چیز از داخل پنل:** توکن تلگرام، فرمول، ارزها، صرافی‌ها، آدرس وب‌هوک، متن صفحه اول، کاربران و اشتراک‌ها؛ بدون ویرایش فایل.
- **دیتابیس SQLite با لاگ کامل:** هر وب‌هوک، هر درخواست به صرافی و پاسخش برای هر کاربر، و هر پیام تلگرام.

**راه‌اندازی:** دستور بالا را اجرا کنید، اولین ثبت‌نام ادمین می‌شود، بقیه تنظیمات از «مدیریت ← تنظیمات». از فایل `data/secrets.json` نسخه پشتیبان بگیرید.

</div>
