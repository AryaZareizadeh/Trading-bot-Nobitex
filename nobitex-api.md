# مستندات API نوبیتکس

> بزرگ‌ترین بازار ارز دیجیتال ایران — مرجع سازمان‌یافته‌ی APIها

این سند نسخه‌ی مرتب‌شده‌ی مستندات API نوبیتکس است. پیش از استفاده، با [قوانین نوبیتکس](https://nobitex.ir) و شرایط استفاده از API آشنا شوید و کانال‌های رسمی تلگرام و صفحه‌ی «سابقه تغییرات API» را برای اطلاع از تغییرات دنبال کنید.

**نکات کلیدی پیش از شروع:**
- کد شما باید قابلیت مدیریت خطاها، کنترل نرخ درخواست‌ها، جلوگیری از تشدید آبشاری خطاها و مدیریت ریسک را داشته باشد.
- آدرس پایه‌ی API: `https://apiv2.nobitex.ir`
- آدرس وب‌سوکت: `wss://ws.nobitex.ir/connection/websocket`
- محیط آزمایشی: `https://testnetapiv2.nobitex.ir` (سایت: `https://testnet.nobitex.ir`)

---

## فهرست مطالب

1. [احراز هویت و توکن](#۱-احراز-هویت-و-توکن)
2. [کلید API (آزمایشی)](#۲-کلید-api-آزمایشی)
3. [تنظیمات کلی (User Agent، محدودیت‌ها، تغییرات)](#۳-تنظیمات-کلی)
4. [اطلاعات بازار (عمومی)](#۴-اطلاعات-بازار-عمومی)
5. [اطلاعات کاربر](#۵-اطلاعات-کاربر)
6. [معامله در بازار اسپات](#۶-معامله-در-بازار-اسپات)
7. [معامله در بازار تعهدی](#۷-معامله-در-بازار-تعهدی)
8. [برداشت رمزارزی](#۸-برداشت-رمزارزی)
9. [برداشت ریالی](#۹-برداشت-ریالی)
10. [فهرست برداشت‌ها](#۱۰-فهرست-برداشت‌ها)
11. [وب‌سوکت](#۱۱-وب‌سوکت)
12. [دفتر آدرس و حالت برداشت امن](#۱۲-دفتر-آدرس-و-حالت-برداشت-امن)
13. [امنیت](#۱۳-امنیت)
14. [طرح معرفی دوستان](#۱۴-طرح-معرفی-دوستان)
15. [احراز هویت (دریافت خودکار توکن)](#۱۵-احراز-هویت-دریافت-خودکار-توکن)
16. [سود و زیان (پرتفو)](#۱۶-سود-و-زیان-پرتفو)
17. [سایر (تنظیمات سیستم)](#۱۷-سایر-تنظیمات-سیستم)
18. [ملاحظات عمومی](#۱۸-ملاحظات-عمومی)
19. [سوالات متداول](#۱۹-سوالات-متداول)

---

## ۱. احراز هویت و توکن

برای استفاده از APIهای **غیرعمومی** باید توکن را در هدر HTTP ارسال کنید:

```
Authorization: Token yourTOKENhereHEX0000000000
```

توکن مشخص می‌کند کدام کاربر درخواست را ارسال کرده است. **هرگز توکن خود را در اختیار دیگران قرار ندهید.** در صورت لو رفتن، فوراً logout کرده و توکن جدید بگیرید.

**روش پیشنهادی دریافت توکن:** مراجعه به پنل کاربری → پروفایل → تنظیمات. اگر هنگام ورود گزینه «مرا به خاطر بسپار» را انتخاب کنید، توکن تا ۳۰ روز (یا تا زمان logout) معتبر است.

برای دریافت دوره‌ای و خودکار توکن می‌توانید از [API ورود](#۱۵-احراز-هویت-دریافت-خودکار-توکن) استفاده کنید، اما این کار تنها در صورت آشنایی با مخاطرات ذخیره‌ی گذرواژه در کد توصیه می‌شود.

---

## ۲. کلید API (آزمایشی)

کلید API دسترسی امن‌تر و با مجوز مشخص را فراهم می‌کند: دسترسی کنترل‌شده از روی IP مشخص با مدت اعتبار دلخواه.

**مزایا:** سطح دسترسی محدود (READ / TRADE / WITHDRAW)، پشتیبانی از IP Whitelist، تاریخ انقضای دلخواه، غیرفعال‌سازی بدون تغییر پسورد، مناسب برای ربات‌ها.

### سطوح دسترسی

| سطح | توضیح |
|---|---|
| `READ` | دریافت اطلاعات بدون تغییر در دیتابیس (مثل لیست سفارشات) |
| `TRADE` | عملیات معامله و ترید که در دیتابیس تغییر ایجاد می‌کند |
| `WITHDRAW` | عملیات برداشت |

هر کلید می‌تواند یک یا چند دسترسی داشته باشد.

### ایجاد کلید — `POST /apikeys/create`

| پارامتر | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `name` | string | الزامی | نام کلید |
| `description` | string | `''` | توضیح دلخواه |
| `permissions` | string | الزامی | `READ`, `TRADE`, `WITHDRAW` جداشده با کاما |
| `ipAddressesWhitelist` | list | `[]` | لیست IPهای مجاز (IPv4/IPv6) |
| `expirationDate` | datetime (UTC) | None | تاریخ انقضا (اختیاری) |

**نکات امنیتی:** ارسال OTP در هدر `X-totp` الزامی است. پس از ایجاد، ایمیل اطلاع‌رسانی ارسال می‌شود و رویداد در لاگ ثبت می‌گردد.

> ⚠️ کلید خصوصی (`privateKey`) فقط **یک بار** در پاسخ نمایش داده می‌شود. حتماً در Secret Manager ذخیره کنید.

```json
// نمونه پاسخ
{
  "key": {
    "key": "5XOCQZSPLQM4MiLzuUnZoBuqgYgTKl40W2X5j1pxfIA=",
    "name": "my-api-key",
    "permissions": "READ,TRADE",
    "expirationDate": "2025-12-31T23:59:59Z",
    "ipAddressesWhitelist": ["192.168.1.10", "10.0.0.5"]
  },
  "privateKey": "S5y19KewZzheCWCO4xqMcwwvtR8vQ-hHjE_cdjz-XxE=",
  "status": "ok"
}
```

### مدیریت کلیدها

| عملیات | درخواست | توضیح |
|---|---|---|
| لیست کلیدها | `GET /apikeys/list` | لیست کلیدها + وضعیت |
| حذف کلید | `POST /apikeys/delete/` | `public_key` در URL |
| بروزرسانی کلید | `POST /apikeys/update/` | قابل تغییر: `name`, `description`, `ipAddressesWhitelist` |

### استفاده از کلید API

سه هدر در هر درخواست لازم است:

| هدر | توضیح |
|---|---|
| `Nobitex-Key` | کلید عمومی کاربر |
| `Nobitex-Signature` | امضای محاسبه‌شده با الگوریتم **Ed25519** |
| `Nobitex-Timestamp` | زمان جاری به ثانیه (Unix timestamp) در UTC |

**محاسبه‌ی امضا** بر اساس: `timestamp` + `method` (مثل GET/POST) + `url` (مسیر کامل مثل `/market/orders/list?fromId=123`) + `body` (بدنه‌ی خام درخواست).

```bash
curl -X POST "https://apiv2.nobitex.ir/orders/cancel-old" \
  -H "Content-Type: application/json" \
  -H "Nobitex-Key: <Your-Public-Key>" \
  -H "Nobitex-Signature: <Generated-Signature>" \
  -H "Nobitex-Timestamp: <Unix-Timestamp>" \
  -d '{"order": 27032, "status": "canceled"}'
```

---

## ۳. تنظیمات کلی

### تنظیم User Agent

اکیداً توصیه می‌شود مقدار هدر `User-Agent` را مطابق الگوی `TraderBot/XXXXX` ارسال کنید (که `XXXXX` نام یکتای دلخواه ربات شماست). این کار به پشتیبانی و عیب‌یابی کمک می‌کند.

### محدودیت‌ها

محدودیت‌های هر API در توضیحات همان API ذکر شده‌اند. هنگام لاگین، اگر دستگاه جدید شناخته شود، محدودیت برداشت به مدت یک ساعت اعمال می‌شود. برای جلوگیری، مقدار `device` دریافتی هنگام لاگین را در درخواست‌های بعدی ارسال کنید، یا از توکن‌های بلندمدت استفاده نمایید.

### تغییرات و موارد قدیمی

API نوبیتکس ضمن حفظ ساختار کلی، به‌طور مستمر به‌روزرسانی می‌شود. صفحه‌ی «سابقه تغییرات» را رصد کنید. ممکن است فیلدهای اضافه‌ای فراتر از مستندات در خروجی باشند؛ این فیلدها را به‌عنوان امکانات آزمایشی در نظر بگیرید و کد شما در صورت مشاهده‌ی فیلد ناشناخته نباید دچار خطا شود.

---

## ۴. اطلاعات بازار (عمومی)

> **توجه:** نمادهای معتبر بازار در بسیاری از این APIها مشترک‌اند. لیست کامل در [پیوست نمادها](#پیوست-لیست-نمادهای-معتبر-بازار) آمده و در ادامه به آن ارجاع داده می‌شود.

### اردربوک — `GET /v3/orderbook/SYMBOL`

- محدودیت: ۳۰۰ درخواست/دقیقه · توکن: ندارد · فقط متد **GET**
- برای دریافت همه‌ی بازارها به‌صورت یکجا از `SYMBOL=all` استفاده کنید.

```json
{
  "status": "ok",
  "lastUpdate": 1644991756704,
  "lastTradePrice": "35650565900",
  "asks": [["1476091000", "1.016"], ["1479700000", "0.2561"]],
  "bids": [["1470001120", "0.126571"], ["1470000000", "0.818994"]]
}
```

| پارامتر پاسخ | نوع | توضیح |
|---|---|---|
| `asks` | array | دوتایی‌های «قیمت، مقدار» سفارش‌های **فروش** |
| `bids` | array | دوتایی‌های «قیمت، مقدار» سفارش‌های **خرید** |
| `lastTradePrice` | string | مبلغ آخرین معامله |
| `lastUpdate` | int | زمان آخرین به‌روزرسانی (Unix) |

**روش پیشنهادی نگهداری:** از [وب‌سوکت](#۱۱-وب‌سوکت) استفاده کنید. در غیر این صورت در حلقه با فاصله‌ی ۱ تا ۱۰ ثانیه فراخوانی کنید (بازه‌های زیر ۱ ثانیه به‌دلیل کش، داده‌ی تکراری برمی‌گردانند). در صورت اجرای چند رشته/سرور، اردربوک را یک‌بار دریافت و میان نمونه‌ها به اشتراک بگذارید.

### نمودار عمق (آزمایشی) — `GET /v2/depth/SYMBOL`

- محدودیت: ۳۰۰ درخواست/دقیقه · توکن: ندارد · فقط متد **GET**
- مشابه اردربوک، اما مقادیر قیمت تجمیع و گرد شده‌اند.

```json
{
  "status": "ok",
  "lastUpdate": "1658565992195",
  "bids": [["7309999000", "2.200274"], ["7310000000", "2.199856"]],
  "asks": [["7409894000", "0.054789"], ["7447665000", "0.055276"]],
  "lastTradePrice": "7417000000"
}
```

### لیست معاملات — `GET /v2/trades/SYMBOL`

- محدودیت: ۶۰ درخواست/دقیقه · توکن: ندارد
- پارامتر `time` به فرمت Unix است.

```json
{
  "status": "ok",
  "trades": [
    {"time": 1588689375067, "price": "1470000110", "volume": "0", "type": "sell"},
    {"time": 1588689360464, "price": "1470000110", "volume": "0.002", "type": "buy"}
  ]
}
```

### آمار بازار — `GET /market/stats`

- محدودیت: ۲۰ درخواست/دقیقه

| پارامتر | نوع | توضیح | نمونه |
|---|---|---|---|
| `srcCurrency` | string | ارز مبدا (اختیاری) | `btc`, `usdt` |
| `dstCurrency` | string | ارز مقصد (اختیاری) | `rls` |

اگر پارامترها وارد نشوند، آمار کل بازار برمی‌گردد. هر کدام به‌تنهایی نیز قابل استفاده‌اند (مثلاً `dstCurrency=rls` همه‌ی بازارهای ریالی).

```json
{
  "status": "ok",
  "stats": {
    "btc-rls": {
      "isClosed": false, "bestSell": "749976360", "bestBuy": "733059600",
      "volumeSrc": "0.2929480000", "volumeDst": "212724856.06",
      "latest": "750350000", "mark": "747461987",
      "dayLow": "686021860", "dayHigh": "750350000",
      "dayOpen": "686021860", "dayClose": "750350000", "dayChange": "9.38"
    }
  }
}
```

> آمار بایننس (کلید `global`) **منسوخ** شده و در نسخه‌های آتی حذف می‌شود.

### آمار OHLC — `GET /market/udf/history`

| پارامتر | نوع | الزام | توضیح |
|---|---|---|---|
| `symbol` | string | الزامی | نماد بازار |
| `resolution` | string | الزامی | بازه‌ی هر کندل |
| `to` | int | الزامی | زمان پایان بازه (Unix) |
| `from` | int | اختیاری | زمان ابتدای بازه |
| `countback` | int | اختیاری | تعداد کندل پیش از زمان پایان (اولویت بیشتر از `from`) |
| `page` | int | `1` | شماره صفحه |

**مقادیر `resolution`:** دقیقه‌ای `1, 5, 15, 30` · ساعتی `60, 180, 240, 360, 720` · روزانه `D, 2D, 3D`

```json
{
  "s": "ok",
  "t": [1562095800, 1562182200],
  "o": [146272500, 150551000],
  "h": [155869600, 161869500],
  "l": [140062400, 150551000],
  "c": [151440200, 157000000],
  "v": [18.221362316, 9.8592626506]
}
```

| فیلد | توضیح | فیلد | توضیح |
|---|---|---|---|
| `s` | وضعیت پاسخ | `l` | کمترین قیمت |
| `t` | زمان شروع | `c` | قیمت پایانی |
| `o` | قیمت شروع | `v` | حجم معاملات |
| `h` | بیشترین قیمت | | |

**نکات:** کندل‌های دقیقه‌ای از ابتدای سال ۱۴۰۱ در دسترس‌اند. حداکثر **۵۰۰ کندل** در هر درخواست؛ برای بیشتر از `page` استفاده کنید. خطای resolution نامعتبر: `{"s": "error", "errmsg": "Invalid resolution!"}` · نبود داده: `{"s": "no_data"}`.

---

## ۵. اطلاعات کاربر

### پروفایل — `GET /users/profile`

اطلاعات پروفایل، کارت/حساب بانکی، موارد تایید شده، تنظیمات (فی تراکنش و …) و خلاصه‌ی آمار مبادلات را برمی‌گرداند. فقط ارسال توکن لازم است.

```json
{
  "status": "ok",
  "profile": {
    "firstName": "...", "lastName": "...", "email": "name@example.com",
    "bankCards": [{"number": "...", "bank": "ملی", "confirmed": true}],
    "bankAccounts": [{"shaba": "IR...", "bank": "ملی", "confirmed": true}],
    "verifications": {"email": true, "mobile": true, "identity": true, "...": "..."},
    "options": {"fee": "0.35", "feeUsdt": "0.2", "tfa": false},
    "withdrawEligible": true
  },
  "tradeStats": {"monthTradesTotal": "10867181.53", "monthTradesCount": 3},
  "websocketAuthParam": "1987577cdf7c7422dee369e188e276ee"
}
```

> `websocketAuthParam` برای اتصال به کانال‌های خصوصی وب‌سوکت استفاده می‌شود.

### تولید آدرس بلاکچین — `POST /users/wallets/generate-address`

- محدودیت: ۳۰ درخواست/ساعت

| پارامتر | نوع | الزام | توضیح |
|---|---|---|---|
| `currency` (ارجح) | string | الزامی (جایگزین `wallet`) | رمزارز کیف |
| `wallet` (قدیمی) | string | در نبود `currency` الزامی | شناسه کیف پول |
| `network` | string | اختیاری | شبکه (مثل `BSC`) |

### افزودن کارت بانکی — `POST /users/cards-add`

- محدودیت: ۳۰ درخواست/۳۰ دقیقه · پارامترها: `number`, `bank`

### افزودن حساب بانکی — `POST /users/accounts-add`

- محدودیت: ۳۰ درخواست/۳۰ دقیقه · پارامترها: `number`, `shaba`, `bank`

### محدودیت‌های کاربر — `GET /users/limitations`

```json
{
  "status": "ok",
  "limitations": {
    "userLevel": "level2",
    "features": {
      "crypto_trade": false, "rial_trade": false,
      "coin_deposit": false, "rial_deposit": false,
      "coin_withdrawal": false, "rial_withdrawal": false
    },
    "limits": {
      "withdrawRialDaily": {"used": "0", "limit": "900000000"},
      "withdrawCoinDaily": {"used": "0", "limit": "2000000000"},
      "withdrawTotalDaily": {"used": "0", "limit": "2000000000"},
      "withdrawTotalMonthly": {"used": "0", "limit": "30000000000"}
    }
  }
}
```

**`features`:** امکان مبادله‌ی رمزارز/ریال، واریز رمزارز/ریال، برداشت رمزارز/ریال. **`limits`:** سقف برداشت روزانه‌ی ریال، روزانه‌ی رمزارز، روزانه‌ی مجموع، ماهانه‌ی مجموع. تمام مبالغ به **ریال** هستند.

### لیست کیف پول‌ها — `GET /users/wallets/list`

- محدودیت: ۲۰ درخواست/۲ دقیقه
- پارامتر `type` (`spot` پیش‌فرض یا `margin`) نوع کیف پول را مشخص می‌کند.

```json
{
  "status": "ok",
  "wallets": [
    {
      "id": 2693280, "currency": "rls",
      "balance": "746212980", "blockedBalance": "0", "activeBalance": "746212980",
      "rialBalance": 746212980, "depositInfo": {"FIAT_MONEY": {"address": null}}
    },
    {
      "id": 133778, "currency": "btc",
      "balance": "0", "depositAddress": "bc1q...",
      "depositInfo": {"BTC": {"address": "bc1q..."}, "BTCLN": {"address": null}}
    }
  ]
}
```

> کیف پول یک رمزارز تنها زمانی ایجاد می‌شود که کاربر سفارشی در آن بازار ثبت یا آدرس واریز ایجاد کرده باشد.

### لیست کیف پول‌ها (انتخابی) — `GET /v2/wallets`

- محدودیت: ۱۵ درخواست/دقیقه

| پارامتر | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `currencies` | string | اختیاری | ارزها جداشده با کاما (`rls,btc`) |
| `type` | string | `spot` | `spot` یا `margin` |

```json
{"status": "ok", "wallets": {"RLS": {"id": 133777, "balance": "0E-10", "blocked": "0"}, "BTC": {"id": 133778, "balance": "0E-10", "blocked": "0"}}}
```

### موجودی — `POST /users/wallets/balance`

- محدودیت: ۶۰ درخواست/۲ دقیقه · پارامتر `currency` (الزامی)
- مقدار موجودی به‌صورت `string` با اعشار زیاد برمی‌گردد؛ برای محاسبات از انواع fixed precision استفاده کنید.

```json
{"balance": "10.2649975000", "status": "ok"}
```

### لیست تراکنش‌های کیف‌پول — `GET /users/wallets/transactions/list`

- محدودیت: ۶۰ درخواست/۲ دقیقه · صفحه‌بندی (پیش‌فرض ۵۰) · پارامتر `wallet` (شناسه‌ی کیف پول، الزامی)

### تاریخچه تراکنش‌ها (انتخابی) — `GET /users/transactions-history`

- محدودیت: ۶۰ درخواست/ساعت · صفحه‌بندی (پیش‌فرض ۵۰)

| پارامتر | نوع | توضیح |
|---|---|---|
| `currency` | string | ارز تراکنش (اختیاری) |
| `tp` | string | نوع تراکنش (اختیاری) |
| `from` / `to` | datetime | بازه‌ی زمانی (حداکثر ۹۰ روز؛ `from` باید عقب‌تر از `to` باشد) |
| `from_id` | int | شناسه‌های بزرگ‌تر از این |

**مقادیر `tp`:** `deposit` (واریز)، `withdraw` (برداشت)، `buy` (دریافتی معامله)، `sell` (پرداختی معامله)، `manual` (سیستمی)، `referral`، `transfer`، `pnl` (سود/زیان تعهدی)، `delegate` (استخر مشارکت)، `staking`، `yield_farming`، `discount`.

### لیست واریزها — `GET /users/wallets/deposits/list`

- محدودیت: ۶۰ درخواست/۲ دقیقه · صفحه‌بندی (پیش‌فرض ۱۰ ریال / ۲۰ سایر) · فیلتر زمانی
- پارامتر `wallet` (پیش‌فرض `all`). واریزهای حداکثر ۳ ماه اخیر. صفحه‌بندی و فیلتر زمانی فقط وقتی `wallet` مشخص شده باشد کار می‌کند.

### بازارهای مورد علاقه

| عملیات | درخواست | محدودیت |
|---|---|---|
| دریافت لیست | `GET /users/markets/favorite` | ۶ درخواست/دقیقه |
| ثبت | `POST /users/markets/favorite` | ۱۲ درخواست/دقیقه |
| حذف | `DELETE /users/markets/favorite` | ۱۲ درخواست/دقیقه |

پارامتر `market`: نماد بازار جداشده با کاما (مثل `BTCIRT,DOGEUSDT`)؛ برای حذف همه `All`. همگی نیازمند توکن‌اند.

---

## ۶. معامله در بازار اسپات

### ثبت سفارش جدید — `POST /market/orders/add`

- محدودیت: ۳۰۰ درخواست/۱۰ دقیقه (**محدودیت مشترک** با همه‌ی APIهای سفارش‌گذاری)

| پارامتر | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `type` | string | الزامی | `buy` یا `sell` |
| `execution` | string | `limit` | `market` (قیمت بازار) یا `limit` (قیمت معین) |
| `srcCurrency` | string | الزامی | رمزارز مبدا |
| `dstCurrency` | string | الزامی | `rls` یا `usdt` |
| `amount` | monetary | الزامی | مقدار رمزارز (حجم) |
| `price` | monetary | الزامی | قیمت واحد |
| `clientOrderId` | string | null | شناسه‌ی سفارش کاربر، تا ۳۲ کاراکتر، یکتا (آزمایشی) |

```json
{
  "status": "ok",
  "order": {
    "type": "sell", "srcCurrency": "Bitcoin", "dstCurrency": "ریال",
    "price": "520000000", "amount": "0.6", "totalPrice": "312000000.0",
    "matchedAmount": 0, "unmatchedAmount": "0.6", "id": 25,
    "status": "Active", "partial": false, "fee": 0,
    "created_at": "2018-11-28T11:36:13.592827+00:00", "clientOrderId": "order1"
  }
}
```

> ثبت سفارش الزاماً به معنی انجام معامله نیست. سفارش‌ها پس از ثبت دوباره اعتبارسنجی می‌شوند؛ اگر بلافاصله «رد شده» می‌شوند، مقدار، قیمت و موجودی را بررسی کنید.

#### سفارش حد ضرر (Stop)

| پارامتر ویژه | نوع | الزام | توضیح |
|---|---|---|---|
| `execution` | string | الزامی | `stop_market` یا `stop_limit` |
| `stopPrice` | monetary | الزامی | قیمت توقف |

در زمان رسیدن قیمت بازار به قیمت توقف فعال می‌شود. وضعیت اولیه `Inactive` است.

```bash
POST /market/orders/add  type=sell srcCurrency=doge dstCurrency=rls \
  amount=64 execution=stop_market stopPrice=47500 clientOrderId=order1
```

#### سفارش OCO

دو سفارش هم‌زمان: یک سفارش با قیمت معین + یک سفارش حد ضرر. با انجام هرکدام، دیگری لغو می‌شود.

| پارامتر ویژه | نوع | الزام | توضیح |
|---|---|---|---|
| `mode` | string | الزامی | `oco` |
| `stopPrice` | monetary | الزامی | قیمت توقف حد ضرر |
| `stopLimitPrice` | monetary | الزامی | قیمت حد ضرر |

**شرط قیمت OCO:**
- خرید: `price < آخرین قیمت بازار < stopPrice و stopLimitPrice`
- فروش: `price > آخرین قیمت بازار > stopPrice و stopLimitPrice`

#### کدهای خطای ثبت سفارش

| کد | توضیح |
|---|---|
| `InvalidOrderPrice` | قیمت تعیین نشده یا اشتباه است |
| `BadPrice` | قیمت با بازار تفاوت زیاد دارد (باید در بازه‌ی ۳۰٪ قیمت کنونی باشد). در حد ضرر: قیمت بازار نمی‌تواند بهتر از قیمت توقف باشد |
| `PriceConditionFailed` | شرط قیمت رعایت نشده |
| `OverValueOrder` | مقدار/ارزش سفارش از موجودی بیشتر است |
| `SmallOrder` | حداقل ارزش معامله رعایت نشده (ریالی: ۳ میلیون ریال، تتری: ۱۱ تتر) |
| `DuplicateOrder` | سفارش مشابه در ۱۰ ثانیه‌ی اخیر ارسال شده |
| `InvalidMarketPair` | بازار وجود ندارد یا ارزها اشتباه‌اند |
| `MarketClosed` | بازار موقتاً بسته است |
| `TradingUnavailable` | کاربر اجازه‌ی معامله ندارد (احراز هویت ناقص) |
| `FeatureUnavailable` | کاربر مجاز به امکانات آزمایشی نیست |
| `DuplicateClientOrderId` | شناسه‌ی سفارش کاربر تکراری است |

**نکات کلیدی:**
- **واحدها:** قیمت در بازار ریالی به **ریال** (نه تومان) و در بازار تتری به **تتر**. واحد `amount` بر حسب رمزارز مبدا.
- **سفارش مارکت:** برای سفارش به قیمت بازار `execution=market`. به‌شدت توصیه می‌شود `price` را به‌عنوان تخمین قیمت ارسال کنید تا سفارش فقط در بازه‌ی مشخص پر شود. بدون `price` معامله تا بازه‌ی نوسان ۱٪ انجام می‌شود.
- **دقت monetary:** بسته به بازار ۰ تا ۸ رقم اعشار؛ ارقام اضافی در مقدار به پایین و در قیمت با روش بانکداری گرد می‌شوند.
- در حالت **Pro** محافظت در برابر سفارش تکراری غیرفعال است؛ حتماً از `clientOrderId` استفاده کنید.

### مشاهده وضعیت سفارش — `POST /market/orders/status`

- محدودیت: ۳۰۰ درخواست/دقیقه

| پارامتر | نوع | الزام | توضیح |
|---|---|---|---|
| `id` | int | اختیاری | شناسه‌ی سفارش |
| `clientOrderId` | string | اختیاری | شناسه‌ی سفارش کاربر (آزمایشی) |

حداقل یکی از `id` یا `clientOrderId` الزامی است؛ در صورت ارسال هر دو، اولویت با `id`. خطای نبود هیچ‌کدام: `NullIdAndClientOrderId`.

**مقادیر `status`:**
- `Active` — مقدار پرنشده دارد و در بازار فعال است
- `Done` — کاملاً معامله شده
- `Inactive` — حد ضرر هنوز فعال نشده
- `Canceled` — پیش از پر شدن کامل لغو شده (توسط کاربر یا سامانه به‌دلیل کمبود نقدینگی در بازه‌ی ۱٪ یا کاهش موجودی)

### فهرست سفارش‌های کاربر — `GET /market/orders/list`

- محدودیت: ۳۰ درخواست/دقیقه · صفحه‌بندی (پیش‌فرض ۱۰۰، حداکثر ۱۰۰۰)

| پارامتر | پیش‌فرض | توضیح |
|---|---|---|
| `status` | `open` | `all` / `open` / `done` / `close` |
| `type` | همه | `buy` یا `sell` |
| `execution` | همه | `limit`, `market`, `stop_limit`, `stop_market` |
| `tradeType` | همه | `spot` یا `margin` |
| `srcCurrency` / `dstCurrency` | همه | فیلتر بازار |
| `details` | `1` | `1` یا `2` (جزئیات بیشتر) |
| `fromId` | `1` | سفارش‌های با شناسه‌ی بزرگ‌تر |
| `order` | متغیر | `id`/`created_at`/`price` (صعودی) یا با `-` نزولی |

**وضعیت‌ها:** `all` (همه)، `open` (باز شامل فعال و غیرفعال)، `done` (بخشی یا کامل پر شده)، `close` (انجام‌شده یا لغوشده). وضعیت‌های `active` و `undone` منسوخ شده‌اند.

**شیٔ Order** (پایه): `type`, `execution` (`Limit`/`Market`/`StopMarket`/`StopLimit`), `tradeType` (`Spot`/`Margin`), `srcCurrency`, `dstCurrency`, `price`, `amount`, `matchedAmount`, `param1` (قیمت توقف). با `details=2` اضافه می‌شود: `id`, `status`, `fee`, `created_at`, `averagePrice`.

> برای تشخیص پر شدن سفارش از `matchedAmount` استفاده کنید (نه فقط `status`). سفارش ممکن است به‌تدریج پر شود و `Active` بماند.

### تغییر وضعیت سفارش — `POST /market/orders/update-status`

- محدودیت: ۹۰ درخواست/دقیقه

| پارامتر | الزام | توضیح |
|---|---|---|
| `order` | اختیاری | شناسه‌ی سفارش |
| `clientOrderId` | اختیاری | شناسه‌ی سفارش کاربر (آزمایشی) |
| `status` | الزامی | وضعیت جدید (`canceled`) |

تغییر مجاز: `new`→`active` یا `active`/`inactive`→`canceled`. اگر سفارش جزئی از OCO باشد، هر دو لغو می‌شوند. خطا: `NullIdAndClientOrderId`.

### لغو جمعی سفارشات — `POST /market/orders/cancel-old`

- محدودیت: ۳۰ درخواست/دقیقه

| پارامتر | توضیح |
|---|---|
| `hours` | ساعت قبل از زمان درخواست (مثلاً `2` یعنی سفارش‌های ۲ ساعت قبل). بدون آن، همه‌ی سفارش‌های فعال لغو می‌شوند |
| `execution` | `market`/`limit`/`stop_market`/`stop_limit` |
| `tradeType` | `spot` یا `margin` |
| `srcCurrency` / `dstCurrency` | فیلتر بازار |

> سفارش‌های حد ضرر غیرفعال غیر-OCO مشمول این لغو نمی‌شوند.

### فهرست معاملات کاربر — `GET /market/trades/list`

- محدودیت: ۳۰ درخواست/دقیقه · صفحه‌بندی (پیش‌فرض ۳۰) · فقط ۳ روز اخیر

| پارامتر | الزام | توضیح |
|---|---|---|
| `srcCurrency` / `dstCurrency` | اختیاری | هر دو باید مقدار داشته باشند یا هر دو خالی |
| `fromId` | اختیاری | حداقل شناسه |

```json
{
  "status": "ok",
  "trades": [{
    "id": 123412, "orderId": 1231222, "srcCurrency": "Tether", "dstCurrency": "﷼",
    "market": "USDT-RLS", "timestamp": "2022-07-05T09:57:38+00:00", "type": "sell",
    "price": "316800", "amount": "57.3605", "total": "18171806.4", "fee": "27257.71"
  }],
  "hasNext": false
}
```

---

## ۷. معامله در بازار تعهدی

> استفاده از این ویژگی به‌منزله‌ی پذیرش شرایط معاملات تعهدی است. موقعیت خرید در مرحله‌ی **تست بتا** است.

### بازارهای تعهدی — `GET /margin/markets/list`

- محدودیت: ۳۰ درخواست/دقیقه

```json
{
  "status": "ok",
  "markets": {
    "BTCUSDT": {"srcCurrency": "btc", "dstCurrency": "usdt", "positionFeeRate": "0.001", "maxLeverage": "5", "sellEnabled": true, "buyEnabled": true}
  }
}
```

شیٔ `MarginMarketSetting`: `srcCurrency`, `dstCurrency`, `positionFeeRate` (نرخ کارمزد تمدید روزانه)، `maxLeverage`, `sellEnabled`, `buyEnabled`. بیشینه‌ی اهرم به عمق بازار و سطح کاربری بستگی دارد؛ برای مقدار دقیق، توکن را ارسال کنید.

### استخرهای مشارکت — `GET /liquidity-pools/list`

- محدودیت: ۱۲ درخواست/دقیقه — ظرفیت `capacity` و `filledCapacity` هر استخر را برمی‌گرداند. در استخرهای غیرفعال ثبت سفارش جدید ممکن نیست (اما بستن موقعیت میسر است).

### انتقال به کیف‌پول تعهدی — `POST /wallets/transfer`

- محدودیت: ۱۰ درخواست/دقیقه

| پارامتر | الزام | توضیح |
|---|---|---|
| `currency` | الزامی | `rls` یا `usdt` |
| `amount` | الزامی | مقدار انتقال |
| `src` | الزامی | `spot` یا `margin` |
| `dst` | الزامی | `spot` یا `margin` |

مبدا و مقصد نباید یکسان باشند. کیف‌پول تعهدی با اولین انتقال ایجاد می‌شود. **خطاها:** `ParseError`, `InvalidAmount`, `SameDestination`, `WalletNotFound` (فقط ریال و تتر)، `InsufficientBalance`.

### محدودیت‌های وکالت — `GET /margin/v2/delegation-limit`

- محدودیت: ۱۲ درخواست/دقیقه · پارامتر `market` (الزامی، مثل `BTCUSDT`) — جایگزین endpoint قدیمی `GET /margin/delegation-limit`.

محدودیت‌های باقی‌مانده به‌تفکیک `buy` و `sell` و برای هر اهرم (از ۱ تا حداکثر، با گام ۰.۵) برمی‌گردد. واحد `limit` در `sell` = رمزارز مبدا، در `buy` = رمزارز مقصد.

```json
{
  "status": "ok",
  "limits": {
    "buy":  [{"leverage": "1", "limit": "1250"}, {"leverage": "2", "limit": "2500"}],
    "sell": [{"leverage": "1", "limit": "0.021"}, {"leverage": "2", "limit": "0.042"}]
  }
}
```

- سفارش فروش تعهدی: `amount ≤ sell.limit` اهرم انتخابی.
- سفارش خرید تعهدی: `price × amount ≤ buy.limit` اهرم انتخابی.

**خطاها:** `TradeLimitation` (سطح احراز ناکافی)، `InvalidSymbol`, `InvalidMarket`, `UnsupportedMarginSymbol`.

### درج سفارش تعهدی — `POST /margin/orders/add`

- محدودیت: ۳۰۰ درخواست/۱۰ دقیقه (مشترک)

| پارامتر | پیش‌فرض | توضیح |
|---|---|---|
| `execution` | `limit` | `limit`/`market`/`stop_limit`/`stop_market` |
| `srcCurrency` / `dstCurrency` | الزامی | بازار |
| `type` | `sell` | `sell` یا `buy` |
| `leverage` | `1` | ضریب وکالت (۱ تا بیشینه با گام ۰.۵) |
| `amount` | الزامی | مقدار وکالت |
| `price` | الزامی | قیمت سفارش |
| `stopPrice` | اختیاری | قیمت توقف حد ضرر |

**OCO تعهدی:** پارامترهای `mode=oco`, `stopPrice`, `stopLimitPrice` (همان شروط قیمت اسپات). بعد از پر شدن سفارش، موقعیت متناظر باز می‌شود.

**خطاها:** `FeatureUnavailable`, `ParseError`, `TradeLimitation`, `InvalidMarketPair`, `MarketClosed`, `TradingUnavailable`, `UnsupportedMarginSrc`, `MarginClosed`, `AmountUnavailable` (موجودی استخر ناکافی)، `ExceedDlegationLimit`, `InsufficientBalance`, `LeverageTooHigh`, `LeverageUnavailable`, `BadPrice`, `SmallOrder`, `PriceConditionFailed`, `DuplicateOrder`.

### مشاهده لیست موقعیت‌ها — `GET /positions/list`

- محدودیت: ۳۰ درخواست/دقیقه · صفحه‌بندی (پیش‌فرض ۵۰)

| پارامتر | پیش‌فرض | توضیح |
|---|---|---|
| `srcCurrency` / `dstCurrency` | همه | بازار |
| `status` | `active` | `active` (باز) یا `past` (بسته/لیکویید/منقضی) |

**شیٔ Position (مشترک):** `id`, `createdAt`, `srcCurrency`, `dstCurrency`, `side` (`sell`/`buy`)، `status` (`Open`/`Closed`/`Liquidated`/`Expired`)، `marginType` (`Isolated Margin`)، `collateral` (وجه تضمین بلوکه)، `leverage`, `openedAt`, `closedAt`, `liquidationPrice`, `entryPrice`, `exitPrice`.

**ویژه‌ی موقعیت باز:** `delegatedAmount`, `liability` (تعهد)، `totalAsset`, `marginRatio` (نسبت تعهد)، `liabilityInOrder`, `assetInOrder`, `unrealizedPNL`, `unrealizedPNLPercent`, `expirationDate`, `extensionFee`, `markPrice`.

**ویژه‌ی موقعیت بسته:** `PNL`, `PNLPercent`.

**نکات:** نوع تضمین `Isolated Margin` (ضرر حداکثر به‌اندازه‌ی وجه تضمین) یا `Cross Margin` (در آینده). نسبت تعهد اولیه ۲ و حداقل برای لیکویید نشدن ۱.۱ است. هر موقعیت حداکثر ۳۰ روز باز می‌ماند.

### مشاهده یک موقعیت — `GET /positions/:positionId:/status`

- محدودیت: ۱۰۰ درخواست/۱۰ دقیقه (مشترک)

### بستن موقعیت — `POST /positions/:positionId:/close`

- محدودیت: ۳۰۰ درخواست/۱۰ دقیقه — با درج سفارش در جهت عکس، تعهد تسویه می‌شود.

| پارامتر | پیش‌فرض | توضیح |
|---|---|---|
| `execution` | `limit` | `limit`/`market`/`stop_limit`/`stop_market` |
| `amount` | الزامی | مقدار سفارش |
| `price` | الزامی | قیمت سفارش |
| `stopPrice` | اختیاری | قیمت توقف حد ضرر |

**OCO:** `mode=oco`, `stopPrice`, `stopLimitPrice`. شروط مقدار: مجموع سفارش‌های تسویه نباید از تعهد (`liability - liabilityInOrder`) بیشتر شود؛ ارزش کل خرید تسویه نباید از `totalAsset - assetInOrder` بیشتر شود؛ تعهد حداکثر ۱۰ رقم دقت دارد.

**خطاها:** `ParseError`, `TradeLimitation`, `TradingUnavailable`, `NoOpenPosition`, `ExceedLiability`, `ExceedTotalAsset`, `BadPrice`, `SmallOrder`, `PriceConditionFailed`.

### ویرایش وجه تضمین — `POST /positions/:positionId:/edit-collateral`

- محدودیت: ۶۰ درخواست/دقیقه · پارامتر `collateral` (الزامی)
- در سود: مازاد وجه تضمین تا نسبت تعهد اولیه قابل آزادسازی است. نزدیک لیکویید: افزایش وجه تضمین قیمت لیکویید را بهبود می‌دهد.
- **خطاها:** `ParseError`, `TryAgainLater`, `LowMarginRatio`, `InsufficientBalance`.

---

## ۸. برداشت رمزارزی

### ثبت درخواست برداشت — `POST /users/wallets/withdraw`

- محدودیت: ۱۰ درخواست/۳ دقیقه

| پارامتر | نوع | الزام | توضیح |
|---|---|---|---|
| `wallet` | int | الزامی | شناسه‌ی کیف پول |
| `network` | string | اختیاری | شبکه‌ی انتقال |
| `invoice` | string | اختیاری | صورت‌حساب (در BTCLN الزامی) |
| `amount` | monetary | اختیاری | مقدار (بدون `invoice` الزامی) |
| `address` | string | اختیاری | آدرس مقصد |
| `explanations` | string | اختیاری | توضیحات |
| `noTag` | boolean | `false` | بدون تگ |
| `tag` | string | اختیاری | تگ انتقال |

**شبکه‌های مجاز:** `FIAT_MONEY, ETH, BSC, ADA, ALGO, APT, ARB, BCH, BNB, BTC, BTCLN, DOGE, DOT, EOS, ETC, LTC, PMN, TRX, OMNI, ZTRX, XLM, XMR, XRP, ATOM, EGLD, FIL, FLR, FLOW, FTM, MATIC, AVAX, HBAR, NEAR, TON, SOL, XTZ, ONE`.

- **BTCLN:** `invoice` الزامی؛ `amount` و `address` از آن استخراج می‌شوند.
- **شبکه‌های تگ‌اجباری (`BNB, EOS, PMN, XLM, XRP`):** `tag` الزامی است مگر `noTag=true`.
- اگر آدرس مقصد در دفتر آدرس باشد، نیازی به `X-TOTP` نیست؛ در غیر این صورت کد یکبارمصرف ایمیل/پیامک می‌شود و این درخواست نیازمند تایید دوعاملی است.

**خطاها:** `InvalidCurrency`, `WithdrawUnavailable`, `WithdrawCurrencyUnavailable`, `CoinWithdrawDisabled`, `InvalidAddressTag`, `MissingAddressTag`, `ExchangeRequiredTag`, `RedundantTag`, `Invalid2FA`, `WithdrawAmountLimitation`, `InsufficientBalance`, `WithdrawLimitReached`, `AmountTooLow`, `AmountTooHigh`, `InvalidMobileNumber`, `NotWhitelistedTargetAddress`.

### تایید برداشت — `POST /users/wallets/withdraw-confirm`

- محدودیت: ۳۰ درخواست/ساعت

| پارامتر | الزام | توضیح |
|---|---|---|
| `withdraw` | الزامی | شناسه‌ی درخواست برداشت |
| `otp` | بسته به مقصد | رمز یکبارمصرف (برای آدرس‌های امن لازم نیست) |

### مشاهده جزئیات برداشت — `GET /withdraws/WITHDRAW`

- محدودیت: ۶۰ درخواست/۲ دقیقه · پارامتر `WITHDRAW` (شناسه)

```json
{
  "status": "ok",
  "withdraw": {
    "id": 433, "status": "Processing", "amount": "0.123", "currency": "eth",
    "network": "BSC", "address": "...", "wallet_id": 3458,
    "blockchain_url": "https://etherscan.io/tx/...", "is_cancelable": true
  }
}
```

---

## ۹. برداشت ریالی

انتقال وجه ریالی میان حساب‌های بانکی کاربر و حساب کاربری.

### ثبت درخواست — `POST /cobank/withdraw`

- محدودیت: ۱۰ درخواست/۳ دقیقه

| پارامتر | الزام | توضیح |
|---|---|---|
| `destinationBankAccountId` | الزامی | شناسه‌ی حساب بانکی تایید شده |
| `amount` | الزامی | مقدار (ریال) |

**خروجی:** `id` (رشته‌ای با پیشوند `CW` یا `WJ`)، `status`, `amount`, `fee`, `fulfilledAmount`, `bankAccountId`, `bankAccountInfo`, `isCancelable`, `records[]`.

**فیلدهای `records`:** `amount`, `bankReferenceNumber` (کد پیگیری بانک)، `status` (`Pending`/`Failed`/`Transferred`)، `estimatedSettleAt`, `providerUpdatedAt`, `transferType` (`normal`/`paya`/`satna`).

**وضعیت‌های درخواست:** `New` (ثبت توسط کاربر)، `Sent` (ارسال به پروایدر)، `Bank processing`, `Partially Done`, `Done`, `Failed`, `Rejected`, `Canceled`.

**خطاها:** `UnAcceptedDisclaimerError`, `FeatureUnavailable`, `ParseError`, `BankAccountNotFound`, `TooManyRequests`, `WithdrawUnavailable`, `WithdrawAmountLimitation`, `InsufficientBalance`, `AmountTooLow`, `InsufficientBalanceOrInactiveWallet`, `WithdrawLimitReached` (در هر ۲۴ ساعت فقط ۳ برداشت ریالی و ۱۰ رمزارزی)، `AmountTooHigh`, `ShabaWithdrawCannotProceed` (سقف هر شبا ۲ میلیارد ریال).

### لغو درخواست — `POST /cobank/withdraw/<id>/cancel`

- محدودیت: ۶۰ درخواست/ساعت و ۱۰ درخواست/دقیقه
- فقط تا وقتی وضعیت `New` است و کمتر از ۳ دقیقه از ثبت گذشته باشد.
- **خطاها:** `WithdrawRequestNotFound`, `UnAcceptedDisclaimerError`, `CancellationFailed`, `NotCancellable`.

### مشاهده جزئیات — `GET /cobank/withdraw/<id>`

- محدودیت: ۶۰ درخواست/۲ دقیقه · **خطاها:** `WithdrawRequestNotFound`, `UnAcceptedDisclaimerError`.

---

## ۱۰. فهرست برداشت‌ها

### فهرست برداشت‌ها — `GET /users/wallets/withdraws/list`

- محدودیت: ۶۰ درخواست/۲ دقیقه · صفحه‌بندی (پیش‌فرض ۲۰) · فیلتر زمانی
- پارامتر `wallet` (پیش‌فرض `all`). شامل برداشت‌های ریالی و رمزارزی.

```json
{
  "status": "ok",
  "withdraws": [
    {"id": 240, "status": "Done", "amount": "1", "currency": "ltc", "network": "LTC",
     "address": "Lgn1zc77...", "wallet_id": 3454,
     "blockchain_url": "https://live.blockcypher.com/ltc/tx/...", "is_cancelable": false}
  ],
  "hasNext": true
}
```

---

## ۱۱. وب‌سوکت

اطلاعات لحظه‌ای از طریق سرور **Centrifugo** ارائه می‌شود.

- آدرس: `wss://ws.nobitex.ir/connection/websocket`
- محدودیت: حداکثر **۱۰۰ اتصال هم‌زمان** برای هر IP · حداکثر **۴۵۰ کانال** برای هر connection
- توکن برای اتصال اولیه لازم نیست (اما برای کانال‌های `private:` لازم است).

**SDKهای رسمی:** `centrifuge-js` (مرورگر/NodeJS/ReactNative)، `centrifuge-python`, `centrifuge-java` (Android)، `centrifuge-go`, `centrifuge-swift` (iOS)، `centrifuge-dart` (Flutter). غیررسمی: `centrifuge-csharp` (C#/.NET/Unity).

```javascript
import { Centrifuge } from 'centrifuge';
const client = new Centrifuge('wss://ws.nobitex.ir/connection/websocket', {});
client.on('connected', (ctx) => console.log('connected', ctx));
client.connect();
```

بدون SDK، پیام اتصال: `{"connect": {}, "id": 1}`

**Ping/Pong:** سرور هر چند ثانیه پیام `{}` ارسال می‌کند و شما باید با `{}` پاسخ دهید. در صورت عدم پاسخ تا ۲۵ ثانیه، اتصال قطع می‌شود. SDKهای رسمی این کار را خودکار انجام می‌دهند.

**اتصال به چند کانال با یک کلاینت:**

```javascript
const channels = ['public:orderbook-BTCIRT', 'public:orderbook-USDTIRT'];
channels.map(channel => {
  const sub = client.newSubscription(channel, { delta: 'fossil' });
  sub.subscribe();
  sub.on('publication', (ctx) => console.log(channel, ctx.data));
  return sub;
});
```

### احراز هویت و توکن وب‌سوکت

- دریافت توکن: `GET /auth/ws/token` (با هدر `Authorization: Token <ApiToken>`)
- توکن را به‌صورت `{ token: '<CONNECTION_TOKEN>' }` به کلاینت پاس دهید، یا بدون SDK: `{"connect": {"token": "<CONNECTION_TOKEN>"}, "id": 1}`

### انواع کانال‌ها

| نوع | الگو | توضیح |
|---|---|---|
| عمومی | `public:channelName` | بدون احراز هویت |
| خصوصی | `private:channelName#{websocketAuthParam}` | نیازمند توکن + `websocketAuthParam` (از پروفایل) |

> `websocketAuthParam` مقداری **ثابت و یکتا** برای هر کاربر است و نیازی به رفرش ندارد. مثال: `private:trades#1987577cdf7c7422dee369e188e276ee`.

> **فلگ `{ delta: 'fossil' }`** (اختیاری): اطلاعات را به‌صورت diff می‌فرستد و پهنای باند را حدود ۶۰٪ کاهش می‌دهد. با SDK رسمی تفاوتی حس نمی‌کنید؛ بدون SDK یا از آن استفاده نکنید یا الگوریتم بازیابی fossil را پیاده کنید.

### کانال اردربوک — `public:orderbook-{SYMBOL}`

نماد را UPPERCASE قرار دهید (مثل `BTCIRT`). فقط هنگام تغییر اردربوک پیام می‌فرستد؛ در بازارهای کم‌حجم فاصله‌ی پیام‌ها زیاد است، پس ابتدا یک‌بار اردربوک را از API نسخه ۳ بگیرید.

```json
{"asks": [["35077909990", "0.009433"]], "bids": [["35020080080", "0.185784"]], "lastTradePrice": "35077909990", "lastUpdate": 1726581829816}
```

### کانال کندل OHLC — `public:candle-{SYMBOL}-{resolution}`

حدوداً هر ۱۰ ثانیه پابلیش می‌شود. مقادیر `resolution`: همان مقادیر [API تاریخچه](#آمار-ohlc--get-marketudfhistory). مثال: `public:candle-BTCIRT-15`.

```json
{"t": 1731852900, "o": 6240000001.0, "h": 6250000000.0, "l": 6238000000.0, "c": 6238031033.0, "v": 1.26}
```

### کانال عمومی معاملات — `public:trades-{SYMBOL}`

```json
{"price": "120000000000", "time": 1762781164192, "type": "sell", "volume": "0.000003"}
```

### کانال وضعیت بازارها

- تک بازار: `public:market-stats-{SYMBOL}` · همه: `public:market-stats-all`

```json
{"isClosed": false, "bestSell": "121073861950", "bestBuy": "120000000000",
 "volumeSrc": "0.000185", "volumeDst": "21590044.01", "latest": "114879999920",
 "mark": "123288285750", "dayLow": "110850000360", "dayHigh": "121073861950",
 "dayOpen": "121073861910", "dayClose": "114879999920", "dayChange": "-5.12"}
```

(کانال `all` همان ساختار را با کلیدهای `btc-irt`, `usdt-irt`, … برمی‌گرداند.)

### کانال خصوصی سفارشات — `private:orders#{websocketAuthParam}`

نیازمند احراز هویت. فیلدها: `amount`, `avgFilledPrice`, `clientOrderId`, `dstCurrency`, `eventTime`, `fee`, `filledAmount`, `lastFillTime`, `marketType` (`Spot`/`Margin`)، `orderId`, `orderType` (`Market`/`Limit`)، `param1`, `price`, `side` (`Buy`/`Sell`)، `srcCurrency`, `status`, `tradeAmount`, `tradeId`, `tradePrice`.

### کانال خصوصی معاملات — `private:trades#{websocketAuthParam}`

نیازمند احراز هویت. به‌محض هر معامله، اطلاعات منتشر می‌شود.

```json
{"srcCurrency": "btc", "dstCurrency": "rls", "timestamp": "2024-11-23T11:31:27+00:00",
 "price": "66683959340", "amount": "0.000404", "total": "26940319.57", "type": "sell",
 "fee": "47150.79", "id": 12942226, "orderId": 520305923}
```

---

## ۱۲. دفتر آدرس و حالت برداشت امن

برداشت به آدرس‌های امن دفتر آدرس به رمز دوعاملی یا OTP نیازی ندارد.

### لیست آدرس‌ها — `GET /address_book`

- محدودیت: ۲۰ درخواست/دقیقه · پارامتر `network` (فیلتر، اختیاری)

```json
{"status": "ok", "data": [{"id": 3, "title": "TetherBinance", "network": "BSC", "address": "000...zzz", "createdAt": "2023-08-09T10:12:37+00:00"}]}
```

### افزودن آدرس — `POST /address_book`

- محدودیت: ۶ درخواست/دقیقه

| پارامتر | الزام | توضیح |
|---|---|---|
| `title` | الزامی | عنوان آدرس |
| `network` | الزامی | شبکه |
| `address` | الزامی | آدرس |
| `tag` | الزامی در شبکه‌های تگ‌اجباری | تگ |
| `otpCode` | الزامی | کد تایید ایمیل/پیامک |
| `tfaCode` | الزامی | کد دوعاملی |

در شبکه‌های `BNB, EOS, PMN, XLM, XRP` تگ اجباری است و زوج (آدرس، تگ) باید یکتا باشد. **خطاها:** `ParseError`, `InvalidOTP`, `Invalid2FA`, `Inactive2FA`, `InvalidAddress`, `DuplicatedAddress`, `InvalidTag`.

### حذف آدرس — `DELETE /address_book/<address_id>/delete`

- محدودیت: ۶ درخواست/دقیقه · خطا: `NotFound`.

### برداشت امن (Whitelist)

| عملیات | درخواست | محدودیت | توضیح |
|---|---|---|---|
| فعال‌سازی | `POST /address_book/whitelist/activate` | ۶/دقیقه | برداشت فقط به آدرس‌های دفتر آدرس (به‌جز شبکه‌ی لایتنینگ) |
| غیرفعال‌سازی | `POST /address_book/whitelist/deactivate` | ۶/دقیقه | نیازمند `otpCode` + `tfaCode`؛ برداشت تا ۲۴ ساعت محدود می‌شود |

---

## ۱۳. امنیت

### سابقه ورود — `GET /users/login-attempts`

```json
{"status": "ok", "attempts": [{"ip": "46.209.130.106", "username": "name@example.com", "status": "Successful", "createdAt": "2018-11-28T14:16:08+00:00"}]}
```

### فعال‌سازی لغو اضطراری — `GET /security/emergency-cancel/activate`

پس از فعال‌سازی، ایمیل/پیامک برداشت حاوی لینکی برای لغو فوری بدون نیاز به لاگین خواهد بود.

```json
{"status": "ok", "cancelCode": {"code": "seJlef35L3"}}
```

> اگر برداشتی با این امکان لغو شود، ثبت درخواست برداشت تا ۷۲ ساعت غیرفعال می‌شود.

### آنتی‌فیشینگ

کد یکتایی که در همه‌ی ایمیل‌های خودکار نوبیتکس درج می‌شود تا اصالت ایمیل تایید گردد.

| عملیات | درخواست | محدودیت |
|---|---|---|
| ایجاد | `POST /security/anti-phishing` | ۱۰/دقیقه |
| دریافت | `GET /security/anti-phishing` | ۱۰/دقیقه |

پارامترهای ایجاد: `code` (۴ تا ۱۵ کاراکتر)، `otpCode`. **خطاها:** `ParseError`, `InvalidOTPCode`, `InvalidCodeLength`, `NotFound`.

---

## ۱۴. طرح معرفی دوستان

با کد دعوت، درصدی از کارمزد معاملات دعوت‌شدگان را دریافت می‌کنید. پیوند: `https://nobitex.ir/signup/?refcode=CODE`. هر کاربر تا ۳۰ کد می‌تواند بسازد.

### فهرست کدهای دعوت — `POST /users/referral/links-list`

- محدودیت: ۵۰ درخواست/۱۰ دقیقه

**شیٔ ReferralLink:** `id`, `referralCode`, `createdAt`, `userShare` (سهم معرف)، `friendShare` (سهم دعوت‌شده)، `description`, `statsRegisters`, `statsTrades`, `statsProfit` (ریال).

### ایجاد کد دعوت — `POST /users/referral/links-add`

- محدودیت: ۵ درخواست/دقیقه · پارامتر `friendShare` (پیش‌فرض ۰)
- **خطاها:** `InvalidGivebackShare`, `TooManyReferralLinks`, `ReferralCodeUnavailable`, `ReferralCodeExists`.

### وضعیت دعوت — `POST /users/referral/referral-status`

- محدودیت: ۵۰ درخواست/۱۰ دقیقه · خروجی: `hasReferrer` (boolean)

### ثبت معرف — `POST /users/referral/set-referrer`

- محدودیت: ۵۰ درخواست/۱۰ دقیقه · پارامتر `referrerCode` (الزامی) · تا ۲۴ ساعت پس از ثبت‌نام
- خطا: `ReferrerChangeUnavailable`.

---

## ۱۵. احراز هویت (دریافت خودکار توکن)

> **روش پیشنهادی:** دریافت توکن از پنل کاربری (پروفایل → تنظیمات). دریافت خودکار فقط در صورت آشنایی با مخاطرات ذخیره‌ی گذرواژه در کد توصیه می‌شود.

### ورود — `POST /auth/login/`

- محدودیت: ۳۰ درخواست/۱۰ دقیقه · **دقت کنید انتهای URL حتماً `/` داشته باشد.**

| پارامتر | الزام | توضیح |
|---|---|---|
| `username` | الزامی | ایمیل کاربر |
| `password` | الزامی | گذرواژه |
| `remember` | پیش‌فرض `no` | `yes` برای توکن ۳۰ روزه |
| `captcha` | الزامی | برابر `api` |

| هدر | الزام | توضیح |
|---|---|---|
| `X-TOTP` | الزامی | کد دوعاملی |
| `User-Agent` | الزامی | `TraderBot/XXXXX` |

```json
{"status": "success", "key": "db2055f743c1ac8c30d23278a496283b1e2dd46f", "device": "AlRyansW"}
```

**نکات:** توکن پیش‌فرض ۴ ساعت معتبر است (با `remember=yes` تا یک ماه). برای لاگین حتماً از **IP ایران** درخواست بفرستید؛ در غیر این صورت خطای 429 برمی‌گردد (VPN/VPS خارجی کار نمی‌کند). در حالت IP ایران `captcha=api` کافی است.

**خطاهای رایج:** `MissingCaptcha` (از پنل توکن بگیرید یا `captcha=api`)، `MissingOTP` (کد دوعاملی را در هدر `X-TOTP` بفرستید). کاربران گوگل: از «فراموشی رمز» رمز تعیین کنید.

### خروج — `POST /auth/logout/`

با هدر `Authorization: Token ...`. توکن سوزانده می‌شود.

---

## ۱۶. سود و زیان (پرتفو)

> این ویژگی به‌صورت **بتا** عرضه شده است. خطای غیرفعال بودن: `PortfolioDisabled` · نبود داده: `LastWeekDailyProfitFail`.

| API | درخواست | محدودیت |
|---|---|---|
| سود و زیان روزانه‌ی هفته‌ی گذشته | `POST /users/portfolio/last-week-daily-profit` | ۱۰/۳ دقیقه |
| سود و زیان کل روزانه‌ی هفته‌ی گذشته | `POST /users/portfolio/last-week-daily-total-profit` | ۱۰/۳ دقیقه |
| سود و زیان کل ماه گذشته | `POST /users/portfolio/last-month-total-profit` | ۱۰/۳ دقیقه |

برای `last-week-daily-profit`، با ارسال `monthly=true` اطلاعات ۳۰ روز گذشته برمی‌گردد. خروجی شامل `report_date`, `total_profit`, `total_profit_percentage`, `total_balance` است.

```json
{"status": "ok", "data": {"total_profit": "8987787000.00", "total_profit_percentage": "83.60"}}
```

---

## ۱۷. سایر (تنظیمات سیستم)

### تنظیمات سیستم — `GET /v2/options`

توکن لازم ندارد. پارامترهای کلی سیستم را برمی‌گرداند: رمزارزهای فعال، حداقل معامله، پله‌های کارمزد، سقف برداشت و … در دو کلید اصلی `features`, `nobitex` و کلید `coins`.

**`features`:** `fcmEnabled`, `autoKYC`, `enabledFeatures` (مثل `PriceAlert`, `StopLoss`, `GiftCard`, `OCO`)، `betaFeatures`.

**`nobitex`:** `allCurrencies`, `activeCurrencies`, `xchangeCurrencies`, `topCurrencies`, `testingCurrencies`, `withdrawLimits` (به‌تفکیک سطح)، `minOrders` (`rls: 3000000`, `usdt: 11`)، `amountPrecisions`, `pricePrecisions`, `giftCard`.

**`coins[]`:** هر رمزارز شامل `coin`, `name`, `defaultNetwork`, `displayPrecision`, `stdName` و `networkList`.

**شیٔ Network:** `network`, `name`, `isDefault`, `beta`, `addressRegex`, `memoRequired`, `memoRegex`, `depositEnable`, `minConfirm`, `depositInfo` (شامل `depositMin`/`depositMax`)، `withdrawEnable`, `withdrawIntegerMultiple`, `withdrawFee`, `withdrawMin`, `withdrawMax`.

> رمزارز بیبی‌دوج به‌دلایل فنی در خروجی این API لیست نمی‌شود.

---

## ۱۸. ملاحظات عمومی

### راهنمای اشکال‌یابی
- به نوع متد (GET/POST) دقت کنید.
- آدرس و وجود/عدم وجود `/` انتهایی را بررسی کنید.

### پاسخ موفق

```json
{"status": "ok", "otherFields": "..."}
```

### پاسخ ناموفق

```json
{"status": "failed", "code": "ErrorCode", "message": "Human-readable error message"}
```

برای نمایش خطا به کاربر از `code` استفاده کنید (نه `message` که فقط برای دیباگ است).

### کدهای HTTP پرکاربرد

| کد | عنوان | توضیح |
|---|---|---|
| 200 | OK | پاسخ داده شده؛ وضعیت اصلی در فیلد `status` |
| 400 | Bad Request | پارامترهای نادرست/ناکافی |
| 401 | Unauthorized | احراز هویت نشده |
| 403 | Forbidden | عملیات مجاز نیست |
| 404 | Not Found | آدرس/شیٔ موجود نیست |
| 422 | Unprocessable Content | پارامتر درست اما معنایی غیرقابل انجام |
| 429 | Too Many Requests | تعداد درخواست بیش از حد |
| 500 | Internal Server Error | خطای موقت سرور |

### صفحه‌بندی

پارامترهای `page` و `pageSize` (مقادیر ۱ تا ۱۰۰؛ پیش‌فرض عمومی ۵۰ در صفحه).

### فیلتر زمانی

پارامترهای `from` و `to` (تاریخ، مثل `2022-05-12`).

### مقادیر پولی (monetary)

برای بالاترین دقت، مقادیر مالی را به‌صورت **رشته** ارسال کنید (نه `float`).

### اعتبارسنجی دوعاملی

در صورت فعال بودن 2FA، رمز یکبارمصرف را در هدر `X-TOTP: 123456` بفرستید.

### محدودیت فراخوانی

```json
{"status": "failed", "code": "TooManyRequests", "message": "...", "backOff": 12, "limit": 60}
```

به اندازه‌ی `backOff` (ثانیه) صبر کنید؛ `limit` سقف مجاز است. نادیده گرفتن مکرر → مسدودی توکن به مدت ۲ دقیقه. در زمان ازدحام بازار ممکن است محدودیت کاهش یابد.

**محدودیت مشترک سفارش‌گذاری:** مجموع APIهای ثبت سفارش (اسپات + تعهدی) = **۳۰۰ درخواست/۱۰ دقیقه**.

**محدودیت احراز ناموفق:** بیش از ۱۰۰ درخواست با توکن اشتباه/منقضی از یک IP در کمتر از ۳۰ دقیقه → مسدودی آن IP تا پایان بازه.

### حالت Pro

با ارسال `pro=yes` برخی محدودیت‌های محافظتی غیرفعال می‌شوند. فقط در صورت آگاهی از تبعات استفاده کنید.

---

## ۱۹. سوالات متداول

- **لاگین و توکن:** ابتدا بخش احراز هویت و روش پیشنهادی را مطالعه کنید.
- **محیط آزمایشی:** بله — سایت `https://testnet.nobitex.ir` و API `https://testnetapiv2.nobitex.ir`.
- **محدودیت‌ها:** بله — حتماً از IP ایران درخواست بفرستید؛ محدودیت‌های اختصاصی هر endpoint نیز اعمال می‌شود.
- **فرمت تاریخ:** Unix Time (ثانیه از ۱ ژانویه ۱۹۷۰ UTC، بدون ثانیه‌ی کبیسه).
- **کد نمونه:** کالکشن Postman نوبیتکس شامل نمونه فراخوانی به PHP، Python، C# و … است.

---

## پیوست: لیست نمادهای معتبر بازار

این لیست برای APIهای اردربوک، نمودار عمق، لیست معاملات، OHLC و بازارهای مورد علاقه مشترک است:

```
BTCIRT, ETHIRT, LTCIRT, USDTIRT, XRPIRT, BCHIRT, BNBIRT, EOSIRT, XLMIRT, ETCIRT,
TRXIRT, DOGEIRT, UNIIRT, DAIIRT, LINKIRT, DOTIRT, AAVEIRT, ADAIRT, SHIBIRT, FTMIRT,
MATICIRT, AXSIRT, MANAIRT, SANDIRT, AVAXIRT, MKRIRT, GMTIRT, USDCIRT, BTCUSDT, ETHUSDT,
LTCUSDT, XRPUSDT, BCHUSDT, BNBUSDT, EOSUSDT, XLMUSDT, ETCUSDT, TRXUSDT, PMNUSDT, DOGEUSDT,
UNIUSDT, DAIUSDT, LINKUSDT, DOTUSDT, AAVEUSDT, ADAUSDT, SHIBUSDT, FTMUSDT, MATICUSDT, AXSUSDT,
MANAUSDT, SANDUSDT, AVAXUSDT, MKRUSDT, GMTUSDT, USDCUSDT, CHZIRT, GRTIRT, CRVIRT, BANDUSDT,
COMPUSDT, EGLDIRT, HBARUSDT, GALIRT, HBARIRT, WBTCUSDT, IMXIRT, WBTCIRT, ONEIRT, GLMUSDT,
ENSIRT, 1M_BTTIRT, SUSHIIRT, LDOIRT, ATOMUSDT, ZROIRT, STORJIRT, ANTIRT, AEVOUSDT, 100K_FLOKIIRT,
RSRUSDT, API3USDT, GLMIRT, XMRIRT, ENSUSDT, OMIRT, RDNTIRT, MAGICUSDT, TIRT, ATOMIRT,
NOTIRT, CVXIRT, XTZIRT, FILIRT, UMAIRT, 1B_BABYDOGEIRT, BANDIRT, SSVIRT, DAOIRT, BLURIRT,
ONEUSDT, EGALAUSDT, GMXIRT, XTZUSDT, FLOWUSDT, GALUSDT, WIRT, CVCUSDT, NMRUSDT, SKLIRT,
SNTIRT, BATUSDT, TRBUSDT, NMRIRT, RDNTUSDT, API3IRT, CVCIRT, WLDIRT, YFIUSDT, SOLIRT,
TUSDT, QNTUSDT, IMXUSDT, AEVOIRT, GMXUSDT, ETHFIUSDT, QNTIRT, GRTUSDT, WLDUSDT, FETIRT,
AGIXIRT, NOTUSDT, LPTIRT, SLPIRT, MEMEUSDT, SOLUSDT, BALUSDT, DAOUSDT, COMPIRT, MEMEIRT,
TONUSDT, BATIRT, SNXIRT, TRBIRT, 1INCHUSDT, OMUSDT, RSRIRT, RNDRIRT, SLPUSDT, SSVUSDT,
RNDRUSDT, AGLDIRT, NEARUSDT, WOOUSDT, YFIIRT, MDTIRT, CRVUSDT, MDTUSDT, EGLDUSDT, LRCIRT,
LPTUSDT, BICOUSDT, 1M_PEPEIRT, BICOIRT, MAGICIRT, ETHFIIRT, ANTUSDT, 1INCHIRT, APEUSDT, 1M_NFTIRT,
ARBIRT, LRCUSDT, WUSDT, BLURUSDT, CELRUSDT, DYDXIRT, CVXUSDT, BALIRT, TONIRT, 100K_FLOKIUSDT,
JSTUSDT, ZROUSDT, ARBUSDT, APTIRT, 1M_NFTUSDT, CELRIRT, UMAUSDT, SKLUSDT, ZRXUSDT, AGLDUSDT,
ALGOIRT, NEARIRT, APTUSDT, ZRXIRT, SUSHIUSDT, FETUSDT, ALGOUSDT, 1M_PEPEUSDT, MASKIRT, EGALAIRT,
FLOWIRT, 1B_BABYDOGEUSDT, MASKUSDT, 1M_BTTUSDT, STORJUSDT, XMRUSDT, OMGIRT, SNTUSDT, APEIRT, FILUSDT,
ENJUSDT, OMGUSDT, WOOIRT, CHZUSDT, ENJIRT, DYDXUSDT, AGIXUSDT, JSTIRT, LDOUSDT, SNXUSDT
```

**انواع ارز (برای `market/stats`):** `rls, btc, eth, ltc, usdt, xrp, bch, bnb, eos, xlm, etc, trx, pmn, doge, uni, dai, link, dot, aave, ada, shib, ftm, matic, axs, mana, sand, avax, mkr, gmt, atom, uma, w, rsr, wld, 1m_nft, flow, agld, ton, mask, snt, agix, algo, ssv, band, omg, comp, zrx, rdnt, imx, 1inch, mdt, sushi, bico, gmx, zro, bal, dao, gal, not, nmr, xmr, enj, apt, lrc, dydx, grt, near, cvx, 100k_floki, fil, sol, ldo, crv, aevo, qnt, om, woo, storj, ant, 1m_btt, magic, ape, rndr, hbar, lpt, glm, blur, wbtc, meme, ethfi, egala, arb, fet, skl, cvc, snx, jst, ens, trb, chz, xtz, api3, slp, t, bat, 1b_babydoge, celr, yfi, egld, one, 1m_pepe, usdc`

---

## منابع رسمی

- [مخزن گیت‌هاب مستندات](https://github.com/nobitex)
- کالکشن Postman نوبیتکس
- [سایت نوبیتکس](https://nobitex.ir)
- کانال رسمی تلگرام نوبیتکس و کانال رسمی تلگرام API
