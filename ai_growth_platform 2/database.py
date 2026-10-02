"""SQLite persistence layer with safe connection handling and small helpers."""
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from catalogue import EVENT_WEIGHTS, PRODUCT_CATALOGUE

log = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("AI_GROWTH_DB_PATH", Path(__file__).parent / "data" / "ai_growth.db"))
ALLOWED_TABLES = ("leads", "outreach_drafts", "campaigns", "products", "interactions")
DRAFT_STATUSES = ("draft", "approved", "rejected")


@contextmanager
def get_conn():
    """Yield a connection that commits on success, rolls back on error, always closes."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
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


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            title TEXT,
            industry TEXT,
            geography TEXT,
            pain_points TEXT,
            budget_evidence INTEGER DEFAULT 0,
            authority_evidence INTEGER DEFAULT 0,
            need_evidence INTEGER DEFAULT 0,
            timing_evidence INTEGER DEFAULT 0,
            bant_score INTEGER DEFAULT 0,
            qualification TEXT DEFAULT 'Nurture',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS outreach_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            step TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','rejected')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            objective TEXT,
            audience TEXT,
            budget REAL,
            ctr REAL,
            conversion_rate REAL,
            roi REAL,
            status TEXT DEFAULT 'draft',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS products (
            product_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price_tier TEXT NOT NULL,
            tags TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_interactions_user ON interactions(user_id);
        CREATE INDEX IF NOT EXISTS idx_drafts_status ON outreach_drafts(status);
        """)
        # Migrations for databases created by earlier versions.
        _ensure_column(conn, "leads", "source", "TEXT DEFAULT 'synthetic'")
        _ensure_column(conn, "leads", "buying_signal", "TEXT")
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email ON leads(email)")
        except sqlite3.IntegrityError:
            log.warning("Duplicate lead emails exist; unique index on leads.email was skipped.")


def seed_demo_data() -> None:
    with get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO products(product_id,name,category,price_tier,tags) VALUES(?,?,?,?,?)",
                [(p["product_id"], p["name"], p["category"], p["price_tier"], ",".join(sorted(p["tags"])))
                 for p in PRODUCT_CATALOGUE],
            )

        if conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 0:
            events = [
                ("U001", "P001", "purchase_completed"), ("U001", "P003", "purchase_intent"),
                ("U001", "P005", "product_interest"), ("U002", "P004", "purchase_completed"),
                ("U002", "P005", "add_to_wishlist"), ("U002", "P006", "purchase_intent"),
                ("U003", "P007", "purchase_completed"), ("U003", "P009", "purchase_completed"),
                ("U003", "P013", "pricing_page_visit"), ("U004", "P002", "form_submission"),
                ("U004", "P010", "cart_abandonment"), ("U005", "P003", "demo_request"),
                ("U005", "P006", "email_click"), ("U005", "P014", "purchase_intent"),
                ("U006", "P010", "cart_abandonment"), ("U006", "P011", "product_interest"),
            ]
            conn.executemany(
                "INSERT INTO interactions(user_id,product_id,event_type,weight) VALUES(?,?,?,?)",
                [(u, p, e, EVENT_WEIGHTS[e]) for u, p, e in events],
            )

        if conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0:
            demo = [
                ("Acme Cloud", "Ava Sharma", "ava@acmecloud.example", "VP Sales", "SaaS", "US",
                 "manual prospecting; CRM fragmentation", "demo"),
                ("Northstar AI", "Liam Chen", "liam@northstar.example", "Head of Revenue", "AI", "Canada",
                 "low pipeline visibility; slow follow-up", "demo"),
                ("BlueCart", "Maya Patel", "maya@bluecart.example", "Director of Growth", "E-commerce", "US",
                 "rising CAC; weak retention", "demo"),
            ]
            conn.executemany(
                """INSERT INTO leads(company,name,email,title,industry,geography,pain_points,source)
                   VALUES(?,?,?,?,?,?,?,?)""", demo)


def reset_db() -> None:
    p = Path(DB_PATH)
    if p.exists():
        p.unlink()


# ---------- Queries ----------
def query_df(sql: str, params: Sequence = ()) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def table_df(table: str) -> pd.DataFrame:
    if table not in ALLOWED_TABLES:
        raise ValueError(f"Unknown table: {table}")
    return query_df(f"SELECT * FROM {table}")  # table is whitelisted above


def fetch_interactions() -> list:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT user_id, product_id, weight FROM interactions")]


# ---------- Writes ----------
def save_lead(lead: dict, bant: Optional[dict] = None) -> int:
    """Insert a lead or update it when the email already exists. Returns the lead id."""
    bant = bant or {}
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO leads(company,name,email,title,industry,geography,pain_points,buying_signal,source,
                                 budget_evidence,authority_evidence,need_evidence,timing_evidence,
                                 bant_score,qualification)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(email) DO UPDATE SET
                 title=excluded.title, pain_points=excluded.pain_points,
                 budget_evidence=excluded.budget_evidence, authority_evidence=excluded.authority_evidence,
                 need_evidence=excluded.need_evidence, timing_evidence=excluded.timing_evidence,
                 bant_score=excluded.bant_score, qualification=excluded.qualification""",
            (lead["company"], lead["name"], lead["email"].strip().lower(), lead.get("title"),
             lead.get("industry"), lead.get("geography"), lead.get("pain_points"),
             lead.get("buying_signal"), lead.get("source", "synthetic"),
             bant.get("budget", 0), bant.get("authority", 0), bant.get("need", 0), bant.get("timing", 0),
             bant.get("score", 0), bant.get("label", "Nurture")),
        )
        row = conn.execute("SELECT id FROM leads WHERE email = ?", (lead["email"].strip().lower(),)).fetchone()
        return int(row["id"])


def save_drafts(lead_id: int, drafts: dict) -> int:
    """Store outreach drafts (status='draft'). Nothing is ever sent by this app."""
    steps = [("email_step_1", "Email 1"), ("email_step_2", "Email 2"),
             ("email_step_3", "Email 3"), ("linkedin", "LinkedIn")]
    rows = [(lead_id, label, drafts[key]) for key, label in steps if drafts.get(key)]
    with get_conn() as conn:
        conn.executemany("INSERT INTO outreach_drafts(lead_id,step,body) VALUES(?,?,?)", rows)
    return len(rows)


def set_draft_status(draft_id: int, status: str) -> None:
    if status not in DRAFT_STATUSES:
        raise ValueError(f"Invalid status: {status}")
    with get_conn() as conn:
        conn.execute(
            "UPDATE outreach_drafts SET status=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, draft_id),
        )


def save_campaign(channel, objective, audience, budget, ctr, conversion_rate, roi, status="draft") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO campaigns(channel,objective,audience,budget,ctr,conversion_rate,roi,status)
               VALUES(?,?,?,?,?,?,?,?)""",
            (channel, objective, audience, budget, ctr, conversion_rate, roi, status),
        )
        return int(cur.lastrowid)


def record_interaction(user_id: str, product_id: str, event_type: str) -> None:
    if event_type not in EVENT_WEIGHTS:
        raise ValueError(f"Unknown event type: {event_type}")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO interactions(user_id,product_id,event_type,weight) VALUES(?,?,?,?)",
            (user_id.strip(), product_id, event_type, EVENT_WEIGHTS[event_type]),
        )
