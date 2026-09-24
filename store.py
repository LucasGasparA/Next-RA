"""Camada de persistência em Postgres para o app web (app.py).

Reconstrói/persiste exatamente o mesmo formato de dict que main.py já usa via
load_db()/save_db() (arquivo JSON): {"tags": [...], "complaints": {...},
"monthly_overrides": {...}}. Assim, toda a lógica de métricas em main.py
(merge_complaints, stats_for_group, compute_ar_window, ...) continua operando
em memória sobre um dict puro, sem saber que existe um banco atrás — só a
camada de carregar/salvar esse dict mora aqui.

O CLI local (main.py --serve, --demo, modo legado) não usa este módulo — ele
continua com load_db/save_db originais em JSON. Este módulo é usado só pelo
app.py (versão web, rodando em Docker com Postgres).
"""
import json
import os

import psycopg
from psycopg.rows import dict_row

DEFAULT_TAGS = ["Suporte Técnico", "CSM", "Aluno", "Comercial", "Produto"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS complaints (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    tag_origem TEXT,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    deactivated_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS tags (
    name TEXT PRIMARY KEY,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS monthly_overrides (
    month_key TEXT PRIMARY KEY,
    sla TEXT
);
CREATE TABLE IF NOT EXISTS ar_snapshots (
    date DATE PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL,
    ar NUMERIC, ma NUMERIC, ir NUMERIC, is_pct NUMERIC, in_pct NUMERIC,
    label TEXT, label_color TEXT,
    window_start TEXT, window_end TEXT,
    n_evaluated INTEGER, total INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Campos gravados em colunas próprias — o resto do registro vai pra JSONB "data".
RECORD_COLUMNS = ("id", "tag_origem", "first_seen", "last_seen", "deactivated_at")


def get_connection() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
        cur.execute("SELECT COUNT(*) AS n FROM tags")
        if cur.fetchone()["n"] == 0:
            for i, tag in enumerate(DEFAULT_TAGS):
                cur.execute(
                    "INSERT INTO tags (name, position) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (tag, i),
                )
    conn.commit()


def _iso(value):
    return value.isoformat(timespec="seconds") if value is not None else None


def load_db(conn: psycopg.Connection) -> dict:
    complaints = {}
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM complaints")
        for row in cur.fetchall():
            record = dict(row["data"])
            record["id"] = row["id"]
            record["tag_origem"] = row["tag_origem"]
            record["first_seen"] = _iso(row["first_seen"])
            record["last_seen"] = _iso(row["last_seen"])
            record["deactivated_at"] = _iso(row["deactivated_at"])
            complaints[row["id"]] = record

        cur.execute("SELECT name FROM tags ORDER BY position")
        tags = [r["name"] for r in cur.fetchall()]

        cur.execute("SELECT month_key, sla FROM monthly_overrides")
        monthly_overrides = {r["month_key"]: {"sla": r["sla"]} for r in cur.fetchall()}

        cur.execute("SELECT value FROM settings WHERE key = 'last_sync'")
        row = cur.fetchone()

    db = {
        "tags": tags or list(DEFAULT_TAGS),
        "complaints": complaints,
        "monthly_overrides": monthly_overrides,
    }
    if row:
        db["last_sync"] = json.loads(row["value"])
    return db


def save_db(conn: psycopg.Connection, db: dict) -> None:
    """Upsert de tudo — seguro de repetir, idempotente. Usado só no ciclo de
    fetch/merge (main.merge_complaints), não em edições pontuais como uma tag
    (ver set_tag/add_tag, que fazem UPDATE/INSERT direcionados sem recarregar
    o dict inteiro)."""
    with conn.cursor() as cur:
        for cid, record in db["complaints"].items():
            data = {k: v for k, v in record.items() if k not in RECORD_COLUMNS}
            cur.execute(
                """
                INSERT INTO complaints (id, data, tag_origem, first_seen, last_seen, deactivated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    data = EXCLUDED.data,
                    tag_origem = EXCLUDED.tag_origem,
                    first_seen = EXCLUDED.first_seen,
                    last_seen = EXCLUDED.last_seen,
                    deactivated_at = EXCLUDED.deactivated_at
                """,
                (
                    cid,
                    json.dumps(data, ensure_ascii=False),
                    record.get("tag_origem"),
                    record.get("first_seen"),
                    record.get("last_seen"),
                    record.get("deactivated_at"),
                ),
            )

        cur.execute("SELECT name FROM tags")
        existing_tags = {r["name"] for r in cur.fetchall()}
        cur.execute("SELECT COALESCE(MAX(position), -1) AS m FROM tags")
        next_position = cur.fetchone()["m"] + 1
        for tag in db.get("tags", []):
            if tag not in existing_tags:
                cur.execute(
                    "INSERT INTO tags (name, position) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (tag, next_position),
                )
                next_position += 1
                existing_tags.add(tag)

        for month_key, override in db.get("monthly_overrides", {}).items():
            cur.execute(
                """
                INSERT INTO monthly_overrides (month_key, sla) VALUES (%s, %s)
                ON CONFLICT (month_key) DO UPDATE SET sla = EXCLUDED.sla
                """,
                (month_key, override.get("sla")),
            )

        if "last_sync" in db:
            cur.execute(
                """
                INSERT INTO settings (key, value) VALUES ('last_sync', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (json.dumps(db["last_sync"]),),
            )
    conn.commit()


def list_tags(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM tags ORDER BY position")
        return [r["name"] for r in cur.fetchall()]


def add_tag(conn: psycopg.Connection, name: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM tags WHERE name = %s", (name,))
        if cur.fetchone() is None:
            cur.execute("SELECT COALESCE(MAX(position), -1) AS m FROM tags")
            next_position = cur.fetchone()["m"] + 1
            cur.execute(
                "INSERT INTO tags (name, position) VALUES (%s, %s) "
                "ON CONFLICT (name) DO NOTHING",
                (name, next_position),
            )
    conn.commit()
    return list_tags(conn)


def set_tag(conn: psycopg.Connection, cid: str, tag: str | None) -> bool:
    """UPDATE pontual numa reclamação — não recarrega/reescreve o dict inteiro."""
    with conn.cursor() as cur:
        cur.execute("UPDATE complaints SET tag_origem = %s WHERE id = %s", (tag, cid))
        updated = cur.rowcount > 0
    conn.commit()
    if updated and tag:
        add_tag(conn, tag)
    return updated


def get_setting(conn: psycopg.Connection, key: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row else None


def set_setting(conn: psycopg.Connection, key: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )
    conn.commit()


def delete_setting(conn: psycopg.Connection, key: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM settings WHERE key = %s", (key,))
    conn.commit()
