#!/usr/bin/env bash
# Create a dedicated bitcoind wallet named "funding" for PDC Funding BTC escrow.
# Does NOT copy or unlock the bridge wallet. Safe to re-run (idempotent).
set -euo pipefail

DATADIR="${BITCOIN_DATADIR:-/home/ubuntu/bitcoin}"
WALLET_NAME="${BTC_WALLET_NAME:-funding}"
SECRETS_DIR="${PDC_SECRETS_DIR:-/etc/pdc-secrets}"
CLI=(bitcoin-cli -datadir="$DATADIR")

echo "==> Ensuring bitcoind wallet '$WALLET_NAME' exists (no bridge copy)"
WALLETS="$("${CLI[@]}" listwallets 2>/dev/null || true)"
if echo "$WALLETS" | grep -q "\"$WALLET_NAME\""; then
  echo "    already loaded"
else
  if [[ -d "$DATADIR/$WALLET_NAME" ]]; then
    echo "    loading existing wallet directory"
    "${CLI[@]}" loadwallet "$WALLET_NAME" >/dev/null
  else
    echo "    creating blank descriptor wallet"
    "${CLI[@]}" createwallet "$WALLET_NAME" false false "" false true true >/dev/null
  fi
fi

echo "==> Writing secrets under $SECRETS_DIR (root-readable by ubuntu group)"
sudo mkdir -p "$SECRETS_DIR"
sudo install -m 640 -o root -g ubuntu /dev/null "$SECRETS_DIR/funding-btc.wallet-name"
echo "$WALLET_NAME" | sudo tee "$SECRETS_DIR/funding-btc.wallet-name" >/dev/null

# RPC credentials from bitcoin SECRETS.env or bitcoin.conf (never print password).
if [[ -f "$DATADIR/SECRETS.env" ]]; then
  # shellcheck disable=SC1090
  set -a
  # Only export BITCOIN_* keys
  # shellcheck disable=SC1091
  source <(grep -E '^BITCOIN_RPC_' "$DATADIR/SECRETS.env" || true)
  set +a
fi
RPC_URL="${BITCOIN_RPC_URL:-http://127.0.0.1:8332}"
RPC_USER="${BITCOIN_RPC_USER:-}"
RPC_PASS="${BITCOIN_RPC_PASS:-}"

echo "$RPC_URL" | sudo tee "$SECRETS_DIR/funding-btc.rpc-url" >/dev/null
echo "$RPC_USER" | sudo tee "$SECRETS_DIR/funding-btc.rpc-user" >/dev/null
echo "$RPC_PASS" | sudo tee "$SECRETS_DIR/funding-btc.rpc-pass" >/dev/null
sudo chmod 640 "$SECRETS_DIR"/funding-btc.*
sudo chown root:ubuntu "$SECRETS_DIR"/funding-btc.*

# Descriptor backup (xpubs / descriptors — keep private).
DESC="$("${CLI[@]}" -rpcwallet="$WALLET_NAME" listdescriptors true 2>/dev/null || true)"
if [[ -n "$DESC" ]]; then
  echo "$DESC" | sudo tee "$SECRETS_DIR/funding-btc.descriptors.json" >/dev/null
  sudo chmod 640 "$SECRETS_DIR/funding-btc.descriptors.json"
  sudo chown root:ubuntu "$SECRETS_DIR/funding-btc.descriptors.json"
  echo "    wrote funding-btc.descriptors.json"
fi

ADDR="$("${CLI[@]}" -rpcwallet="$WALLET_NAME" getnewaddress "funding-ops-check" "bech32")"
echo "$ADDR" | sudo tee "$SECRETS_DIR/funding-btc.sample-address" >/dev/null
sudo chmod 640 "$SECRETS_DIR/funding-btc.sample-address"
sudo chown root:ubuntu "$SECRETS_DIR/funding-btc.sample-address"
echo "    sample receive address stored (ops check only)"

echo "==> Append hints for pdc-funding .env (edit manually if needed):"
cat <<EOF
BTC_RPC_URL=$RPC_URL
BTC_RPC_USER=$RPC_USER
BTC_RPC_PASSWORD=<from $SECRETS_DIR/funding-btc.rpc-pass>
BTC_WALLET_NAME=$WALLET_NAME
EOF
echo "Done."
