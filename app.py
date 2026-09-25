"""Radar de Reclamações — versão web (Flask).

Embrulha a lógica de main.py (fórmula do AR, merge do banco, escaping HTML,
lock de concorrência, CSS) numa camada web: login por senha única, um campo
pra colar o refresh_token (salvo cifrado no Postgres) e um histórico datado
da nota AR. O CLI local original (main.py --serve/--demo, modo legado)
continua funcionando exatamente como antes, sem depender deste arquivo.
"""
import hmac
import os
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, redirect, request, session, url_for

import ar_history
import main
import store
import token_store

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") != "development",
)

main.DEMO_MODE = os.environ.get("RADAR_DEMO") == "1"


def _wait_for_schema(retries: int = 10, delay: float = 1.5) -> None:
    """Espera o Postgres do docker-compose ficar pronto antes de servir."""
    last_exc = None
    for _ in range(retries):
        try:
            conn = store.get_connection()
            try:
                store.init_schema(conn)
            finally:
                conn.close()
            return
        except Exception as exc:  # psycopg.OperationalError etc.
            last_exc = exc
            time.sleep(delay)
    raise RuntimeError(f"Não consegui conectar ao Postgres ({os.environ.get('DATABASE_URL')}): {last_exc}")


_wait_for_schema()


# ---------------------------------------------------------------------------
# AUTENTICAÇÃO (senha única compartilhada)
# ---------------------------------------------------------------------------
AUTH_CSS = """
  .auth-wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center; background: var(--canvas); position: relative; }
  .auth-card { background: var(--card); border: 1px solid var(--border);
    border-radius: 20px; box-shadow: var(--glow); padding: 30px 32px; width: 360px; position: relative; }
  .auth-wordmark { display: flex; align-items: center; gap: 8px; font-family: 'Manrope', sans-serif;
    font-weight: 800; font-size: 18px; color: var(--ink); margin-bottom: 4px; }
  .auth-wordmark .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--violet-bright); flex-shrink: 0; box-shadow: 0 0 10px var(--violet-bright); }
  .auth-meta { font-size: 12.5px; color: var(--ink-dim); margin: 0 0 20px; }
  .auth-title { font-size: 15px; font-weight: 700; margin: 0 0 16px; color: var(--ink); }
  .auth-card input, .auth-card textarea {
    width: 100%; border: 1px solid var(--border-strong); border-radius: 8px;
    background: var(--canvas); color: var(--ink);
    padding: 9px 11px; font-size: 13px; font-family: inherit; margin-bottom: 12px; box-sizing: border-box;
  }
  .auth-card input:focus, .auth-card textarea:focus { outline: 2px solid var(--violet-bright); outline-offset: 1px; }
  .auth-card textarea { min-height: 90px; resize: vertical; }
  .auth-card button { width: 100%; }
  .auth-error { color: var(--coral); font-size: 12px; margin: 0 0 12px; }
  .auth-note { color: var(--ink-dim); font-size: 12px; margin: 0 0 14px; line-height: 1.5; }
"""


def render_login_page(error: str | None) -> str:
    error_html = f'<p class="auth-error">{main.esc(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8">
<title>Next RA</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
{main.FONT_LINKS}
<style>{main.CSS}{AUTH_CSS}</style></head>
<body><div class="bg-glow"></div><div class="auth-wrap"><div class="auth-card">
  <div class="auth-wordmark"><span class="dot"></span>Next RA</div>
  <p class="auth-meta">Painel de reputação · Reclame Aqui</p>
  {error_html}
  <form method="POST">
    <input type="password" name="password" placeholder="Senha" autofocus required>
    <button class="btn" type="submit">Entrar</button>
  </form>
</div></div></body></html>"""


def render_setup_page(error: str | None, prefill: str) -> str:
    error_html = f'<p class="auth-error">{main.esc(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8">
<title>Next RA</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
{main.FONT_LINKS}
<style>{main.CSS}{AUTH_CSS}</style></head>
<body><div class="bg-glow"></div><div class="auth-wrap"><div class="auth-card" style="width:430px">
  <div class="auth-wordmark"><span class="dot"></span>Next RA</div>
  <p class="auth-meta">Conectar ao Reclame Aqui</p>
  <p class="auth-note">
    Cole o <code>refresh_token</code> capturado pela extensão do navegador (ou obtido via
    Postman/DevTools com <code>scope=openid offline_access</code>). Ele fica salvo cifrado
    no servidor — não precisa colar de novo a cada visita, só se expirar ou for revogado.
  </p>
  {error_html}
  <form method="POST">
    <textarea name="refresh_token" placeholder="Cole o refresh_token aqui" autofocus>{main.esc(prefill)}</textarea>
    <button class="btn" id="setupBtn" type="submit">Conectar</button>
  </form>
</div></div>
<script>
document.querySelector('form').addEventListener('submit', function () {{
  var btn = document.getElementById('setupBtn');
  btn.disabled = true; btn.textContent = 'Conectando...';
}});
</script>
</body></html>"""


@app.before_request
def require_login():
    if request.path == "/login" or request.path.startswith("/static/"):
        return None
    if not session.get("authenticated"):
        return redirect(url_for("login"))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if hmac.compare_digest(password, os.environ["APP_PASSWORD"]):
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        error = "Senha incorreta."
    return render_login_page(error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# CICLO DE ATUALIZAÇÃO (fetch -> merge -> save -> snapshot do AR)
# ---------------------------------------------------------------------------
def run_refresh_cycle(conn) -> dict:
    fetched, complete = main.fetch_complaints_now(
        token_loader=lambda: token_store.load_refresh_token(conn),
        token_saver=lambda t: token_store.save_refresh_token(conn, t),
    )
    with main.DB_LOCK:
        db = store.load_db(conn)
        db = main.merge_complaints(db, fetched, complete=complete)
        store.save_db(conn, db)
        data = main.build_dashboard_data(db)
    ar_history.append_snapshot(conn, data["ar"], datetime.now())
    return data


def _apply_new_token(token: str) -> str | None:
    """Salva o token e roda o primeiro ciclo de fetch. Retorna a mensagem de
    erro em caso de falha, ou None se deu tudo certo. Usado tanto por /setup
    (primeira conexão, página cheia) quanto por /api/token (troca de token
    pelo modal do dashboard, via fetch)."""
    if not token and not main.DEMO_MODE:
        return "Cole o refresh_token antes de continuar."
    conn = store.get_connection()
    try:
        if not main.DEMO_MODE:
            token_store.save_refresh_token(conn, token)
        try:
            run_refresh_cycle(conn)
        except RuntimeError as exc:
            if not main.DEMO_MODE:
                token_store.clear_refresh_token(conn)  # não deixa token ruim salvo
            return str(exc)
        return None
    finally:
        conn.close()


@app.route("/setup", methods=["GET", "POST"])
def setup():
    error = None
    prefill = ""
    if request.method == "POST":
        token = (request.form.get("refresh_token") or "").strip()
        prefill = token
        error = _apply_new_token(token)
        if error is None:
            return redirect(url_for("dashboard", toast="token_ok"))
    return render_setup_page(error, prefill)


@app.route("/api/token", methods=["POST"])
def api_token():
    body = request.get_json(silent=True) or {}
    token = (body.get("refresh_token") or "").strip()
    error = _apply_new_token(token)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
def render_ar_trend_panel(snapshots: list[dict]) -> str:
    if len(snapshots) < 2:
        body = (
            '<p class="empty">Ainda não há histórico suficiente — volte depois de '
            "atualizações em pelo menos dois dias diferentes.</p>"
        )
    else:
        usable = [s for s in snapshots if s["ar"] is not None]
        width, height, pad = 640, 160, 24
        n = len(usable)

        def x(i):
            return pad + (width - 2 * pad) * (i / (n - 1)) if n > 1 else width / 2

        def y(v):
            return height - pad - (height - 2 * pad) * (v / 10)  # AR é sempre 0-10

        points = " ".join(f"{x(i):.1f},{y(s['ar']):.1f}" for i, s in enumerate(usable))
        circles = []
        for i, s in enumerate(usable):
            tip = f"{s['date']} · AR {s['ar']:.2f} · {s['label']}"
            circles.append(
                f'<circle cx="{x(i):.1f}" cy="{y(s["ar"]):.1f}" r="3.5" '
                f'fill="{main.score_color(s["ar"])}"><title>{main.esc(tip)}</title></circle>'
            )
        body = f"""
        <svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;display:block">
          <polyline points="{points}" fill="none" stroke="var(--violet)" stroke-width="2"/>
          {''.join(circles)}
        </svg>
        <div class="proj-progress-top"><span>{main.esc(usable[0]['date'])}</span><span>{main.esc(usable[-1]['date'])}</span></div>
        """
    return f"""
  <div class="panel">
    <p class="panel-title">Histórico da nota AR</p>
    <p class="panel-note">Um ponto por dia em que o painel foi atualizado &middot; eixo fixo de 0 a 10</p>
    {body}
  </div>"""


@app.route("/")
def dashboard():
    conn = store.get_connection()
    try:
        if not main.DEMO_MODE and token_store.load_refresh_token(conn) is None:
            return redirect(url_for("setup"))
        with main.DB_LOCK:
            db = store.load_db(conn)
            data = main.build_dashboard_data(db)
        extra_panel_html = render_ar_trend_panel(ar_history.load_snapshots(conn))
        extra_header_html = (
            '<button type="button" class="link-btn" onclick="openTokenModal()">Trocar token</button>'
            '<a href="/logout">Sair</a>'
        )
        return main.render_dashboard(
            data,
            interactive=True,
            extra_panel_html=extra_panel_html,
            extra_header_html=extra_header_html,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API (mesmas rotas do DashboardHandler original, agora sobre Postgres)
# ---------------------------------------------------------------------------
@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    conn = store.get_connection()
    try:
        data = run_refresh_cycle(conn)
        return jsonify({"ok": True, "total_ativas": data["total_active"]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/tag", methods=["POST"])
def api_tag():
    body = request.get_json(silent=True) or {}
    cid = str(body.get("id", ""))
    tag = body.get("tag") or None
    conn = store.get_connection()
    try:
        if store.set_tag(conn, cid, tag):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "reclamação não encontrada"}), 404
    finally:
        conn.close()


@app.route("/api/tags", methods=["GET", "POST"])
def api_tags():
    conn = store.get_connection()
    try:
        if request.method == "GET":
            return jsonify({"tags": store.list_tags(conn)})
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()[:60]
        tags = store.add_tag(conn, name) if name else store.list_tags(conn)
        return jsonify({"ok": True, "tags": tags})
    finally:
        conn.close()


@app.route("/api/categoria", methods=["POST"])
def api_categoria():
    body = request.get_json(silent=True) or {}
    cid = str(body.get("id", ""))
    categoria = body.get("categoria") or None
    conn = store.get_connection()
    try:
        if store.set_categoria(conn, cid, categoria):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "reclamação não encontrada"}), 404
    finally:
        conn.close()


@app.route("/api/categorias", methods=["GET", "POST"])
def api_categorias():
    conn = store.get_connection()
    try:
        if request.method == "GET":
            return jsonify({"categorias": store.list_categorias(conn)})
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()[:60]
        categorias = store.add_categoria(conn, name) if name else store.list_categorias(conn)
        return jsonify({"ok": True, "categorias": categorias})
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_ENV") == "development")
