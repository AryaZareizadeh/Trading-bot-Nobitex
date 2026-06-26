## 9. Crypto Withdrawals

### Submit a Crypto Withdrawal Request

**Endpoint:** `POST /users/wallets/withdraw`
**Rate limit:** 10 requests / 3 minutes

```shell
curl -X POST 'https://apiv2.nobitex.ir/users/wallets/withdraw' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000' \
  -H 'X-TOTP: 123456' \
  -H 'Content-Type: application/json' \
  --data '{"wallet": 3456, "invoice": "lnbc123m1pskcu80pp5qqqsyqcyq5rqwz..."}'
```

Example response:
```json
{
  "status": "ok",
  "withdraw": {
    "id": 432,
    "createdAt": "2021-12-11T10:13:42.957103+00:00",
    "status": "New",
    "amount": "0.0123",
    "currency": "btc",
    "network": "BTCLN",
    "invoice": "lnbc123m1pskcu80pp5qqqsyqcyq5rqwz...",
    "address": "SaMpLeWaLlEtAdDrEsS",
    "tag": "123456",
    "wallet_id": 3456,
    "blockchain_url": "https://nobitex.ir/receipt/Bitcoin/ewd23d...",
    "is_cancelable": true
  }
}
```

> If the destination address is already in your address book, you don't need to send `X-TOTP` in the request header.
>
> If the destination address is **not** in your address book, a one-time verification code is sent to you by email and SMS after the withdrawal request is created.

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `wallet` | int | required | Wallet ID | `3456` |
| `network` | string | optional | Transfer network<sup>1</sup> | `"BTCLN"` |
| `invoice` | string | optional | Invoice<sup>2</sup> | `"lnbc123m1pskcu80pp5qqqsyqcyq5rqwz..."` |
| `amount` | monetary | optional | Amount<sup>3</sup> | `"0.123"` |
| `address` | string | optional | Destination address<sup>4</sup> | `"SaMpLeWaLlEtAdDrEsS"` |
| `explanations` | string | optional | Free-form note | `"sample explanation"` |
| `noTag` | boolean | `false` | No tag<sup>5</sup> | `false` |
| `tag` | string | optional | Transfer tag<sup>6</sup> | `"123456"` |

1. **Network:** must be one of the supported networks for the requested currency (when multiple networks exist).
2. **Invoice:** required for `BTCLN` (Lightning) network transfers. If `invoice` is set, `amount`/`address` are derived from it and the corresponding body fields are ignored. The invoice's network must match `network` if both are sent.
3. **Amount:** required when not using an invoice.
4. **Destination address:** the target wallet address; ignored when using an invoice.
5. **No tag:** for networks that normally require a transfer tag, set `noTag: true` if you explicitly want to withdraw without one.
6. **Transfer tag:** required on networks that need a tag, unless `noTag` is set. ([What is a memo/tag/comment?](https://nobitex.ir/mag/what-is-memo/))

**Error responses**

If the withdrawal request is invalid, one of the following errors may be returned — in every case, the request was **not** registered, and you'll need to resubmit if desired.

| Error code | Description |
|---|---|
| `InvalidCurrency` | This crypto market is disabled |
| `WithdrawUnavailable` | User has withdrawal restrictions |
| `WithdrawCurrencyUnavailable` | Withdrawals for this currency/network aren't available |
| `<Coin>WithdrawDisabled` | Withdrawals for this currency are temporarily disabled, e.g. `BTCWithdrawDisabled` |
| `InvalidAddressTag` | Tag is invalid |
| `MissingAddressTag` | Tag missing or incorrect |
| `ExchangeRequiredTag` | A tag is required for this market |
| `RedundantTag` | An unnecessary tag was sent |
| `Invalid2FA` | The 2FA code sent was incorrect |
| `WithdrawAmountLimitation` | Requested amount exceeds your limits (daily/monthly, etc.) |
| `InsufficientBalance` | Insufficient balance |
| `WithdrawLimitReached` | Too many requests with the same status currently pending |
| `AmountTooLow` | Below the minimum amount for this market/network |
| `AmountTooHigh` | Above the maximum amount for this market/network |
| `InvalidMobileNumber` | User has no verified mobile number |
| `NotWhitelistedTargetAddress` | Secure-withdrawal mode is on, but this network/address (or tag) isn't in your address book |

**Notes**
1. If the destination address isn't in your address book, this request requires [two-factor verification](#14-security-login-2fa-sessions).
2. The requesting user must be eligible to withdraw from their wallet (minimum verification level, verified phone number, daily/monthly limits not exceeded, and other conditions per Nobitex's terms).
3. If secure-withdrawal mode is enabled, the chosen network/address (and tag, if required) must already be saved in your address book.
4. A withdrawal request created here still needs confirmation (see below).

### Confirm a Crypto Withdrawal Request

**Endpoint:** `POST /users/wallets/withdraw-confirm`
**Rate limit:** 30 requests/hour

```shell
curl -X POST 'https://apiv2.nobitex.ir/users/wallets/withdraw-confirm' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000' \
  -H 'Content-Type: application/json' \
  --data '{"withdraw": 432, "otp": 623005}'
```

Example response:
```json
{
  "status": "ok",
  "withdraw": {
    "id": 432,
    "createdAt": "2021-12-11T10:13:42.957103+00:00",
    "status": "Verified",
    "amount": "0.0123",
    "currency": "btc",
    "network": "BTCLN",
    "invoice": "lnbc123m1pskcu80pp5qqqsyqcyq5rqwz...",
    "address": "SaMpLeWaLlEtAdDrEsS",
    "tag": "123456",
    "wallet_id": 3456,
    "blockchain_url": "https://nobitex.ir/receipt/Bitcoin/ewd23d...",
    "is_cancelable": true
  }
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `withdraw` | int | required | Withdrawal request ID | `432` |
| `otp` | int | required depending on destination address | OTP code sent to you | `623005` |

**Notes**
1. No OTP is needed when withdrawing to a whitelisted address book entry.
2. To confirm a withdrawal to an unverified address, retrieve the OTP sent to your mobile/email and submit it here.
3. Dedicated-IP support is coming soon.

### Get Crypto Withdrawal Details

**Endpoint:** `GET /withdraws/<WITHDRAW>`
**Rate limit:** 60 requests / 2 minutes

```shell
curl 'https://apiv2.nobitex.ir/withdraws/433' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000'
```

Example response:
```json
{
  "status": "ok",
  "withdraw": {
    "id": 433,
    "createdAt": "2021-12-14T06:53:11.593812+00:00",
    "status": "Processing",
    "amount": "0.123",
    "currency": "eth",
    "network": "BSC",
    "invoice": null,
    "address": "SaMpLeWaLlEtAdDrEsS",
    "tag": null,
    "wallet_id": 3458,
    "blockchain_url": "https://etherscan.io/tx/tx111111",
    "is_cancelable": true
  }
}
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `WITHDRAW` | int | required | Withdrawal request ID (path param) | `433` |

---

## 10. Rial (IRR) Withdrawals

These endpoints are specifically for IRR withdrawals — moving Rial balance from your Nobitex account to your bank account.

### Submit a Rial Withdrawal Request

**Endpoint:** `POST /cobank/withdraw`
**Rate limit:** 10 requests / 3 minutes

```shell
curl -X POST 'https://apiv2.nobitex.ir/cobank/withdraw' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000' \
  -H 'Content-Type: application/json' \
  --data '{"destinationBankAccountId": 13568, "amount": "2500000000"}'
```

Example response:
```json
{
  "status": "ok",
  "result": {
    "id": "CW430542",
    "createdAt": "2021-12-11T10:13:42.957103+00:00",
    "status": "New",
    "amount": "2500000000",
    "fee": "500000",
    "fulfilledAmount": "499500000",
    "bankAccountId": 13568,
    "bankAccountInfo": "صادرات: IR670190123456789001234567",
    "isCancelable": true,
    "records": [
      { "amount": "1000000000", "bankReferenceNumber": null, "status": "Pending", "estimatedSettleAt": null, "providerUpdatedAt": "2021-12-11T10:13:42.957103+00:00", "transferType": "normal" },
      { "amount": "1000000000", "bankReferenceNumber": "35413545", "status": "Failed", "estimatedSettleAt": "2021-12-11T10:13:42.957103+00:00", "providerUpdatedAt": "2021-12-11T10:13:42.957103+00:00", "transferType": "paya" },
      { "amount": "499500000", "bankReferenceNumber": "35415784546", "status": "Transferred", "estimatedSettleAt": "2021-12-11T10:13:42.957103+00:00", "providerUpdatedAt": "2021-12-11T10:13:42.957103+00:00", "transferType": "satna" }
    ]
  }
}
```

Error response example:
```json
{ "status": "failed", "code": "WithdrawUnavailable", "message": "WithdrawUnavailable" }
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `destinationBankAccountId` | int | required | Verified bank account ID (from your [profile](#profile)) | `13568` |
| `amount` | monetary | required | Amount (IRR) | `2500000000` |

**Response fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `id` | string | Withdrawal request ID — prefixed `WJ` or `CW`, followed by a number | `CW13568` |
| `createdAt` | string | Creation time | `2021-12-11T10:13:42.957103+00:00` |
| `status` | string | Request status | `New` |
| `amount` | monetary | Requested amount (IRR) | `2500000000` |
| `fee` | monetary | Fee (IRR) | `20000` |
| `fulfilledAmount` | monetary | Amount actually transferred so far (starts at 0) | `1500000000` |
| `bankAccountId` | int | Bank account ID | `13568` |
| `bankAccountInfo` | string | Bank name + account number | `صادرات: IR670190123456789001234567` |
| `isCancelable` | bool | Whether the request can still be cancelled | `true`, `false` |
| `records` | array | Sub-transaction records, if any | |

**`records` item fields**

| Field | Type | Description | Example |
|---|---|---|---|
| `amount` | monetary | Amount sent to the bank for this record (IRR) | `1000000000` |
| `bankReferenceNumber` | string | Bank tracking reference | `123f345345g34634` |
| `status` | string | Sub-transaction status | `Pending`, `Failed`, `Transferred` |
| `estimatedSettleAt` | string | Estimated settlement time | `2021-12-11T10:13:42.957103+00:00` |
| `providerUpdatedAt` | string | Last update time from the provider | `2021-12-11T10:13:42.957103+00:00` |
| `transferType` | string | Transfer rail | `normal`, `paya`, `satna` |

**Withdrawal request statuses**

| Status | Description |
|---|---|
| `New` | Request submitted by the user |
| `Sent` | Sent to the payment provider |
| `Bank processing` | Being processed by the bank |
| `Partially Done` | Partially succeeded |
| `Done` | Fully succeeded |
| `Failed` | Fully failed |
| `Rejected` | Rejected (by Nobitex, the provider, or the bank) |
| `Canceled` | Cancelled by the user |

**Error responses**

| Error code | Description |
|---|---|
| `UnAcceptedDisclaimerError` | You need to confirm your spot wallet balance disclaimer before withdrawing |
| `FeatureUnavailable` | This feature isn't available to you yet |
| `ParseError` | Problem with an input parameter |
| `BankAccountNotFound` | Bank account not found |
| `TooManyRequests` | You've already submitted a request — please wait |
| `WithdrawUnavailable` | Rial withdrawals are restricted for you |
| `WithdrawAmountLimitation` | Amount exceeds your max withdrawal limit |
| `InsufficientBalance` | Insufficient balance |
| `AmountTooLow` | Below the minimum withdrawal amount |
| `InsufficientBalanceOrInactiveWallet` | Insufficient balance |
| `WithdrawLimitReached` | Limited to 3 IRR withdrawals and 10 crypto withdrawals per 24 hours |
| `AmountTooHigh` | Above the maximum withdrawal amount |
| `ShabaWithdrawCannotProceed` | Max 2 billion IRR per SHABA/IBAN — split across multiple SHABA numbers if needed |

**Notes**

`estimatedSettleAt` and `bankReferenceNumber` start out empty and are populated once the bank returns that information.

### Cancel a Rial Withdrawal Request

**Endpoint:** `POST /cobank/withdraw/<id>/cancel`
**Rate limit:** 60 requests/hour, and 10 requests/minute

```shell
curl -X POST 'https://apiv2.nobitex.ir/cobank/withdraw/<id>/cancel' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000' \
  -H 'Content-Type: application/json'
```

Example response: same `result` object shape as above, with `"status": "Canceled"` and `"isCancelable": false`.

Error example:
```json
{ "status": "failed", "code": "WithdrawRequestNotFound", "message": "Withdraw Request Not Found" }
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `id` | string | required | Withdrawal request ID | `CW256854` |

**Notes**

A withdrawal can only be cancelled while it's still `New` and less than 3 minutes have passed since it was created.

**Error responses**

| Error code | Description |
|---|---|
| `WithdrawRequestNotFound` | Withdrawal request not found |
| `UnAcceptedDisclaimerError` | You need to confirm your spot wallet balance disclaimer before withdrawing |
| `CancellationFailed` | Cancellation failed |
| `NotCancellable` | This request can no longer be cancelled |

### Get Rial Withdrawal Details

**Endpoint:** `GET /cobank/withdraw/<id>`
**Rate limit:** 60 requests / 2 minutes

```shell
curl -X GET 'https://apiv2.nobitex.ir/cobank/withdraw/<id>' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000' \
  -H 'Content-Type: application/json'
```

Example response: same `result` object shape as in "Submit" above.

Error example:
```json
{ "status": "failed", "code": "WithdrawRequestNotFound", "message": "Withdraw Request Not Found" }
```

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `id` | string | required | Withdrawal request ID | `CW256854` |

**Error responses**

| Error code | Description |
|---|---|
| `WithdrawRequestNotFound` | Withdrawal request not found |
| `UnAcceptedDisclaimerError` | You need to confirm your spot wallet balance disclaimer before withdrawing |

---

## 11. Withdrawal Status & Lists

### List All Withdrawals

**Endpoint:** `GET /users/wallets/withdraws/list`
**Rate limit:** 60 requests / 2 minutes
**Pagination:** Yes (default 20)
**Date filter:** Yes

```shell
curl 'https://apiv2.nobitex.ir/users/wallets/withdraws/list' \
  -H 'Authorization: Token yourTOKENhereHEX0000000000'
```

Example response:
```json
{
  "status": "ok",
  "withdraws": [
    {
      "id": 10,
      "createdAt": "2021-12-11T10:13:42.957103+00:00",
      "status": "Canceled",
      "amount": "0.0123",
      "currency": "btc",
      "network": "BTCLN",
      "invoice": "lnbc123m1pskcu80pp5qqqsyqcyq5rqwz...",
      "address": "SaMpLeWaLlEtAdDrEsS",
      "tag": "123456",
      "wallet_id": 3456,
      "blockchain_url": "https://nobitex.ir/receipt/Bitcoin/ewd23d...",
      "is_cancelable": true
    },
    {
      "id": 238,
      "createdAt": "2025-07-01T22:00:15.256089+00:00",
      "status": "Done",
      "amount": "1000000",
      "currency": "rls",
      "network": "FIAT_MONEY",
      "invoice": null,
      "address": "تجارت: IR140180000000003333333333",
      "tag": null,
      "wallet_id": 3451,
      "blockchain_url": "nobitex://app/wallet/rls/transaction/CW238",
      "is_cancelable": false
    },
    {
      "id": 239,
      "createdAt": "2025-07-01T22:05:15.256089+00:00",
      "status": "Done",
      "amount": "5000000",
      "currency": "rls",
      "address": "سامان: IR030560080180001896427001",
      "tag": null,
      "wallet_id": 341,
      "blockchain_url": "nobitex://app/wallet/rls/transaction/WJ239",
      "is_cancelable": false,
      "network": "FIAT_MONEY",
      "invoice": null
    },
    {
      "id": 240,
      "createdAt": "2025-07-02T22:05:15.256089+00:00",
      "status": "Done",
      "amount": "1",
      "currency": "ltc",
      "network": "LTC",
      "address": "Lgn1zc77mEjk72KvX...",
      "tag": null,
      "wallet_id": 3454,
      "blockchain_url": "https://live.blockcypher.com/ltc/tx/c1ed4229e598d4cf...",
      "is_cancelable": false
    }
  ],
  "hasNext": true
}
```

Returns your most recent withdrawals — both Rial and crypto.

**Request parameters**

| Parameter | Type | Default | Description | Example |
|---|---|---|---|---|
| `wallet` | string | `all` | Wallet ID | `3456` |

---

