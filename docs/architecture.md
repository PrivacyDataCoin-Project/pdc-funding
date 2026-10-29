# PDC Funding architecture

Community crowdfunding for Privacy Data Coin. This service is an off-chain escrow ledger in front of one PDC simplewallet. It is not an on-chain contract. PDC L1 has no EVM; donation identity is an integrated address plus an 8-byte `payment_id`, the same pattern as the PDC tipbot.

## Components

| Piece | Role |
| --- | --- |
| `funding/app.py` | FastAPI SSR UI, public read API, admin session for status, comments, payouts, and a manual scan |
| `funding/service.py` | Registration, proposals, status changes, payout reservation |
| `funding/db.py` | SQLite (WAL). Balances are sums, not cached columns |
| `funding/wallet.py` | simplewallet JSON-RPC. Empty `WALLET_RPC_URL` is dry-run: no chain I/O |
| `funding/scanner.py` | Background loop. Credits confirmed incoming payments |
| `funding/prices.py` | Display-only USD quotes from Trade `GET /api/prices/usd` |
| `funding/amounts.py` | Human PDC strings ↔ atomic units (`1 PDC = 10^12`) |

One process holds the wallet RPC, the SQLite file, and the session secret. Uvicorn is started by `python -m funding` (`funding/__main__.py`).

## Status flow

`0 disabled` · `1 proposal` · `2 funding` · `3 wip` · `4 completed`

An admin move to status `>= 2` assigns `payment_id` and `integrated_address` once. The scanner credits proposals in status `2` or `3` only. Payouts are allowed in `2`, `3`, and `4`.

## Money flow

1. Author submits a proposal (status `proposal`) with a target and an optional payout address or `@alias`.
2. Admin opens funding. The wallet RPC `make_integrated_address` returns the donation address. Dry-run synthesizes one.
3. Donors pay that integrated address on L1.
4. The scanner calls wallet `get_bulk_payments` and daemon `GET {DAEMON_RPC_URL}/getinfo`.
5. A payment is inserted into `deposits` when it is unlocked, has enough confirmations, and is not a different asset id. `received` is `SUM(deposits.amount_atomic)`.
6. Admin payout reserves a `payouts` row (`tx_hash` starts with `pending:`), then calls wallet `transfer` for `PDC_ASSET_ID`. On a definite reject the row is deleted. On success the real tx id replaces the placeholder. `paid` is `SUM(payouts.amount_atomic)`, so a reservation already reduces available escrow.

Available escrow for a proposal is `received - paid`, including pending reservations. The hot wallet itself is one pooled balance. Per-proposal numbers are an application invariant, not a chain partition. Transfer fees are paid by that wallet and are not subtracted from the proposal row.

## Trust boundaries

- **Browser → app.** Session cookie (itsdangerous, `SameSite=Lax`). Admin is a boolean on `users`. The first account is admin when the table is empty; bootstrap uses `BOOTSTRAP_ADMIN_PASSWORD` when set.
- **App → SQLite.** Local file. Operators who can write the file can change balances.
- **App → simplewallet RPC.** Full custody of the escrow key. A hostile or buggy RPC can report payments or drop transfers. Transport failures after `transfer` are treated as ambiguous.
- **App → daemon RPC.** Chain height and `@alias` resolution. Height is required before crediting when `DEPOSIT_MIN_CONFIRMATIONS > 0`.
- **App → Trade prices.** USD text on the page only. Prices do not gate payouts.

## Confirmation and unlock assumptions

These match Zano-style simplewallet / daemon behavior:

- `getinfo.height` is chain size (number of blocks).
- Payment `block_height` is the block index.
- Confirmations = `height - block_height` (a tx in the tip has 1 confirmation when `height == block_height + 1`).
- `unlock_time == 0` is unlocked. Values below `500_000_000` are block heights (`height >= unlock_time`). Larger values are unix timestamps.

The scan watermark (`meta.deposit_scan_min_height`) stays at least `DEPOSIT_MIN_CONFIRMATIONS + 5` blocks behind the tip, and behind any payment that is still locked. The first scan after upgrade sets that watermark to `0` once (`deposit_scan_watermark_v2`) so older unconfirmed payments are not skipped. Re-scans are safe: deposits are unique on `(tx_hash, payment_id)` after hex tx ids are lowercased.

## API

- `GET /api/v1/proposals` — public list
- `GET /api/v1/health` — `wallet` is `live` or `dry-run`
- `POST /api/v1/scan` — admin session, one scanner pass

## What this repo does not do

No L1 consensus changes, no multisig, no view-key deposit proofs, and no separation of donor funds inside the wallet. Loss of the wallet file, or an admin session, is loss of escrow.
