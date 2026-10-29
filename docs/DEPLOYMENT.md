# Deployment — funding.privacydatacoin.com

Production host pattern matches tipbot/oracle on the PDC edge box (`/home/ubuntu/pdc-funding`).

## Admin password

1. **First boot:** set `BOOTSTRAP_ADMIN_PASSWORD` in `/home/ubuntu/pdc-funding/.env` **before** the first start (only applies when `users` is empty).
2. **After login:** change it in the UI at **https://funding.privacydatacoin.com/account/password** (nav → Password).
3. Editing `.env` later does **not** change an existing admin hash.

Default bootstrap user: `BOOTSTRAP_ADMIN_USER` (usually `admin`).

## Layout

```text
/home/ubuntu/pdc-funding/
  funding/ … tests/ deploy/ docs/
  .env                 # mode 600 — secrets
  data/funding.db
  .venv/
```

## App env (essentials)

```env
SECRET_KEY=<long random>
BIND_HOST=127.0.0.1
BIND_PORT=8095
SESSION_HTTPS_ONLY=1
DATABASE_PATH=/home/ubuntu/pdc-funding/data/funding.db
BOOTSTRAP_ADMIN_USER=admin
BOOTSTRAP_ADMIN_PASSWORD=<initial>
BOOTSTRAP_ADMIN_EMAIL=admin@example.com
WALLET_RPC_URL=          # empty = dry-run until escrow wallet is ready
DAEMON_RPC_URL=http://127.0.0.1:19211
```

Keep escrow wallet separate from tipbot. Surplus cold.

## Install / update

```bash
cd /home/ubuntu/pdc-funding
git pull --ff-only
./.venv/bin/pip install -r requirements.txt
PYTHONPATH=. ./.venv/bin/pytest -q   # must be green
sudo systemctl restart pdc-funding
curl -fsS http://127.0.0.1:8095/api/v1/health
```

Nginx + TLS: copy `deploy/nginx-funding.privacydatacoin.com.conf.example`, then certbot. systemd: `deploy/pdc-funding.service.example`.
