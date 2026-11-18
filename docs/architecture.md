# PDC Funding architecture

Community crowdfunding for Privacy Data Coin. This service is an off-chain escrow ledger in front of one PDC simplewallet (PDC + BTCX) and an optional Bitcoin Core named wallet `funding` (native BTC). It is not an on-chain contract. PDC L1 has no EVM; L1 donation identity is an integrated address plus an 8-byte `payment_id`, the same pattern as the PDC tipbot. BTC donations use a fresh bech32 address per proposal asset row.

## Components

| Piece | Role |
| --- | --- |
| `funding/app.py` | FastAPI SSR UI, public read-only JSON API, session auth with TOTP 2FA, admin users/payouts |
| `funding/totp.py` | AES-GCM encrypted TOTP secrets (`FUNDING_TOTP_KEK`), QR enrollment |
| `funding/service.py` | Registration, multi-asset proposals, status changes, payout reservation |
| `funding/db.py` | SQLite (WAL). `proposal_assets` + balances as sums, not cached columns |
| `funding/assets.py` | PDC / BTCX / BTC tickers, decimals, asset ids |
| `funding/wallet.py` | simplewallet JSON-RPC (PDC+BTCX). Empty `WALLET_RPC_URL` is dry-run |
| `funding/btc_wallet.py` | bitcoind JSON-RPC for wallet `funding`. Empty `BTC_RPC_URL` is dry-run |
| `funding/scanner.py` | Background loop. Credits L1 bulk payments and BTC address deltas |
| `funding/prices.py` | Display-only USD quotes from Trade `GET /api/prices/usd` |
| `funding/amounts.py` | Wrappers around `assets` parse/format |

One process holds the wallet RPCs, the SQLite file, and the session secret. Uvicorn is started by `python -m funding` (`funding/__main__.py`).

## Status flow

`0 disabled` · `1 proposal` · `2 funding` · `3 wip` · `4 completed`

An admin move to status `>= 2` assigns `payment_id` and `integrated_address` once. The scanner credits proposals in status `2` or `3` only. Payouts are allowed in `2`, `3`, and `4`.

## Money flow

1. Author submits a proposal (status `proposal`) with one or more assets (PDC, BTCX, BTC), per-asset targets and payout destinations.
2. Admin opens funding. For PDC/BTCX the wallet RPC `make_integrated_address` returns a shared donation address. For BTC, bitcoind `getnewaddress` on wallet `funding` returns a bech32 address. Dry-run synthesizes placeholders.
3. Donors pay the relevant address (L1 integrated or BTC).
4. The scanner credits L1 via `get_bulk_payments` (keyed by `asset_id`) and BTC via `getreceivedbyaddress` deltas. Daemon height gates L1 confirmations.
5. Deposits are unique on `(tx_hash, payment_id, asset)`. `received` / `paid` are per-asset sums on `proposal_assets`.
6. Admin payout reserves escrow **only from this proposal’s funded balances** (hard fee model): PDC carves `TRANSFER_FEE` from the payout amount; BTCX sends full BTCX and covers L1 fee from PDC escrow, or oracle-converts leftover BTCX/BTC on the same proposal (+2% slip) when PDC is short; BTC sends the full reserved amount with `subtractfeefromamount` so the miner fee comes from that BTC. Authors freely choose any one, two, or all three assets at create time (nothing forced). Liquid PDC must still exist in the hot L1 wallet to pay the chain fee (maintain a small fee buffer; conversions reimburse the pool in BTCX/BTC).

Available escrow is per asset: `received - paid`, including pending reservations. Hot wallets are pooled; per-proposal numbers are an application invariant.

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

Public JSON under `/api/v1` is read-only (GET only). OpenAPI `/docs` is disabled.

- `GET /api/v1/proposals` — public list (funding progress + donation address when open)
- `GET /api/v1/health` — `wallet` / `btc_wallet` are `live` or `dry-run`

Mutations (register, login, proposals, status, comments, payouts) use HTML form POSTs with a session cookie, not the JSON API. Deposit crediting runs in the background scanner.

## What this repo does not do

No L1 consensus changes, no multisig, no view-key deposit proofs, and no separation of donor funds inside the wallet. Loss of the wallet file, or an admin session, is loss of escrow.
