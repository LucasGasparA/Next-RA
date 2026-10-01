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
DEFAULT_CUSTOM_STATUSES = [("Moderada", "#8B7FA3"), ("Removida", "#FF6B6D")]
STATUS_COLOR_PALETTE = ["#8B7FA3", "#FF6B6D", "#5AC8C8", "#D68C45", "#6C8CD5", "#B88BD6", "#4FB0E0"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS complaints (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    tag_origem TEXT,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    deactivated_at TIMESTAMPTZ
);
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS categoria TEXT;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS responsavel TEXT;
CREATE TABLE IF NOT EXISTS tags (
    name TEXT PRIMARY KEY,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS categorias (
    name TEXT PRIMARY KEY,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS responsaveis (
    name TEXT PRIMARY KEY,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_statuses (
    name TEXT PRIMARY KEY,
    color TEXT NOT NULL,
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
RECORD_COLUMNS = ("id", "tag_origem", "categoria", "responsavel", "first_seen", "last_seen", "deactivated_at")


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
        cur.execute("SELECT COUNT(*) AS n FROM custom_statuses")
        if cur.fetchone()["n"] == 0:
            for i, (name, color) in enumerate(DEFAULT_CUSTOM_STATUSES):
                cur.execute(
                    "INSERT INTO custom_statuses (name, color, position) VALUES (%s, %s, %s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (name, color, i),
                )
    conn.commit()


def _iso(value):
    return value.isoformat(timespec="seconds") if value is not None else None


def _encode_origins(origins) -> str | None:
    """Uma reclamação pode ter várias origens — guarda como lista em JSON na
    mesma coluna TEXT (sem migração de schema)."""
    origins = [o for o in (origins or []) if o]
    if not origins:
        return None
    return json.dumps(origins, ensure_ascii=False)


def _decode_origins(raw) -> list[str]:
    """Tolerante a dado legado: antes dessa mudança, a coluna guardava uma
    string única (não um JSON de lista) — nesse caso vira lista de 1 item."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
    except (json.JSONDecodeError, TypeError):
        pass
    return [raw]


def load_db(conn: psycopg.Connection) -> dict:
    complaints = {}
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM complaints")
        for row in cur.fetchall():
            record = dict(row["data"])
            record["id"] = row["id"]
            record["tag_origem"] = _decode_origins(row["tag_origem"])
            record["categoria"] = row["categoria"]
            record["responsavel"] = row["responsavel"]
            record["first_seen"] = _iso(row["first_seen"])
            record["last_seen"] = _iso(row["last_seen"])
            record["deactivated_at"] = _iso(row["deactivated_at"])
            complaints[row["id"]] = record

        cur.execute("SELECT name FROM tags ORDER BY position")
        tags = [r["name"] for r in cur.fetchall()]

        cur.execute("SELECT name FROM categorias ORDER BY position")
        categorias = [r["name"] for r in cur.fetchall()]

        cur.execute("SELECT name FROM responsaveis ORDER BY position")
        responsaveis = [r["name"] for r in cur.fetchall()]

        cur.execute("SELECT name, color FROM custom_statuses ORDER BY position")
        custom_statuses = [{"name": r["name"], "color": r["color"]} for r in cur.fetchall()]

        cur.execute("SELECT month_key, sla FROM monthly_overrides")
        monthly_overrides = {r["month_key"]: {"sla": r["sla"]} for r in cur.fetchall()}

        cur.execute("SELECT value FROM settings WHERE key = 'last_sync'")
        row = cur.fetchone()

    db = {
        "tags": tags or list(DEFAULT_TAGS),
        "categorias": categorias,
        "responsaveis": responsaveis,
        "custom_statuses": custom_statuses,
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
                INSERT INTO complaints (id, data, tag_origem, categoria, responsavel, first_seen, last_seen, deactivated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    data = EXCLUDED.data,
                    tag_origem = EXCLUDED.tag_origem,
                    categoria = EXCLUDED.categoria,
                    responsavel = EXCLUDED.responsavel,
                    first_seen = EXCLUDED.first_seen,
                    last_seen = EXCLUDED.last_seen,
                    deactivated_at = EXCLUDED.deactivated_at
                """,
                (
                    cid,
                    json.dumps(data, ensure_ascii=False),
                    _encode_origins(record.get("tag_origem")),
                    record.get("categoria"),
                    record.get("responsavel"),
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

        cur.execute("SELECT name FROM categorias")
        existing_categorias = {r["name"] for r in cur.fetchall()}
        cur.execute("SELECT COALESCE(MAX(position), -1) AS m FROM categorias")
        next_cat_position = cur.fetchone()["m"] + 1
        for categoria in db.get("categorias", []):
            if categoria not in existing_categorias:
                cur.execute(
                    "INSERT INTO categorias (name, position) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (categoria, next_cat_position),
                )
                next_cat_position += 1
                existing_categorias.add(categoria)

        cur.execute("SELECT name FROM responsaveis")
        existing_responsaveis = {r["name"] for r in cur.fetchall()}
        cur.execute("SELECT COALESCE(MAX(position), -1) AS m FROM responsaveis")
        next_resp_position = cur.fetchone()["m"] + 1
        for nome in db.get("responsaveis", []):
            if nome not in existing_responsaveis:
                cur.execute(
                    "INSERT INTO responsaveis (name, position) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (nome, next_resp_position),
                )
                next_resp_position += 1
                existing_responsaveis.add(nome)

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


# ---------------------------------------------------------------------------
# Listas geridas (Origens/Motivos/Responsáveis) — todas com o mesmo formato
# de tabela (name TEXT PRIMARY KEY, position INTEGER), por isso reaproveitam
# as mesmas 4 operações genéricas abaixo.
# ---------------------------------------------------------------------------
def _list_named(conn: psycopg.Connection, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT name FROM {table} ORDER BY position")
        return [r["name"] for r in cur.fetchall()]


def _add_named(conn: psycopg.Connection, table: str, name: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM {table} WHERE name = %s", (name,))
        if cur.fetchone() is None:
            cur.execute(f"SELECT COALESCE(MAX(position), -1) AS m FROM {table}")
            next_position = cur.fetchone()["m"] + 1
            cur.execute(
                f"INSERT INTO {table} (name, position) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
                (name, next_position),
            )
    conn.commit()
    return _list_named(conn, table)


def _rename_named(conn: psycopg.Connection, table: str, old: str, new: str) -> bool:
    """False se `new` já existir (e for diferente de `old`) ou se `old` não existir."""
    with conn.cursor() as cur:
        if new != old:
            cur.execute(f"SELECT 1 FROM {table} WHERE name = %s", (new,))
            if cur.fetchone() is not None:
                return False
        cur.execute(f"UPDATE {table} SET name = %s WHERE name = %s", (new, old))
        updated = cur.rowcount > 0
    conn.commit()
    return updated


def _delete_named(conn: psycopg.Connection, table: str, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {table} WHERE name = %s", (name,))
    conn.commit()


def list_tags(conn: psycopg.Connection) -> list[str]:
    return _list_named(conn, "tags")


def add_tag(conn: psycopg.Connection, name: str) -> list[str]:
    return _add_named(conn, "tags", name)


def rename_tag(conn: psycopg.Connection, old: str, new: str) -> bool:
    return _rename_named(conn, "tags", old, new)


def delete_tag(conn: psycopg.Connection, name: str) -> None:
    _delete_named(conn, "tags", name)


def list_categorias(conn: psycopg.Connection) -> list[str]:
    return _list_named(conn, "categorias")


def add_categoria(conn: psycopg.Connection, name: str) -> list[str]:
    return _add_named(conn, "categorias", name)


def rename_categoria(conn: psycopg.Connection, old: str, new: str) -> bool:
    return _rename_named(conn, "categorias", old, new)


def delete_categoria(conn: psycopg.Connection, name: str) -> None:
    _delete_named(conn, "categorias", name)


def list_responsaveis(conn: psycopg.Connection) -> list[str]:
    return _list_named(conn, "responsaveis")


def add_responsavel(conn: psycopg.Connection, name: str) -> list[str]:
    return _add_named(conn, "responsaveis", name)


def rename_responsavel(conn: psycopg.Connection, old: str, new: str) -> bool:
    return _rename_named(conn, "responsaveis", old, new)


def delete_responsavel(conn: psycopg.Connection, name: str) -> None:
    _delete_named(conn, "responsaveis", name)


def list_custom_statuses(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT name, color FROM custom_statuses ORDER BY position")
        return [{"name": r["name"], "color": r["color"]} for r in cur.fetchall()]


def add_custom_status(conn: psycopg.Connection, name: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM custom_statuses WHERE name = %s", (name,))
        if cur.fetchone() is None:
            cur.execute("SELECT COUNT(*) AS n FROM custom_statuses")
            color = STATUS_COLOR_PALETTE[cur.fetchone()["n"] % len(STATUS_COLOR_PALETTE)]
            cur.execute("SELECT COALESCE(MAX(position), -1) AS m FROM custom_statuses")
            next_position = cur.fetchone()["m"] + 1
            cur.execute(
                "INSERT INTO custom_statuses (name, color, position) VALUES (%s, %s, %s) "
                "ON CONFLICT (name) DO NOTHING",
                (name, color, next_position),
            )
    conn.commit()
    return list_custom_statuses(conn)


def rename_custom_status(conn: psycopg.Connection, old: str, new: str) -> bool:
    return _rename_named(conn, "custom_statuses", old, new)


def delete_custom_status(conn: psycopg.Connection, name: str) -> None:
    _delete_named(conn, "custom_statuses", name)


# ---------------------------------------------------------------------------
# Edição pontual de 1 reclamação (sem recarregar o dict inteiro)
# ---------------------------------------------------------------------------
def set_origins(conn: psycopg.Connection, cid: str, origins: list[str]) -> bool:
    encoded = _encode_origins(origins)
    with conn.cursor() as cur:
        cur.execute("UPDATE complaints SET tag_origem = %s WHERE id = %s", (encoded, cid))
        updated = cur.rowcount > 0
    conn.commit()
    if updated:
        for name in (origins or []):
            if name:
                add_tag(conn, name)
    return updated


def set_categoria(conn: psycopg.Connection, cid: str, categoria: str | None) -> bool:
    with conn.cursor() as cur:
        cur.execute("UPDATE complaints SET categoria = %s WHERE id = %s", (categoria, cid))
        updated = cur.rowcount > 0
    conn.commit()
    if updated and categoria:
        add_categoria(conn, categoria)
    return updated


def set_responsavel(conn: psycopg.Connection, cid: str, responsavel: str | None) -> bool:
    with conn.cursor() as cur:
        cur.execute("UPDATE complaints SET responsavel = %s WHERE id = %s", (responsavel, cid))
        updated = cur.rowcount > 0
    conn.commit()
    if updated and responsavel:
        add_responsavel(conn, responsavel)
    return updated


# ---------------------------------------------------------------------------
# Rename com propagação — troca o valor antigo pelo novo em toda reclamação
# que já usava ele, pra não deixar dado "órfão" apontando pra um nome que não
# existe mais na lista gerida. Volume pequeno (centenas de reclamações), por
# isso faz em Python sobre o dict carregado em vez de SQL dedicado.
# ---------------------------------------------------------------------------
def propagate_rename(conn: psycopg.Connection, field: str, old: str, new: str) -> int:
    """field: 'tag_origem' (lista), 'categoria', 'responsavel' ou 'status'."""
    count = 0
    with conn.cursor() as cur:
        if field == "tag_origem":
            cur.execute("SELECT id, tag_origem FROM complaints WHERE tag_origem LIKE %s", (f"%{old}%",))
            for row in cur.fetchall():
                origins = _decode_origins(row["tag_origem"])
                if old not in origins:
                    continue
                new_origins = [new if o == old else o for o in origins]
                # remove duplicata caso `new` já estivesse na lista também
                new_origins = list(dict.fromkeys(new_origins))
                cur.execute(
                    "UPDATE complaints SET tag_origem = %s WHERE id = %s",
                    (_encode_origins(new_origins), row["id"]),
                )
                count += 1
        else:
            column = {"categoria": "categoria", "responsavel": "responsavel", "status": "status"}[field]
            if column == "status":
                cur.execute("UPDATE complaints SET data = jsonb_set(data, '{status}', to_jsonb(%s::text)) WHERE data->>'status' = %s", (new, old))
            else:
                cur.execute(f"UPDATE complaints SET {column} = %s WHERE {column} = %s", (new, old))
            count = cur.rowcount
    conn.commit()
    return count


def count_usage(conn: psycopg.Connection, field: str, name: str) -> int:
    """Quantas reclamações (ativas ou não) usam esse valor — usado antes de
    permitir excluir um item de uma lista gerida."""
    with conn.cursor() as cur:
        if field == "tag_origem":
            cur.execute("SELECT tag_origem FROM complaints WHERE tag_origem LIKE %s", (f"%{name}%",))
            return sum(1 for row in cur.fetchall() if name in _decode_origins(row["tag_origem"]))
        if field == "status":
            cur.execute("SELECT COUNT(*) AS n FROM complaints WHERE data->>'status' = %s", (name,))
            return cur.fetchone()["n"]
        column = {"categoria": "categoria", "responsavel": "responsavel"}[field]
        cur.execute(f"SELECT COUNT(*) AS n FROM complaints WHERE {column} = %s", (name,))
        return cur.fetchone()["n"]


def insert_manual_complaint(conn: psycopg.Connection, record: dict) -> None:
    """INSERT pontual de uma reclamação criada manualmente (ver
    main.build_manual_complaint) — não recarrega/reescreve o dict inteiro,
    mesmo espírito de set_tag/set_categoria."""
    data = {k: v for k, v in record.items() if k not in RECORD_COLUMNS}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO complaints (id, data, tag_origem, categoria, responsavel, first_seen, last_seen, deactivated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                record["id"],
                json.dumps(data, ensure_ascii=False),
                _encode_origins(record.get("tag_origem")),
                record.get("categoria"),
                record.get("responsavel"),
                record.get("first_seen"),
                record.get("last_seen"),
                record.get("deactivated_at"),
            ),
        )
    conn.commit()
    for name in (record.get("tag_origem") or []):
        add_tag(conn, name)
    if record.get("categoria"):
        add_categoria(conn, record["categoria"])
    if record.get("responsavel"):
        add_responsavel(conn, record["responsavel"])


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
