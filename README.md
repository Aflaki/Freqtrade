# Signal Relay

میدلوری که سیگنال‌ها را از طریق **وب‌هوک** می‌گیرد و هم‌زمان به چند صرافی یا API پخش می‌کند. هر حساب کانفیگ خودش را دارد: مبلغ، لوریج، نوع بازار، نمادهای مجاز و غیره.

```
منبع سیگنال ──POST──▶ /webhook/<secret> ──▶ نرمال‌سازی ──┬─▶ Binance  (۵۰ تتر، لوریج ۱۰)
                                                       ├─▶ Bybit    (۵٪ موجودی، لوریج ۵)
                                                       ├─▶ OKX Spot (۰.۰۰۱ BTC)
                                                       └─▶ API دلخواه (HTTP + قالب JSON)
```

## راه‌اندازی
```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env        # کلیدها و WEBHOOK_SECRET را پر کنید
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
یا با Docker:
```bash
docker build -t signal-relay .
docker run -d -p 8000:8000 -v $PWD/config.yaml:/app/config.yaml --env-file .env signal-relay
```
> برای وب‌هوک معمولاً HTTPS لازم است؛ پشت nginx/Caddy یا Cloudflare Tunnel قرار دهید.

## اتصال به Freqtrade
۱. در `config.yaml` بگذارید `signal.source: freqtrade`.
۲. بلوک `webhook` فایل [`freqtrade/webhook-config.json`](freqtrade/webhook-config.json) را در `config.json` خود Freqtrade کپی کنید و `url` را با آدرس سرور و secret خودتان عوض کنید:
   ```
   "url": "https://relay.example.com/webhook/YOUR_WEBHOOK_SECRET"
   ```
   Freqtrade هدر سفارشی نمی‌فرستد، پس secret حتماً باید داخل URL باشد.
۳. چند ربات Freqtrade دارید؟ به هر کدام URL جدا با `?accounts=` بدهید:
   ```
   ربات A → /webhook/SECRET?accounts=binance_main,bybit_alt
   ربات B → /webhook/SECRET?accounts=bitget_copy
   ```

رفتار:
| پیام Freqtrade | کار میدلور |
|---|---|
| `entry_fill` با Long / Short | باز کردن لانگ / شورت با حجم کانفیگ هر حساب |
| `exit_fill` نهایی | بستن کامل پوزیشن همان جهت |
| `exit_fill` جزئی (partial exit) | بستن **همان درصد** از پوزیشن هر حساب (مثلاً ۲۵٪) |
| DCA / افزایش پوزیشن | یک ورود جدید؛ در حالت `copy_stake` به نسبت همان سفارش |
| status، cancel و بقیه | نادیده گرفته می‌شود (پاسخ 200 تا Freqtrade دوباره نفرستد) |

هر دو فرمت `json` و `form` (پیش‌فرض Freqtrade) پشتیبانی می‌شود. اسم جفت‌ارزها مثل `BTC/USDT:USDT` برای هر صرافی خودکار تطبیق داده می‌شود؛ حتی اگر Freqtrade روی فیوچرز باشد و حساب شما اسپات.

## فرمت سیگنال (حالت generic)
```json
{ "action": "buy", "symbol": "BTCUSDT", "price": 60000, "id": "optional-unique-id", "size_multiplier": 1 }
```
- اکشن‌ها: `long` `short` `close_long` `close_short` `close` (و aliasها مثل `buy`/`sell`/`exit`)
- اگر منبع شما اسم فیلدها را فرق می‌فرستد، در `signal.fields` نگاشت کنید (مسیر نقطه‌دار مثل `data.side` هم کار می‌کند).
- نماد تمیز می‌شود: `BINANCE:BTCUSDT.P` → `BTCUSDT`، و برای هر صرافی خودکار پیدا می‌شود (یا با `symbol_map` دستی).

## احراز هویت (یکی کافی است)
- `POST /webhook/<secret>`
- هدر `X-Webhook-Secret: <secret>`
- فیلد `passphrase` داخل JSON

## حجم سفارش (`sizing.mode`)
| mode | معنی `amount` | مثال |
|---|---|---|
| `fixed_quote` | مارجین به ارز quote | 50 تتر × لوریج 10 = پوزیشن 500 تتری |
| `fixed_base` | مقدار ارز پایه | 0.01 BTC |
| `percent_balance` | درصد موجودی آزاد (به‌عنوان مارجین) | 5٪ |
| `copy_stake` | نسبت به مارجینی که Freqtrade استفاده کرد | 0.5 = نصف استیک Freqtrade |

`max_notional` سقف ارزش هر سفارش است. `size_multiplier` داخل سیگنال حجم را برای همه حساب‌ها ضرب می‌کند.

## تست
- `dry_run: true` فقط سفارش را محاسبه و لاگ می‌کند و چیزی ارسال نمی‌کند.
- `sandbox: true` از تست‌نت صرافی استفاده می‌کند (اگر صرافی داشته باشد).
- `?wait=1` نتیجه هر حساب را در پاسخ برمی‌گرداند. بدون آن، پاسخ فوری `accepted` است.
- `?accounts=binance_main,bybit_alt` سیگنال را فقط به همین حساب‌ها می‌فرستد.

```bash
curl -X POST "http://localhost:8000/webhook/YOUR_SECRET?wait=1" \
  -H "Content-Type: application/json" \
  -d '{"action":"buy","symbol":"BTCUSDT","price":60000}'
```

## امکانات دیگر
- اجرای موازی برای همه حساب‌ها؛ خطای یک صرافی روی بقیه اثر ندارد
- retry خودکار برای خطای شبکه و rate limit؛ بدون retry برای موجودی ناکافی و سفارش نامعتبر
- حذف سیگنال تکراری (`dedupe_seconds`)
- محدودیت IP (`allowed_ips`)
- قفل جدا برای هر حساب تا سفارش‌ها روی هم نیفتند
- پشتیبانی از همه صرافی‌های ccxt (بیش از ۱۰۰ صرافی) + هر API دلخواه با `type: http`
