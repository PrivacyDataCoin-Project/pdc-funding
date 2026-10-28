"""SQLite persistence for proposals, deposits, payouts, users, comments."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
    credited_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(tx_hash, payment_id)
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

CREATE INDEX IF NOT EXISTS ix_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS ix_proposals_payment_id ON proposals(payment_id);
CREATE INDEX IF NOT EXISTS ix_deposits_proposal ON deposits(proposal_id);
CREATE INDEX IF NOT EXISTS ix_comments_proposal ON comments(proposal_id);
"""


@dataclass
class User:
    id: int
    username: str
    email: str
    password_hash: str
    is_admin: bool


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


@dataclass
class Payout:
    id: int
    proposal_id: int
    amount_atomic: int
    to_address: str
    tx_hash: str
    milestone_note: str
    created_at: str


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def user_count(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"])

    def create_user(
        self,
        username: str,
        email: str,
        password_hash: str,
        *,
        is_admin: bool = False,
    ) -> User:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, email, password_hash, is_admin) "
                "VALUES (?, ?, ?, ?)",
                (username.strip(), email.strip().lower(), password_hash, int(is_admin)),
            )
            return self._user_from_id(conn, int(cur.lastrowid))

    def get_user(self, user_id: int) -> Optional[User]:
        with self._lock, self._connect() as conn:
            return self._user_from_id(conn, user_id)

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

    def update_proposal_status(self, proposal_id: int, status: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE proposals SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, proposal_id),
            )

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
            "AS paid_atomic "
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
                "SELECT p.*, u.username AS author, 0 AS received_atomic, 0 AS paid_atomic "
                "FROM proposals p JOIN users u ON u.id = p.user_id "
                "WHERE p.payment_id IS NOT NULL AND p.status IN (2, 3)"
            ).fetchall()
            return [self._row_proposal(r) for r in rows]

    def credit_deposit(
        self,
        *,
        proposal_id: int,
        payment_id: str,
        tx_hash: str,
        amount_atomic: int,
        block_height: int,
    ) -> bool:
        """Insert deposit if new. Returns True when credited."""
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO deposits "
                    "(proposal_id, payment_id, tx_hash, amount_atomic, block_height) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        proposal_id,
                        payment_id.lower(),
                        tx_hash,
                        amount_atomic,
                        block_height,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

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
                )
                for r in rows
            ]

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
            "AS paid_atomic "
            "FROM proposals p JOIN users u ON u.id = p.user_id WHERE p.id = ?",
            (proposal_id,),
        ).fetchone()
        return self._row_proposal(row) if row else None

    @staticmethod
    def _row_user(row: sqlite3.Row) -> User:
        return User(
            id=int(row["id"]),
            username=str(row["username"]),
            email=str(row["email"]),
            password_hash=str(row["password_hash"]),
            is_admin=bool(row["is_admin"]),
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
