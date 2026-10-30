# PDC Funding

Community crowdfunding for **Privacy Data Coin** — FFS-style proposals, escrow donations, milestone payouts.

Inspired by the Wownero/AEON funding systems, rebuilt for PDC:

- Wallet pattern from **pdc-tipbot** (`payment_id` + integrated addresses, not Monero subaccounts)
- USD prices from Trade `GET /api/prices/usd` (oracle when Trade enables it)
- Atomic amounts (`10^12`), SQLite, FastAPI + modern SSR UI

## Status flow

`proposal` → `funding` → `wip` → `completed` (plus `disabled`)

When an admin moves a proposal to **funding**, the app assigns an integrated donation address. The deposit scanner credits confirmed payments. Admins pay milestones from available escrow.

## Quick start (dry-run)

```bash
cd pdc-funding
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# optional: set BOOTSTRAP_ADMIN_PASSWORD=...
python -m funding
```

Open http://127.0.0.1:8088

With empty `WALLET_RPC_URL`, donation addresses and payouts are simulated (dry-run). No chain I/O.

```bash
pytest
```

## Live wallet

Point `.env` at a thin PDC `simplewallet` RPC (same style as tipbot):

```env
WALLET_RPC_URL=http://127.0.0.1:19311/json_rpc
WALLET_RPC_USER=...
WALLET_RPC_PASSWORD=...
DAEMON_RPC_URL=http://127.0.0.1:19211
```

Keep escrow thin; store surplus cold. Operator keys = custodial trust.

## API

- `GET /api/v1/proposals` — public read-only list
- `GET /api/v1/health` — public readiness (`wallet`: `live` | `dry-run`)

JSON `/api/v1` has no write methods. OpenAPI UI is disabled.

## Security notes

- Custodial escrow (admin can pay out)
- Session cookies + bcrypt passwords
- Change `SECRET_KEY` and bootstrap admin password before any public deploy
- Never commit `.env`, wallet files, or seeds
- Architecture, findings, and reconcile steps: [docs/architecture.md](docs/architecture.md), [docs/security-audit.md](docs/security-audit.md)

Deposits are credited only after `DEPOSIT_MIN_CONFIRMATIONS` against daemon `getinfo` height (chain size). If that height is unknown, nothing is credited.

A milestone payout is reserved in SQLite before the wallet RPC call. An uncertain RPC result keeps the reservation so the same balance cannot be sent twice. Pending rows show as `pending` in the UI until an operator reconciles them (see the audit doc).

The first live deposit scan after upgrade resets the scan watermark once. Credits stay idempotent on `(tx_hash, payment_id)`.

## License

MIT — see [LICENSE](LICENSE).
