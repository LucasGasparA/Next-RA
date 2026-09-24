"""Histórico datado da nota AR — recurso novo do app web (app.py).

Um snapshot por dia (upsert por `date`): clicar em "Atualizar agora" várias
vezes no mesmo dia não deve poluir o gráfico de tendência com pontos
duplicados, então a atualização mais recente do dia sobrescreve a entrada
daquele dia. Gerado a partir de main.compute_ar_window(db, now), que já
retorna todos os campos necessários.
"""
from datetime import datetime

import psycopg

SNAPSHOT_FIELDS = (
    "ar", "ma", "ir", "is_pct", "in_pct", "label", "label_color",
    "window_start", "window_end", "n_evaluated", "total",
)


def append_snapshot(conn: psycopg.Connection, ar_window: dict, now: datetime) -> None:
    values = {field: ar_window.get(field) for field in SNAPSHOT_FIELDS}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ar_snapshots
                (date, at, ar, ma, ir, is_pct, in_pct, label, label_color,
                 window_start, window_end, n_evaluated, total)
            VALUES
                (%(date)s, %(at)s, %(ar)s, %(ma)s, %(ir)s, %(is_pct)s, %(in_pct)s,
                 %(label)s, %(label_color)s, %(window_start)s, %(window_end)s,
                 %(n_evaluated)s, %(total)s)
            ON CONFLICT (date) DO UPDATE SET
                at = EXCLUDED.at, ar = EXCLUDED.ar, ma = EXCLUDED.ma, ir = EXCLUDED.ir,
                is_pct = EXCLUDED.is_pct, in_pct = EXCLUDED.in_pct,
                label = EXCLUDED.label, label_color = EXCLUDED.label_color,
                window_start = EXCLUDED.window_start, window_end = EXCLUDED.window_end,
                n_evaluated = EXCLUDED.n_evaluated, total = EXCLUDED.total
            """,
            {"date": now.date(), "at": now, **values},
        )
    conn.commit()


def load_snapshots(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ar_snapshots ORDER BY date")
        rows = cur.fetchall()

    snapshots = []
    for row in rows:
        snap = dict(row)
        snap["date"] = snap["date"].isoformat()
        snap["at"] = snap["at"].isoformat(timespec="seconds")
        for field in ("ar", "ma", "ir", "is_pct", "in_pct"):
            if snap[field] is not None:
                snap[field] = float(snap[field])
        snapshots.append(snap)
    return snapshots
