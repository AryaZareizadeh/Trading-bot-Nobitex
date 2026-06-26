## 13. Address Book

The address book and "secure withdrawal" (whitelist) mode improve crypto-withdrawal security and speed by letting you pre-register and verify withdrawal addresses. Withdrawing to a whitelisted address book entry skips 2FA / OTP verification.

### List Address Book Entries

**Endpoint:** `GET /address_book`
**Rate limit:** 20 requests/minute

```shell
curl 'https://apiv2.nobitex.ir/address_book' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
  "status": "ok",
  "data": [
    { "id": 3, "title": "TetherBinance", "network": "BSC", "address": "000000xxxxxxx111111111zzzzzzz", "createdAt": "2023-08-09T10:12:37+00:00" },
    { "id": 4, "title": "BinanceCoinOKX", "network": "BNB", "address": "000000xxxxxxx222222222zzzzzzz", "tag": "test17280992", "createdAt": "2023-08-09T10:26:12+00:00" }
  ]
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `network` | string | all networks | Filter by network | `BSC` |

### Add a New Address

**Endpoint:** `POST /address_book`
**Rate limit:** 6 requests/minute

```shell
curl -X POST 'https://apiv2.nobitex.ir/address_book' \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"title": "test", "network": "BSC", "address": "000000xxxxxxx111111111zzzzzzz",
    "otpCode": "123456", "tfaCode": "654321"}'
```

Example response:
```json
{
  "status": "ok",
  "data": {
    "id": 5,
    "title": "test",
    "network": "BSC",
    "address": "000000xxxxxxx111111111zzzzzzz",
    "createdAt": "2023-08-09T10:22:37+00:00"
  }
}
```

To get the `otpCode` (sent via email):
```shell
curl -X POST 'https://apiv2.nobitex.ir/v2/otp/request' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000' \
  --data '{"type": "email", "usage": "address_book"}'
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `title` | string | required | Display name for this address | `test` |
| `network` | string | required | Network | `BSC` |
| `address` | string | required | Address | `000000xxxxxxx111111111zzzzzzz` |
| `tag` | string | required on tag-required networks | Tag | `test17280992` |
| `otpCode` | string | required | Email/SMS verification code | `123456` |
| `tfaCode` | string | required | 2FA code | `654321` |

**Notes**
1. The address can't be duplicated on tag-less networks.
2. Tag-required networks require a `tag`.
3. On tag-required networks, the (address, tag) pair must be unique.
4. Use the request above to get the `otpCode` via email.

**Error responses**

| Error code | Description |
|---|---|
| `ParseError` | A required/typed input parameter is invalid |
| `InvalidOTP` | Invalid `otpCode` |
| `Invalid2FA` | Invalid `tfaCode` |
| `Inactive2FA` | 2FA isn't enabled |
| `InvalidAddress` | Address isn't valid for the given network |
| `DuplicatedAddress` | This address is already registered |
| `InvalidTag` | Tag format doesn't match this network |

### Delete an Address

**Endpoint:** `DELETE /address_book/<address_id>/delete`
**Rate limit:** 6 requests/minute

```shell
curl -X DELETE 'https://apiv2.nobitex.ir/address_book/<address_id>/delete' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Response: `{ "status": "ok" }`

**Error responses**

| Error code | Description |
|---|---|
| `NotFound` | No address with that ID |

### Enable Secure Withdrawal (Whitelist Mode)

When secure withdrawal mode is enabled, crypto withdrawals are restricted to addresses in your address book — withdrawals to other addresses are blocked, except on the Lightning network.

**Endpoint:** `POST /address_book/whitelist/activate`
**Rate limit:** 6 requests/minute

```shell
curl -X POST 'https://apiv2.nobitex.ir/address_book/whitelist/activate' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Response: `{ "status": "ok" }`

### Disable Secure Withdrawal

For account security, disabling secure withdrawal mode imposes a 24-hour withdrawal restriction.

**Endpoint:** `POST /address_book/whitelist/deactivate`
**Rate limit:** 6 requests/minute

```shell
curl -X POST 'https://apiv2.nobitex.ir/address_book/whitelist/deactivate' \
  -H "Authorization: Token yourTOKENhereHEX0000000000" \
  -H "content-type: application/json" \
  --data '{"otpCode": "1234", "tfaCode": "12345"}'
```

Response: `{ "status": "ok" }`

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `otpCode` | string | required | Email/SMS verification code | `1234` |
| `tfaCode` | string | required | 2FA code | `12345` |

**Error responses**

| Error code | Description |
|---|---|
| `ParseError` | A required/typed input parameter is invalid |
| `InvalidOTP` | Invalid `otpCode` |
| `Invalid2FA` | Invalid `tfaCode` |
| `Inactive2FA` | 2FA isn't enabled |

---

## 14. Security (Login, 2FA, Sessions)

### Login History

**Endpoint:** `GET /users/login-attempts`

```shell
curl 'https://apiv2.nobitex.ir/users/login-attempts' \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{
    "status": "ok",
    "attempts": [
        {
            "ip": "46.209.130.106",
            "username": "name@example.com",
            "status": "Successful",
            "createdAt": "2018-11-28T14:16:08.264308+00:00"
        }
    ]
}
```

### Activate Emergency Withdrawal Cancellation

**Endpoint:** `GET /security/emergency-cancel/activate`

```shell
curl 'https://apiv2.nobitex.ir/security/emergency-cancel/activate' \
  -X GET \
  -H "Authorization: Token yourTOKENhereHEX0000000000"
```

Example response:
```json
{ "status": "ok", "cancelCode": { "code": "seJlef35L3" } }
```

Activates emergency cancellation for withdrawal requests. Once enabled, the SMS/email sent after a withdrawal request includes a link that lets you instantly cancel that withdrawal — without logging in — in case you didn't actually initiate it.

**Notes**

If a withdrawal is cancelled this way, withdrawal requests are disabled on your account for 72 hours.

### Anti-Phishing Code

To increase trust in emails sent from Nobitex, users can set a unique code that will appear in every automated Nobitex email — confirming the email genuinely came from Nobitex.

#### Set Anti-Phishing Code

**Endpoint:** `POST /security/anti-phishing`
**Rate limit:** 10 requests/minute

```shell
curl --location --request POST 'https://apiv2.nobitex.ir/security/anti-phishing' \
--header 'Authorization: Token yourTOKENhereHEX0000000000' \
--form 'code="sample_anti_phishing"' \
--form 'otpCode="123456"'
```

Response: `{ "status": "ok" }`

To get the `otpCode`:
```shell
curl -X POST 'https://apiv2.nobitex.ir/v2/otp/request' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000' \
  --data '{"type": "email", "usage": "anti_phishing_code"}'
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `code` | string | required | User-chosen anti-phishing code | `sample_anti_phishing` |
| `otpCode` | number | required | OTP sent to the user's phone | `12345` |

**Error responses**

```json
{ "status": "failed", "code": "ParseError", "message": "Missing string value" }
```
(missing `code` or `otpCode`)

```json
{ "status": "failed", "code": "InvalidOTPCode", "message": "Please use otp/request endpoint" }
```
(invalid OTP)

```json
{ "status": "failed", "code": "InvalidCodeLength", "message": "Code length must be between 4 and 15 characters" }
```

#### Get Anti-Phishing Code

**Endpoint:** `GET /security/anti-phishing`
**Rate limit:** 10 requests/minute

```shell
curl --location --request GET 'https://apiv2.nobitex.ir/security/anti-phishing' \
--header 'Authorization: Token yourTOKENhereHEX0000000000'
```

Example response:
```json
{ "status": "ok", "antiPhishingCode": "s*********g" }
```

If no anti-phishing code is set:
```json
{ "status": "failed", "code": "NotFound", "message": "AntiPhishingCode is not declared for this user" }
```

---

## 15. Referral Program

Nobitex's referral program lets you invite friends with your own link and earn a share of their trading fees. See the [referral program terms](https://nobitex.ir/policies/referral/) for details.

Referrals work through "invite codes." Each user can generate one or more invite codes (typically 6 digits, but length/format can vary). When creating a code you can set how the trading-fee rebate is split between the referrer and the referred friend.

Use "List Invite Codes" to see your current codes plus stats. If you don't have one yet, use "Create Invite Code."

> To build an invite link from a code, use: `https://nobitex.ir/signup/?refcode=CODE`

### List Invite Codes

**Endpoint:** `POST /users/referral/links-list`
**Rate limit:** 50 requests / 10 minutes

```shell
curl -X GET 'https://apiv2.nobitex.ir/users/referral/links-list' \
  --header 'Authorization: Token yourTOKENhereHEX0000000000'
```

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `links` | list of ReferralLink | Your invite codes | `[{...ReferralLink...}, ...]` |

**ReferralLink object**

| Field | Type | Description | Example |
|---|---|---|---|
| `id` | int | Link ID | `1001` |
| `referralCode` | string | Invite code | `40404` |
| `createdAt` | datetime | Created at | `2020-07-15T11:32:38.326809+00:00` |
| `userShare` | int | Referrer's share of the referred user's trading fees (%) | `20` |
| `friendShare` | int | Referred friend's share of their own fee rebate (%) | `10` |
| `description` | string | Optional note | `Shared on Instagram page X` |
| `statsRegisters` | int | Users registered via this code | `20` |
| `statsTrades` | int | Total trades by users registered via this code | `240` |
| `statsProfit` | monetary (IRR) | Total IRR earnings from this code | `3200000` |

### Create Invite Code

**Endpoint:** `POST /users/referral/links-add`
**Rate limit:** 5 requests/minute

```shell
curl -X GET 'https://apiv2.nobitex.ir/users/get-referral-code' \
--header 'Authorization: Token yourTOKENhereHEX0000000000'
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `friendShare` | int | `0` | Fee-rebate share given to the referred friend (%) | `10` |

**Error responses**

| Error code | Description |
|---|---|
| `InvalidGivebackShare` | Invalid `friendShare` value |
| `TooManyReferralLinks` | Reached the 30-code-per-user limit |
| `ReferralCodeUnavailable` | Creating invite codes isn't currently available |
| `ReferralCodeExists` | An error occurred while saving the code |

### Referral Status

**Endpoint:** `POST /users/referral/referral-status`
**Rate limit:** 50 requests / 10 minutes

```shell
curl -X GET 'https://apiv2.nobitex.ir/users/referral/referral-status' \
--header 'Authorization: Token yourTOKENhereHEX0000000000'
```

Check whether the current user was referred by someone else.

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `hasReferrer` | boolean | Was this user referred? | `true` |

### Set Referrer

**Endpoint:** `POST /users/referral/set-referrer`
**Rate limit:** 50 requests / 10 minutes

```shell
curl -X GET 'https://apiv2.nobitex.ir/users/referral/set-referrer' \
  --header 'Authorization: Token yourTOKENhereHEX0000000000'
```

An invite code is normally captured at signup (manually or via the referral link). This endpoint lets a user set their referrer up to 24 hours after registering.

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `referrerCode` | string | required | The referrer's invite code | `40404` |

**Error responses**

| Error code | Description |
|---|---|
| `ReferrerChangeUnavailable` | More than 24 hours have passed since registration — referrer can no longer be set |

---

