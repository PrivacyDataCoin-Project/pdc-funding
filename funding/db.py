"""SQLite persistence for proposals, deposits, payouts, users, comments."""

from __future__ import annotations

import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_MAX_ATOMIC = 2**63 - 1


def normalize_tx_hash(tx_hash: str) -> str:
    raw = (tx_hash or "").strip()
    if _HEX_RE.fullmatch(raw):
        return raw.lower()
    return raw

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    totp_secret_enc BLOB,
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS login_challenges (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    purpose TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    headline TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    status INTEGER NOT NULL DEFAULT 1,
    funds_target_atomic INTEGER NOT NULL CHECK (funds_target_atomic > 0),
    payout_address TEXT NOT NULL DEFAULT '',
    payment_id TEXT UNIQUE,
    integrated_address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id),
    payment_id TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    amount_atomic INTEGER NOT NULL CHECK (amount_atomic > 0),
    block_height INTEGER NOT NULL DEFAULT 0,
    asset TEXT NOT NULL DEFAULT 'PDC',
    credited_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(tx_hash, payment_id, asset)
);

CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id),
    amount_atomic INTEGER NOT NULL CHECK (amount_atomic > 0),
    to_address TEXT NOT NULL,
    tx_hash TEXT NOT NULL DEFAULT '',
    milestone_note TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    message TEXT NOT NULL,
    replied_to INTEGER REFERENCES comments(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposal_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id),
    asset TEXT NOT NULL,
    funds_target_atomic INTEGER NOT NULL CHECK (funds_target_atomic > 0),
    payout_address TEXT NOT NULL DEFAULT '',
    payment_id TEXT,
    integrated_address TEXT NOT NULL DEFAULT '',
    btc_address TEXT NOT NULL DEFAULT '',
    UNIQUE(proposal_id, asset)
);

CREATE INDEX IF NOT EXISTS ix_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS ix_proposals_payment_id ON proposals(payment_id);
CREATE INDEX IF NOT EXISTS ix_deposits_proposal ON deposits(proposal_id);
CREATE INDEX IF NOT EXISTS ix_comments_proposal ON comments(proposal_id);
CREATE INDEX IF NOT EXISTS ix_proposal_assets_pid ON proposal_assets(payment_id);
CREATE INDEX IF NOT EXISTS ix_proposal_assets_btc ON proposal_assets(btc_address);
-- BTC receive addresses must be globally unique (one chain UTXO stream → one ledger).
CREATE UNIQUE INDEX IF NOT EXISTS ux_proposal_assets_btc_addr
    ON proposal_assets(btc_address) WHERE btc_address != '';
"""


@dataclass
class User:
    id: int
    username: str
    email: str
    password_hash: str
    is_admin: bool
    totp_enabled: bool = False
    created_at: str = ""


@dataclass
class Proposal:
    id: int
    user_id: int
    headline: str
    content: str
    category: str
    status: int
    funds_target_atomic: int
    payout_address: str
    payment_id: Optional[str]
    integrated_address: str
    created_at: str
    updated_at: str
    author: str = ""
    received_atomic: int = 0
    paid_atomic: int = 0
    pending_atomic: int = 0


@dataclass
class Comment:
    id: int
    proposal_id: int
    user_id: int
    message: str
    replied_to: Optional[int]
    created_at: str
    author: str = ""


@dataclass
class Deposit:
    id: int
    proposal_id: int
    payment_id: str
    tx_hash: str
    amount_atomic: int
    block_height: int
    credited_at: str
    asset: str = "PDC"


@dataclass
class Payout:
    id: int
    proposal_id: int
    amount_atomic: int
    to_address: str
    tx_hash: str
    milestone_note: str
    created_at: str
    asset: str = "PDC"


@dataclass
class ProposalAsset:
    id: int
    proposal_id: int
    asset: str
    funds_target_atomic: int
    payout_address: str
    payment_id: Optional[str]
    integrated_address: str
    btc_address: str
    received_atomic: int = 0
    paid_atomic: int = 0
    pending_atomic: int = 0


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        if conn.in_transaction:
            conn.commit()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(users)")}
        if "totp_enabled" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if "totp_secret_enc" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN totp_secret_enc BLOB")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS login_challenges ("
            "id TEXT PRIMARY KEY, "
            "user_id INTEGER NOT NULL REFERENCES users(id), "
            "purpose TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, "
            "consumed_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS proposal_assets ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "proposal_id INTEGER NOT NULL REFERENCES proposals(id), "
            "asset TEXT NOT NULL, "
            "funds_target_atomic INTEGER NOT NULL CHECK (funds_target_atomic > 0), "
            "payout_address TEXT NOT NULL DEFAULT '', "
            "payment_id TEXT, "
            "integrated_address TEXT NOT NULL DEFAULT '', "
            "btc_address TEXT NOT NULL DEFAULT '', "
            "UNIQUE(proposal_id, asset))"
        )
        dep_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(deposits)")}
        if "asset" not in dep_cols:
            conn.execute(
                "ALTER TABLE deposits ADD COLUMN asset TEXT NOT NULL DEFAULT 'PDC'"
            )
        pay_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(payouts)")}
        if "asset" not in pay_cols:
            conn.execute(
                "ALTER TABLE payouts ADD COLUMN asset TEXT NOT NULL DEFAULT 'PDC'"
            )
        # Rebuild deposits unique key to include asset when still on legacy (tx,pid).
        idx_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='deposits'"
        ).fetchone()
        create_sql = str(idx_sql["sql"] or "") if idx_sql else ""
        if (
            "UNIQUE(tx_hash, payment_id, asset)" not in create_sql
            and "UNIQUE(tx_hash, payment_id)" in create_sql
        ):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS deposits_v2 ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "proposal_id INTEGER NOT NULL REFERENCES proposals(id), "
                "payment_id TEXT NOT NULL, "
                "tx_hash TEXT NOT NULL, "
                "amount_atomic INTEGER NOT NULL CHECK (amount_atomic > 0), "
                "block_height INTEGER NOT NULL DEFAULT 0, "
                "asset TEXT NOT NULL DEFAULT 'PDC', "
                "credited_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "UNIQUE(tx_hash, payment_id, asset))"
            )
            conn.execute(
                "INSERT OR IGNORE INTO deposits_v2 "
                "(id, proposal_id, payment_id, tx_hash, amount_atomic, block_height, asset, credited_at) "
                "SELECT id, proposal_id, payment_id, tx_hash, amount_atomic, block_height, "
                "COALESCE(asset, 'PDC'), credited_at FROM deposits"
            )
            conn.execute("DROP TABLE deposits")
            conn.execute("ALTER TABLE deposits_v2 RENAME TO deposits")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_deposits_proposal ON deposits(proposal_id)"
            )
        # Backfill legacy single-target proposals into proposal_assets as PDC.
        missing = conn.execute(
            "SELECT p.id, p.funds_target_atomic, p.payout_address, "
            "p.payment_id, p.integrated_address "
            "FROM proposals p LEFT JOIN proposal_assets a "
            "ON a.proposal_id = p.id AND a.asset = 'PDC' "
            "WHERE a.id IS NULL"
        ).fetchall()
        for row in missing:
            conn.execute(
                "INSERT INTO proposal_assets "
                "(proposal_id, asset, funds_target_atomic, payout_address, "
                "payment_id, integrated_address, btc_address) "
                "VALUES (?, 'PDC', ?, ?, ?, ?, '')",
                (
                    int(row["id"]),
                    int(row["funds_target_atomic"]),
                    str(row["payout_address"] or ""),
                    str(row["payment_id"]) if row["payment_id"] else None,
                    str(row["integrated_address"] or ""),
                ),
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_proposal_assets_btc_addr "
            "ON proposal_assets(btc_address) WHERE btc_address != ''"
        )

    def user_count(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"])

    def list_users(self, *, limit: int = 200, offset: int = 0) -> list[User]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY id ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._row_user(r) for r in rows]

    def create_user(
        self,
        username: str,
        email: str,
        password_hash: str,
        *,
        is_admin: bool = False,
        promote_if_first: bool = False,
    ) -> User:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if promote_if_first and not is_admin:
                row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
                if int(row["c"]) == 0:
                    is_admin = True
            cur = conn.execute(
                "INSERT INTO users (username, email, password_hash, is_admin) "
                "VALUES (?, ?, ?, ?)",
                (username.strip(), email.strip().lower(), password_hash, int(is_admin)),
            )
            return self._user_from_id(conn, int(cur.lastrowid))

    def get_user(self, user_id: int) -> Optional[User]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            return self._row_user(row) if row else None

    def set_password_hash(self, user_id: int, password_hash: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"user {user_id} missing")

    def get_totp_secret_enc(self, user_id: int) -> Optional[bytes]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT totp_secret_enc FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not row or row["totp_secret_enc"] is None:
                return None
            return bytes(row["totp_secret_enc"])

    def set_totp_secret(
        self, user_id: int, secret_enc: bytes, *, enabled: bool = False
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE users SET totp_secret_enc = ?, totp_enabled = ? WHERE id = ?",
                (secret_enc, int(enabled), user_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"user {user_id} missing")

    def enable_totp(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE users SET totp_enabled = 1 "
                "WHERE id = ? AND totp_secret_enc IS NOT NULL",
                (user_id,),
            )
            if cur.rowcount != 1:
                raise ValueError("totp secret missing")

    def clear_totp(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE users SET totp_secret_enc = NULL, totp_enabled = 0 WHERE id = ?",
                (user_id,),
            )
            if cur.rowcount != 1:
                raise KeyError(f"user {user_id} missing")

    def create_login_challenge(
        self, user_id: int, purpose: str, *, ttl_seconds: int = 300
    ) -> str:
        cid = secrets.token_hex(16)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO login_challenges (id, user_id, purpose, expires_at) "
                "VALUES (?, ?, ?, datetime('now', ?))",
                (cid, user_id, purpose, f"+{int(ttl_seconds)} seconds"),
            )
        return cid

    def peek_login_challenge(
        self, challenge_id: str, *, purpose: str
    ) -> Optional[int]:
        """Return user_id if challenge is still valid (not consumed)."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM login_challenges "
                "WHERE id = ? AND purpose = ? AND consumed_at IS NULL "
                "AND expires_at > datetime('now')",
                (challenge_id, purpose),
            ).fetchone()
            return int(row["user_id"]) if row else None

    def consume_login_challenge(
        self, challenge_id: str, *, purpose: str
    ) -> Optional[int]:
        """Return user_id if challenge is valid and mark consumed; else None."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT user_id FROM login_challenges "
                "WHERE id = ? AND purpose = ? AND consumed_at IS NULL "
                "AND expires_at > datetime('now')",
                (challenge_id, purpose),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE login_challenges SET consumed_at = datetime('now') WHERE id = ?",
                (challenge_id,),
            )
            return int(row["user_id"])

    def get_user_by_username(self, username: str) -> Optional[User]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (username.strip(),),
            ).fetchone()
            return self._row_user(row) if row else None

    def create_proposal(
        self,
        *,
        user_id: int,
        headline: str,
        content: str,
        category: str,
        funds_target_atomic: int,
        payout_address: str = "",
        status: int = 1,
    ) -> Proposal:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO proposals "
                "(user_id, headline, content, category, status, funds_target_atomic, payout_address) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    headline.strip(),
                    content.strip(),
                    category,
                    status,
                    funds_target_atomic,
                    payout_address.strip(),
                ),
            )
            return self._proposal_from_id(conn, int(cur.lastrowid))

    def set_donation_address(
        self, proposal_id: int, payment_id: str, integrated_address: str
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE proposals SET payment_id = ?, integrated_address = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (payment_id.lower(), integrated_address, proposal_id),
            )

    def set_donation_address_if_empty(
        self, proposal_id: int, payment_id: str, integrated_address: str
    ) -> bool:
        """Assign an address once. A lost race leaves the winning row in place."""
        pid = (payment_id or "").strip().lower()
        addr = (integrated_address or "").strip()
        if not pid or not addr:
            raise ValueError("payment_id and integrated address required")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE proposals SET payment_id = ?, integrated_address = ?, "
                "updated_at = datetime('now') "
                "WHERE id = ? AND (payment_id IS NULL OR payment_id = '' "
                "OR integrated_address IS NULL OR integrated_address = '')",
                (pid, addr, proposal_id),
            )
            return cur.rowcount == 1

    def update_proposal_status(self, proposal_id: int, status: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE proposals SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, proposal_id),
            )

    def delete_proposal(self, proposal_id: int) -> bool:
        """Remove a proposal and all related ledger/comment rows. Returns False if missing."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if not row:
                return False
            # Break comment self-FK before deleting the thread.
            conn.execute(
                "UPDATE comments SET replied_to = NULL WHERE proposal_id = ?",
                (proposal_id,),
            )
            conn.execute("DELETE FROM comments WHERE proposal_id = ?", (proposal_id,))
            conn.execute("DELETE FROM deposits WHERE proposal_id = ?", (proposal_id,))
            conn.execute("DELETE FROM payouts WHERE proposal_id = ?", (proposal_id,))
            conn.execute(
                "DELETE FROM proposal_assets WHERE proposal_id = ?", (proposal_id,)
            )
            cur = conn.execute("DELETE FROM proposals WHERE id = ?", (proposal_id,))
            return cur.rowcount == 1

    def get_proposal(self, proposal_id: int) -> Optional[Proposal]:
        with self._lock, self._connect() as conn:
            return self._proposal_from_id(conn, proposal_id)

    def list_proposals(
        self,
        *,
        status: Optional[int] = None,
        category: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Proposal]:
        clauses: list[str] = []
        args: list[object] = []
        if status is not None:
            clauses.append("p.status = ?")
            args.append(status)
        if category:
            clauses.append("p.category = ?")
            args.append(category)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT p.*, u.username AS author, "
            "COALESCE((SELECT SUM(amount_atomic) FROM deposits d WHERE d.proposal_id = p.id), 0) "
            "AS received_atomic, "
            "COALESCE((SELECT SUM(amount_atomic) FROM payouts o WHERE o.proposal_id = p.id), 0) "
            "AS paid_atomic, "
            "COALESCE((SELECT SUM(amount_atomic) FROM payouts o "
            "WHERE o.proposal_id = p.id AND o.tx_hash LIKE 'pending:%'), 0) "
            "AS pending_atomic "
            f"FROM proposals p JOIN users u ON u.id = p.user_id {where} "
            "ORDER BY p.created_at DESC LIMIT ? OFFSET ?"
        )
        args.extend([limit, offset])
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
            return [self._row_proposal(r) for r in rows]

    def proposals_needing_scan(self) -> list[Proposal]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT p.*, u.username AS author, "
                "0 AS received_atomic, 0 AS paid_atomic, 0 AS pending_atomic "
                "FROM proposals p JOIN users u ON u.id = p.user_id "
                "WHERE p.payment_id IS NOT NULL AND p.status IN (2, 3, 4)"
            ).fetchall()
            return [self._row_proposal(r) for r in rows]

    def add_proposal_asset(
        self,
        *,
        proposal_id: int,
        asset: str,
        funds_target_atomic: int,
        payout_address: str = "",
    ) -> ProposalAsset:
        ticker = (asset or "").strip().upper()
        if funds_target_atomic <= 0 or funds_target_atomic > _MAX_ATOMIC:
            raise ValueError("invalid target")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO proposal_assets "
                "(proposal_id, asset, funds_target_atomic, payout_address) "
                "VALUES (?, ?, ?, ?)",
                (
                    proposal_id,
                    ticker,
                    funds_target_atomic,
                    (payout_address or "").strip(),
                ),
            )
            return self._proposal_asset_from_id(conn, int(cur.lastrowid))

    def list_proposal_assets(self, proposal_id: int) -> list[ProposalAsset]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT a.*, "
                "COALESCE((SELECT SUM(d.amount_atomic) FROM deposits d "
                " WHERE d.proposal_id = a.proposal_id AND d.asset = a.asset), 0) "
                "AS received_atomic, "
                "COALESCE((SELECT SUM(o.amount_atomic) FROM payouts o "
                " WHERE o.proposal_id = a.proposal_id AND o.asset = a.asset), 0) "
                "AS paid_atomic, "
                "COALESCE((SELECT SUM(o.amount_atomic) FROM payouts o "
                " WHERE o.proposal_id = a.proposal_id AND o.asset = a.asset "
                " AND o.tx_hash LIKE 'pending:%'), 0) AS pending_atomic "
                "FROM proposal_assets a WHERE a.proposal_id = ? ORDER BY a.id",
                (proposal_id,),
            ).fetchall()
            return [self._row_proposal_asset(r) for r in rows]

    def get_proposal_asset(
        self, proposal_id: int, asset: str
    ) -> Optional[ProposalAsset]:
        ticker = (asset or "").strip().upper()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT a.*, "
                "COALESCE((SELECT SUM(d.amount_atomic) FROM deposits d "
                " WHERE d.proposal_id = a.proposal_id AND d.asset = a.asset), 0) "
                "AS received_atomic, "
                "COALESCE((SELECT SUM(o.amount_atomic) FROM payouts o "
                " WHERE o.proposal_id = a.proposal_id AND o.asset = a.asset), 0) "
                "AS paid_atomic, "
                "COALESCE((SELECT SUM(o.amount_atomic) FROM payouts o "
                " WHERE o.proposal_id = a.proposal_id AND o.asset = a.asset "
                " AND o.tx_hash LIKE 'pending:%'), 0) AS pending_atomic "
                "FROM proposal_assets a WHERE a.proposal_id = ? AND a.asset = ?",
                (proposal_id, ticker),
            ).fetchone()
            return self._row_proposal_asset(row) if row else None

    def set_asset_l1_address_if_empty(
        self,
        proposal_id: int,
        asset: str,
        payment_id: str,
        integrated_address: str,
    ) -> bool:
        pid = (payment_id or "").strip().lower()
        addr = (integrated_address or "").strip()
        ticker = (asset or "").strip().upper()
        if not pid or not addr:
            raise ValueError("payment_id and integrated address required")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            clash = conn.execute(
                "SELECT proposal_id FROM proposal_assets "
                "WHERE payment_id IS NOT NULL AND lower(payment_id) = ? "
                "AND proposal_id != ? LIMIT 1",
                (pid, proposal_id),
            ).fetchone()
            if clash:
                raise RuntimeError(
                    f"payment_id already assigned to proposal #{int(clash['proposal_id'])}"
                )
            cur = conn.execute(
                "UPDATE proposal_assets SET payment_id = ?, integrated_address = ? "
                "WHERE proposal_id = ? AND asset = ? "
                "AND (payment_id IS NULL OR payment_id = '' "
                "OR integrated_address IS NULL OR integrated_address = '')",
                (pid, addr, proposal_id, ticker),
            )
            # Keep legacy columns in sync for PDC so old scanner paths still work.
            if ticker == "PDC" and cur.rowcount == 1:
                conn.execute(
                    "UPDATE proposals SET payment_id = ?, integrated_address = ?, "
                    "updated_at = datetime('now') "
                    "WHERE id = ? AND (payment_id IS NULL OR payment_id = '' "
                    "OR integrated_address IS NULL OR integrated_address = '')",
                    (pid, addr, proposal_id),
                )
            return cur.rowcount == 1

    def set_asset_btc_address_if_empty(
        self, proposal_id: int, btc_address: str
    ) -> bool:
        addr = (btc_address or "").strip()
        if not addr:
            raise ValueError("btc address required")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            clash = conn.execute(
                "SELECT proposal_id FROM proposal_assets "
                "WHERE btc_address != '' AND lower(btc_address) = lower(?) "
                "AND proposal_id != ? LIMIT 1",
                (addr, proposal_id),
            ).fetchone()
            if clash:
                raise RuntimeError(
                    f"btc_address already assigned to proposal #{int(clash['proposal_id'])}"
                )
            cur = conn.execute(
                "UPDATE proposal_assets SET btc_address = ? "
                "WHERE proposal_id = ? AND asset = 'BTC' "
                "AND (btc_address IS NULL OR btc_address = '')",
                (addr, proposal_id),
            )
            return cur.rowcount == 1

    def assets_needing_l1_scan(self) -> list[ProposalAsset]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT a.*, 0 AS received_atomic, 0 AS paid_atomic, 0 AS pending_atomic "
                "FROM proposal_assets a "
                "JOIN proposals p ON p.id = a.proposal_id "
                "WHERE a.payment_id IS NOT NULL AND a.payment_id != '' "
                "AND a.asset IN ('PDC', 'BTCX') AND p.status IN (2, 3, 4)"
            ).fetchall()
            return [self._row_proposal_asset(r) for r in rows]

    def assets_needing_btc_scan(self) -> list[ProposalAsset]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT a.*, 0 AS received_atomic, 0 AS paid_atomic, 0 AS pending_atomic "
                "FROM proposal_assets a "
                "JOIN proposals p ON p.id = a.proposal_id "
                "WHERE a.asset = 'BTC' AND a.btc_address IS NOT NULL AND a.btc_address != '' "
                "AND p.status IN (2, 3, 4)"
            ).fetchall()
            return [self._row_proposal_asset(r) for r in rows]

    def credit_deposit(
        self,
        *,
        proposal_id: int,
        payment_id: str,
        tx_hash: str,
        amount_atomic: int,
        block_height: int,
        asset: str = "PDC",
    ) -> bool:
        """Insert deposit if new. Returns True when credited."""
        tx_norm = normalize_tx_hash(tx_hash)
        pid = (payment_id or "").strip().lower()
        ticker = (asset or "PDC").strip().upper() or "PDC"
        if (
            not tx_norm
            or not pid
            or amount_atomic <= 0
            or amount_atomic > _MAX_ATOMIC
        ):
            return False
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Defense-in-depth: payment_id must belong to this proposal+asset
            # (BTC uses the bech32 address as payment_id).
            owned = conn.execute(
                "SELECT 1 FROM proposal_assets WHERE proposal_id = ? AND asset = ? "
                "AND ("
                "  (asset != 'BTC' AND payment_id IS NOT NULL AND lower(payment_id) = ?) "
                "  OR (asset = 'BTC' AND btc_address != '' AND lower(btc_address) = ?)"
                ")",
                (proposal_id, ticker, pid, pid),
            ).fetchone()
            if not owned:
                return False
            existing = conn.execute(
                "SELECT 1 FROM deposits WHERE lower(tx_hash) = lower(?) "
                "AND lower(payment_id) = lower(?) AND asset = ?",
                (tx_norm, pid, ticker),
            ).fetchone()
            if existing:
                return False
            try:
                conn.execute(
                    "INSERT INTO deposits "
                    "(proposal_id, payment_id, tx_hash, amount_atomic, block_height, asset) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        proposal_id,
                        pid,
                        tx_norm,
                        amount_atomic,
                        block_height,
                        ticker,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def credit_btc_cumulative(
        self,
        *,
        proposal_id: int,
        btc_address: str,
        chain_received_sats: int,
        block_height: int,
    ) -> int:
        """Credit BTC delta so ledger SUM never exceeds chain getreceivedbyaddress.

        Idempotent and race-safe under BEGIN IMMEDIATE: concurrent scanners cannot
        double-credit when chain height (or scan timing) changes between reads.
        Returns credited sats (0 when already caught up).
        """
        addr = (btc_address or "").strip().lower()
        if not addr or chain_received_sats <= 0 or chain_received_sats > _MAX_ATOMIC:
            return 0
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_atomic), 0) AS s FROM deposits "
                "WHERE proposal_id = ? AND asset = 'BTC' AND lower(payment_id) = ?",
                (proposal_id, addr),
            ).fetchone()
            owned = conn.execute(
                "SELECT 1 FROM proposal_assets WHERE proposal_id = ? AND asset = 'BTC' "
                "AND btc_address != '' AND lower(btc_address) = ?",
                (proposal_id, addr),
            ).fetchone()
            if not owned:
                return 0
            already = int(row["s"])
            delta = int(chain_received_sats) - already
            if delta <= 0:
                return 0
            # Stable id for this cumulative total (no height) → unique constraint helps.
            tx_hash = f"btc:{addr}:cum:{chain_received_sats}"
            try:
                conn.execute(
                    "INSERT INTO deposits "
                    "(proposal_id, payment_id, tx_hash, amount_atomic, block_height, asset) "
                    "VALUES (?, ?, ?, ?, ?, 'BTC')",
                    (proposal_id, addr, tx_hash, delta, int(block_height)),
                )
            except sqlite3.IntegrityError:
                return 0
            return delta

    def list_deposits(self, proposal_id: int) -> list[Deposit]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM deposits WHERE proposal_id = ? ORDER BY credited_at DESC",
                (proposal_id,),
            ).fetchall()
            return [
                Deposit(
                    id=int(r["id"]),
                    proposal_id=int(r["proposal_id"]),
                    payment_id=str(r["payment_id"]),
                    tx_hash=str(r["tx_hash"]),
                    amount_atomic=int(r["amount_atomic"]),
                    block_height=int(r["block_height"]),
                    credited_at=str(r["credited_at"]),
                    asset=str(r["asset"]) if "asset" in r.keys() else "PDC",
                )
                for r in rows
            ]

    def reserve_payout(
        self,
        *,
        proposal_id: int,
        amount_atomic: int,
        to_address: str,
        milestone_note: str,
        created_by: Optional[int],
        asset: str = "PDC",
    ) -> int:
        """Insert a pending payout under a write lock. Raises ValueError if insolvent."""
        if amount_atomic <= 0 or amount_atomic > _MAX_ATOMIC:
            raise ValueError("amount must be positive")
        dest = (to_address or "").strip()
        if not dest:
            raise ValueError("payout address required")
        ticker = (asset or "PDC").strip().upper() or "PDC"
        pending = f"pending:{secrets.token_hex(16)}"
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if not row:
                raise KeyError("proposal not found")
            if int(row["status"]) not in (2, 3, 4):
                raise ValueError("proposal must be funding/wip/completed to pay out")
            target = conn.execute(
                "SELECT 1 FROM proposal_assets WHERE proposal_id = ? AND asset = ?",
                (proposal_id, ticker),
            ).fetchone()
            if not target:
                raise ValueError(f"proposal has no {ticker} target")
            received = conn.execute(
                "SELECT COALESCE(SUM(amount_atomic), 0) AS s FROM deposits "
                "WHERE proposal_id = ? AND asset = ?",
                (proposal_id, ticker),
            ).fetchone()
            paid = conn.execute(
                "SELECT COALESCE(SUM(amount_atomic), 0) AS s FROM payouts "
                "WHERE proposal_id = ? AND asset = ?",
                (proposal_id, ticker),
            ).fetchone()
            available = int(received["s"]) - int(paid["s"])
            if amount_atomic > available:
                raise ValueError("amount exceeds available escrow balance")
            cur = conn.execute(
                "INSERT INTO payouts "
                "(proposal_id, amount_atomic, to_address, tx_hash, milestone_note, created_by, asset) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal_id,
                    amount_atomic,
                    dest,
                    pending,
                    milestone_note,
                    created_by,
                    ticker,
                ),
            )
            return int(cur.lastrowid)

    def release_payout(self, payout_id: int) -> bool:
        """Drop a reservation that was never broadcast. Returns True when deleted."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "DELETE FROM payouts WHERE id = ? AND tx_hash LIKE 'pending:%'",
                (payout_id,),
            )
            return cur.rowcount == 1

    def finalize_payout(self, payout_id: int, tx_hash: str) -> Payout:
        norm = normalize_tx_hash(tx_hash)
        if not norm or norm.startswith("pending:"):
            raise RuntimeError("refusing to finalize payout without a tx hash")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE payouts SET tx_hash = ? WHERE id = ? AND tx_hash LIKE 'pending:%'",
                (norm, payout_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"payout reservation {payout_id} missing")
            row = conn.execute(
                "SELECT * FROM payouts WHERE id = ?", (payout_id,)
            ).fetchone()
            return Payout(
                id=int(row["id"]),
                proposal_id=int(row["proposal_id"]),
                amount_atomic=int(row["amount_atomic"]),
                to_address=str(row["to_address"]),
                tx_hash=str(row["tx_hash"]),
                milestone_note=str(row["milestone_note"]),
                created_at=str(row["created_at"]),
                asset=str(row["asset"]) if "asset" in row.keys() else "PDC",
            )

    def add_payout(
        self,
        *,
        proposal_id: int,
        amount_atomic: int,
        to_address: str,
        tx_hash: str,
        milestone_note: str,
        created_by: Optional[int],
    ) -> Payout:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO payouts "
                "(proposal_id, amount_atomic, to_address, tx_hash, milestone_note, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    proposal_id,
                    amount_atomic,
                    to_address,
                    tx_hash,
                    milestone_note,
                    created_by,
                ),
            )
            row = conn.execute(
                "SELECT * FROM payouts WHERE id = ?", (int(cur.lastrowid),)
            ).fetchone()
            return Payout(
                id=int(row["id"]),
                proposal_id=int(row["proposal_id"]),
                amount_atomic=int(row["amount_atomic"]),
                to_address=str(row["to_address"]),
                tx_hash=str(row["tx_hash"]),
                milestone_note=str(row["milestone_note"]),
                created_at=str(row["created_at"]),
                asset=str(row["asset"]) if "asset" in row.keys() else "PDC",
            )

    def list_payouts(self, proposal_id: int) -> list[Payout]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM payouts WHERE proposal_id = ? ORDER BY created_at DESC",
                (proposal_id,),
            ).fetchall()
            return [
                Payout(
                    id=int(r["id"]),
                    proposal_id=int(r["proposal_id"]),
                    amount_atomic=int(r["amount_atomic"]),
                    to_address=str(r["to_address"]),
                    tx_hash=str(r["tx_hash"]),
                    milestone_note=str(r["milestone_note"]),
                    created_at=str(r["created_at"]),
                    asset=str(r["asset"]) if "asset" in r.keys() else "PDC",
                )
                for r in rows
            ]

    def add_comment(
        self,
        *,
        proposal_id: int,
        user_id: int,
        message: str,
        replied_to: Optional[int] = None,
    ) -> Comment:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO comments (proposal_id, user_id, message, replied_to) "
                "VALUES (?, ?, ?, ?)",
                (proposal_id, user_id, message.strip(), replied_to),
            )
            row = conn.execute(
                "SELECT c.*, u.username AS author FROM comments c "
                "JOIN users u ON u.id = c.user_id WHERE c.id = ?",
                (int(cur.lastrowid),),
            ).fetchone()
            return self._row_comment(row)

    def list_comments(self, proposal_id: int) -> list[Comment]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT c.*, u.username AS author FROM comments c "
                "JOIN users u ON u.id = c.user_id "
                "WHERE c.proposal_id = ? ORDER BY c.created_at ASC",
                (proposal_id,),
            ).fetchall()
            return [self._row_comment(r) for r in rows]

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def _user_from_id(self, conn: sqlite3.Connection, user_id: int) -> User:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise KeyError(f"user {user_id} missing")
        return self._row_user(row)

    def _proposal_from_id(
        self, conn: sqlite3.Connection, proposal_id: int
    ) -> Optional[Proposal]:
        row = conn.execute(
            "SELECT p.*, u.username AS author, "
            "COALESCE((SELECT SUM(amount_atomic) FROM deposits d WHERE d.proposal_id = p.id), 0) "
            "AS received_atomic, "
            "COALESCE((SELECT SUM(amount_atomic) FROM payouts o WHERE o.proposal_id = p.id), 0) "
            "AS paid_atomic, "
            "COALESCE((SELECT SUM(amount_atomic) FROM payouts o "
            "WHERE o.proposal_id = p.id AND o.tx_hash LIKE 'pending:%'), 0) "
            "AS pending_atomic "
            "FROM proposals p JOIN users u ON u.id = p.user_id WHERE p.id = ?",
            (proposal_id,),
        ).fetchone()
        return self._row_proposal(row) if row else None

    @staticmethod
    def _row_user(row: sqlite3.Row) -> User:
        keys = row.keys()
        return User(
            id=int(row["id"]),
            username=str(row["username"]),
            email=str(row["email"]),
            password_hash=str(row["password_hash"]),
            is_admin=bool(row["is_admin"]),
            totp_enabled=bool(row["totp_enabled"]) if "totp_enabled" in keys else False,
            created_at=str(row["created_at"]) if "created_at" in keys else "",
        )

    @staticmethod
    def _row_proposal(row: sqlite3.Row) -> Proposal:
        keys = row.keys()
        return Proposal(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            headline=str(row["headline"]),
            content=str(row["content"]),
            category=str(row["category"]),
            status=int(row["status"]),
            funds_target_atomic=int(row["funds_target_atomic"]),
            payout_address=str(row["payout_address"] or ""),
            payment_id=str(row["payment_id"]) if row["payment_id"] else None,
            integrated_address=str(row["integrated_address"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            author=str(row["author"]) if "author" in keys else "",
            received_atomic=int(row["received_atomic"])
            if "received_atomic" in keys
            else 0,
            paid_atomic=int(row["paid_atomic"]) if "paid_atomic" in keys else 0,
            pending_atomic=int(row["pending_atomic"])
            if "pending_atomic" in keys
            else 0,
        )

    @staticmethod
    def _row_comment(row: sqlite3.Row) -> Comment:
        return Comment(
            id=int(row["id"]),
            proposal_id=int(row["proposal_id"]),
            user_id=int(row["user_id"]),
            message=str(row["message"]),
            replied_to=int(row["replied_to"]) if row["replied_to"] is not None else None,
            created_at=str(row["created_at"]),
            author=str(row["author"]) if "author" in row.keys() else "",
        )

    def _proposal_asset_from_id(
        self, conn: sqlite3.Connection, asset_row_id: int
    ) -> ProposalAsset:
        row = conn.execute(
            "SELECT a.*, 0 AS received_atomic, 0 AS paid_atomic, 0 AS pending_atomic "
            "FROM proposal_assets a WHERE a.id = ?",
            (asset_row_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"proposal_asset {asset_row_id} missing")
        return self._row_proposal_asset(row)

    @staticmethod
    def _row_proposal_asset(row: sqlite3.Row) -> ProposalAsset:
        keys = row.keys()
        return ProposalAsset(
            id=int(row["id"]),
            proposal_id=int(row["proposal_id"]),
            asset=str(row["asset"]),
            funds_target_atomic=int(row["funds_target_atomic"]),
            payout_address=str(row["payout_address"] or ""),
            payment_id=str(row["payment_id"]) if row["payment_id"] else None,
            integrated_address=str(row["integrated_address"] or ""),
            btc_address=str(row["btc_address"] or ""),
            received_atomic=int(row["received_atomic"])
            if "received_atomic" in keys
            else 0,
            paid_atomic=int(row["paid_atomic"]) if "paid_atomic" in keys else 0,
            pending_atomic=int(row["pending_atomic"])
            if "pending_atomic" in keys
            else 0,
        )
