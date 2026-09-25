#!/usr/bin/env python3
"""
Radar de Reclamações — Next Fit / Reclame Aqui
================================================
Servidor local: roda, busca os dados atualizados, guarda tudo num banco local
(data/complaints_db.json) e serve um dashboard interativo em localhost — com
botão de atualizar, edição de tags de origem e cálculo do índice AR (fórmula
oficial do RA, documentada manualmente pelo time) numa janela móvel de 6 meses.

USO:
    python main.py --serve                 # dados reais + servidor local
    python main.py --serve --demo          # dados de exemplo (sample_response.json) + servidor local
    python main.py                         # modo antigo: gera dashboard.html uma vez e sai (sem botões)
    python main.py --demo                  # idem, com dados de exemplo

CONFIGURAÇÃO NECESSÁRIA (ver README.md):
    1. Faça login manual uma vez no navegador/Postman com scope=openid offline_access
       para obter um refresh_token de longa duração (offline token).
    2. Salve esse refresh_token no arquivo refresh_token.txt (mesma pasta deste script).
    3. Preencha COMPLAINTS_API_URL abaixo com a URL exata que você já tem no Postman.
    4. Confirme os nomes dos parâmetros de paginação (PAGE_PARAM / LIMIT_PARAM).

SOBRE O BANCO LOCAL (data/complaints_db.json):
    Guarda o histórico de todas as reclamações já vistas (não só as ativas).
    A cada atualização:
      - reclamação nova entra no banco;
      - reclamação que sumiu da listagem da API é marcada como "desativada"
        (não some do histórico, só passa a não contar nos índices);
      - reclamação que volta a aparecer é reativada automaticamente;
      - a tag de origem (Suporte Técnico / CSM / Aluno / ...) fica salva por
        reclamação e sobrevive entre execuções — só é definida manualmente
        pela UI, a API não fornece essa classificação.
"""

import argparse
import calendar
import html as html_lib
import json
import statistics
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO — ajuste estes valores com base no que você já tem no Postman
# ---------------------------------------------------------------------------
AUTH_URL = "https://www.reclameaqui.com.br/auth/token"
REALM = "reclameaqui-company"

# ID da empresa (header "company-id" exigido pelas rotas do painel) — confirmado
# via DevTools em 31/08/2026.
COMPANY_ID = "p1Btc0t8z6-PpaR3"

# URL real da API de reclamações — confirmada via DevTools em 31/08/2026.
COMPLAINTS_API_URL = "https://iosearch.reclameaqui.com.br/raichu-io-site-search-v1/complains/company"

# Paginação estilo Elasticsearch: "index" = quantos registros pular (0, 10, 20...),
# "offset" = tamanho da página. PAGE_SIZE define os dois — pode tentar um valor
# maior (ex: 50) pra reduzir o número de requisições, mas 10 é o valor confirmado
# funcionando de verdade.
PAGE_SIZE = 10

BASE_DIR = Path(__file__).parent
REFRESH_TOKEN_FILE = BASE_DIR / "refresh_token.txt"
DATA_DIR = BASE_DIR / "data"
DB_FILE = DATA_DIR / "complaints_db.json"
OUTPUT_HTML = BASE_DIR / "dashboard.html"
SAMPLE_FILE = BASE_DIR / "sample_response.json"

DEFAULT_TAGS = ["Suporte Técnico", "CSM", "Aluno", "Comercial", "Produto"]
DEFAULT_CATEGORIES: list[str] = []
ANSWERED_INTERACTION_TYPES = {"ANSWER", "FINAL_ANSWER", "REPLY"}
AR_WINDOW_MONTHS = 6

# A RA não considera o mês corrente na janela de reputação — só o mês anterior
# já entra fechado. Confirmado comparando com o painel oficial em 01/09/2026:
# a janela mostrada era 01/03/2026–31/08/2026 (ou seja, 1 mês de defasagem a
# partir de hoje, incluindo agosto mas não setembro). Um valor de 2 aqui foi
# testado antes e não bateu com o painel real (dava 01/02–31/07, um mês inteiro
# deslocado) — 1 é o valor que reproduz a janela oficial.
AR_WINDOW_LAG_MONTHS = 1

# estado global simples (usado pelo servidor local)
DEMO_MODE = False

# BUGFIX #3 — o ThreadingHTTPServer atende requisições em paralelo, e todo
# endpoint que mexe no banco faz load -> altera -> save. Sem lock, um refresh
# demorado rodando junto com um POST /api/tag fazia a última escrita sobrescrever
# a outra (a tag simplesmente sumia). Este lock serializa o ciclo inteiro de
# leitura+escrita; a parte lenta (a chamada HTTP à API do RA) fica FORA dele.
DB_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# AUTENTICAÇÃO
# ---------------------------------------------------------------------------
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.reclameaqui.com.br",
    "Referer": "https://www.reclameaqui.com.br/",
}

# Headers extras exigidos pelas rotas autenticadas do painel da empresa
# (confirmado via DevTools) — usados só nas chamadas à API de reclamações,
# não na troca de refresh_token.
COMPANY_HEADERS = {
    "company-id": COMPANY_ID,
    "realm": REALM,
}


def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """Troca o refresh_token por um access_token novo (sem captcha)."""
    body = json.dumps({"realm": REALM, "refresh_token": refresh_token}).encode("utf-8")
    req = urllib.request.Request(
        AUTH_URL,
        data=body,
        headers={**BROWSER_HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:800]
        except Exception:
            pass
        print(f"  [debug] resposta de erro do servidor:\n{error_body}\n")
        raise RuntimeError(
            f"Falha ao renovar o token ({e.code}). Se for 401, o refresh_token "
            f"provavelmente expirou/foi revogado — refaça o login (com scope "
            f"offline_access) e atualize {REFRESH_TOKEN_FILE.name}. Se for 403, "
            f"pode ser bloqueio de bot/WAF na chamada — veja o corpo da resposta "
            f"acima e me mostra pra eu ajustar os headers."
        ) from e

    access_token = payload["access_token"]
    new_refresh_token = payload.get("refresh_token", refresh_token)
    return access_token, new_refresh_token


# ---------------------------------------------------------------------------
# BUSCA DAS RECLAMAÇÕES (paginada)
# ---------------------------------------------------------------------------
def fetch_all_complaints(access_token: str) -> tuple[list[dict], bool]:
    """Retorna (itens, coleta_completa).

    coleta_completa=False significa que a paginação terminou com menos itens do
    que o `count` reportado pela API (ou sem count nenhum). Nesse caso o merge
    NÃO pode desativar o que faltou — veja merge_complaints().
    """
    all_items: list[dict] = []
    seen_ids: set[str] = set()
    index = 0
    total_expected = None
    request_body = json.dumps({"filtroModeracao": None, "filtroStatusDeAvaliacao": None}).encode("utf-8")

    while True:
        params = {
            "index": index,
            "deleted": "bool:false",
            "offset": PAGE_SIZE,
            "company": COMPANY_ID,
            "order": "created",
            "orderType": "desc",
        }
        url = f"{COMPLAINTS_API_URL}?{urllib.parse.urlencode(params, safe=':')}"
        req = urllib.request.Request(
            url,
            data=request_body,
            method="POST",
            headers={
                **BROWSER_HEADERS,
                **COMPANY_HEADERS,
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                pass
            print(f"  [debug] falha em index={index} ({e.code}):\n{error_body}\n")
            raise

        items = payload.get("data", [])
        total_expected = payload.get("count", total_expected)

        # dedupe por id — trava de segurança caso a paginação real não incremente
        # exatamente do jeito que a gente assumiu (index += PAGE_SIZE)
        new_items = [it for it in items if str(it.get("id") or it.get("legacyId")) not in seen_ids]
        for it in new_items:
            seen_ids.add(str(it.get("id") or it.get("legacyId")))
        all_items.extend(new_items)

        print(
            f"  index={index}: {len(items)} recebidos, {len(new_items)} novos "
            f"(total até agora: {len(all_items)} de {total_expected if total_expected is not None else '?'})"
        )

        if not items or not new_items:
            break
        if total_expected is not None and len(all_items) >= total_expected:
            break
        index += PAGE_SIZE
        if index > 200_000:  # trava de segurança
            break

    if total_expected is None:
        complete = False
        print(
            "  ⚠️  a API não devolveu 'count' nesta resposta — tratando a coleta como "
            "PARCIAL por segurança (nada será desativado neste ciclo)."
        )
    elif len(all_items) < total_expected:
        complete = False
        print(
            f"  ⚠️  coleta PARCIAL: a API reportou count={total_expected} mas coletamos "
            f"{len(all_items)}. Nenhuma reclamação será desativada neste ciclo — sem isso, "
            f"as {total_expected - len(all_items)} que faltaram sairiam dos índices como se "
            f"tivessem sido removidas do RA."
        )
    else:
        complete = True
        if len(all_items) > total_expected:
            print(
                f"  nota: coletamos {len(all_items)} contra count={total_expected} "
                f"(a listagem provavelmente cresceu durante a paginação)."
            )

    return all_items, complete


def _default_token_loader() -> str | None:
    if not REFRESH_TOKEN_FILE.exists():
        return None
    return REFRESH_TOKEN_FILE.read_text(encoding="utf-8").strip()


def _default_token_saver(token: str) -> None:
    REFRESH_TOKEN_FILE.write_text(token, encoding="utf-8")


def fetch_complaints_now(
    token_loader: Callable[[], str | None] | None = None,
    token_saver: Callable[[str], None] | None = None,
) -> tuple[list[dict], bool]:
    """Busca reclamações — reais (via refresh_token) ou de exemplo, conforme DEMO_MODE.

    token_loader/token_saver permitem trocar de onde o refresh_token vem/vai (por
    padrão, o arquivo REFRESH_TOKEN_FILE) — usado pelo app web (app.py) pra ler/
    gravar o token no Postgres em vez de num arquivo local.

    Retorna (itens, coleta_completa).
    """
    if DEMO_MODE:
        print(f"[demo] carregando dados de exemplo de {SAMPLE_FILE.name} ...")
        payload = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
        return payload["data"], True

    token_loader = token_loader or _default_token_loader
    token_saver = token_saver or _default_token_saver

    refresh_token = token_loader()
    if not refresh_token:
        raise RuntimeError(
            f"Nenhum refresh_token configurado. Crie {REFRESH_TOKEN_FILE.name} com o "
            f"seu refresh_token (offline token) — veja o README.md."
        )

    print("Renovando access_token...")
    access_token, new_refresh_token = refresh_access_token(refresh_token)
    if new_refresh_token != refresh_token:
        token_saver(new_refresh_token)
        print("  refresh_token rotacionado e salvo.")

    print("Buscando reclamações...")
    return fetch_all_complaints(access_token)  # (itens, completa)


# ---------------------------------------------------------------------------
# BANCO DE DADOS LOCAL (data/complaints_db.json)
# ---------------------------------------------------------------------------
def load_db() -> dict:
    if DB_FILE.exists():
        db = json.loads(DB_FILE.read_text(encoding="utf-8"))
        db.setdefault("tags", list(DEFAULT_TAGS))
        db.setdefault("categorias", list(DEFAULT_CATEGORIES))
        db.setdefault("complaints", {})
        db.setdefault("monthly_overrides", {})  # ex: {"2026-06": {"sla": "8 dias e 5 horas"}}
        return db
    return {
        "tags": list(DEFAULT_TAGS),
        "categorias": list(DEFAULT_CATEGORIES),
        "complaints": {},
        "monthly_overrides": {},
    }


def save_db(db: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_complaints(db: dict, fetched: list[dict], complete: bool = True) -> dict:
    """Funde a lista recém-buscada no banco local.

    - novas -> entram com tag_origem=None
    - já existentes -> atualiza os campos da API, preserva tag_origem
    - que sumiram da listagem -> marca deactivated_at (sem apagar do histórico)
    - que reaparecem -> reativa automaticamente (deactivated_at=None)

    BUGFIX #2 — a desativação em massa só acontece com complete=True. Numa coleta
    parcial (paginação que devolveu menos que o `count`), tudo que não veio seria
    marcado como desativado e sairia silenciosamente dos índices, corrompendo o
    histórico de forma permanente e invisível. Com complete=False só atualizamos
    e reativamos, nunca desativamos.
    """
    now = datetime.now().isoformat(timespec="seconds")
    fetched_ids = set()

    for c in fetched:
        cid = c.get("id") or c.get("legacyId")
        if cid is None:
            continue
        cid = str(cid)
        fetched_ids.add(cid)

        existing = db["complaints"].get(cid, {})
        record = dict(c)
        record["id"] = cid
        record["tag_origem"] = existing.get("tag_origem")
        record["categoria"] = existing.get("categoria")
        record["first_seen"] = existing.get("first_seen", now)
        record["last_seen"] = now
        record["deactivated_at"] = None
        db["complaints"][cid] = record

    if complete:
        desativadas = 0
        for cid, rec in db["complaints"].items():
            if cid not in fetched_ids and rec.get("deactivated_at") is None:
                rec["deactivated_at"] = now
                desativadas += 1
        if desativadas:
            print(f"  {desativadas} reclamação(ões) sumiram da listagem e foram desativadas.")
    else:
        ausentes = sum(
            1 for cid, rec in db["complaints"].items()
            if cid not in fetched_ids and rec.get("deactivated_at") is None
        )
        if ausentes:
            print(
                f"  coleta parcial: {ausentes} reclamação(ões) do banco não vieram nesta "
                f"resposta, mas foram MANTIDAS ativas (não dá pra saber se foram removidas "
                f"do RA ou se a paginação falhou)."
            )

    db["last_sync"] = {"at": now, "complete": complete, "fetched": len(fetched_ids)}
    return db


# ---------------------------------------------------------------------------
# MÉTRICAS
# ---------------------------------------------------------------------------
def is_answered(c: dict) -> bool:
    if c.get("status") == "ANSWERED":
        return True
    for i in c.get("interactions", []) or []:
        if i.get("type") in ANSWERED_INTERACTION_TYPES:
            return True
    return False


def month_key(created: str | None) -> str | None:
    if not created or len(created) < 7:
        return None
    return created[:7]  # "YYYY-MM"


def trailing_months(ref_year: int, ref_month: int, n: int) -> list[tuple[int, int]]:
    """Últimos n meses terminando em (ref_year, ref_month), incluído. Do mais antigo pro mais novo."""
    months = []
    y, m = ref_year, ref_month
    for _ in range(n):
        months.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    months.reverse()
    return months


def mk(y: int, m: int) -> str:
    return f"{y:04d}-{m:02d}"


def month_label(mk_str: str) -> str:
    meses = ["", "Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    y, m = mk_str.split("-")
    return f"{meses[int(m)]}/{y[2:]}"


def stats_for_group(complaints: list[dict]) -> dict:
    """Calcula IR/MA/IS/IN e AR para um conjunto de reclamações ATIVAS."""
    total = len(complaints)
    answered = sum(1 for c in complaints if is_answered(c))
    evaluated = [c for c in complaints if c.get("evaluated") and c.get("score") is not None]
    solved = sum(1 for c in evaluated if c.get("solved"))
    deal_yes = sum(1 for c in evaluated if c.get("dealAgain") is True)

    ir = round(100 * answered / total, 2) if total else None
    ma = round(statistics.mean([c["score"] for c in evaluated]), 2) if evaluated else None
    is_pct = round(100 * solved / len(evaluated), 2) if evaluated else None
    in_pct = round(100 * deal_yes / len(evaluated), 2) if evaluated else None

    ar = None
    if None not in (ir, ma, is_pct, in_pct):
        ar = round(((ir * 2) + (ma * 10 * 3) + (is_pct * 3) + (in_pct * 2)) / 100, 2)

    label, label_color = reputation_label(ar, ma, ir, len(evaluated))
    ra1000 = ra1000_status(len(evaluated), ir, is_pct, ma, in_pct)

    return {
        "total": total,
        "answered": answered,
        "ir": ir,
        "n_evaluated": len(evaluated),
        "ma": ma,
        "solved": solved,
        "is_pct": is_pct,
        "deal_yes": deal_yes,
        "in_pct": in_pct,
        "ar": ar,
        "label": label,
        "label_color": label_color,
        "ra1000": ra1000,
    }


# Faixas oficiais de reputação (blog.reclameaqui.com.br/conheca-os-indicadores-de-reputacao-do-reclame-aqui)
REPUTATION_BANDS = [
    (8, 10, "Ótima", "#3DD68C"),
    (7, 8, "Boa", "#B345F7"),
    (6, 7, "Regular", "#FFC93D"),
    (5, 6, "Ruim", "#FF8F5C"),
]


def reputation_label(ar, ma, ir, n_evaluated):
    """Classificação oficial: 'Sem Reputação Definida' com <10 avaliações nos
    últimos 6 meses; 'Não Recomendada' se nota média <5 OU resposta <50%,
    independente da nota final; senão, faixa pela nota AR (0 a 10)."""
    if n_evaluated < 10:
        return "Sem Reputação Definida", "#8B7FA3"
    if ar is None:
        return "Sem Reputação Definida", "#8B7FA3"
    if (ma is not None and ma < 5) or (ir is not None and ir < 50):
        return "Não Recomendada", "#FF6B6D"
    for lo, hi, name, color in REPUTATION_BANDS:
        if lo <= ar < hi or (hi == 10 and ar == 10):
            return name, color
    return "Não Recomendada", "#FF6B6D"


def ra1000_status(n_evaluated, ir, is_pct, ma, in_pct):
    """Os 5 critérios do selo RA1000 (blog.reclameaqui.com.br/selo-ra1000...),
    avaliados na mesma janela do AR. Não confirma o selo em si (isso depende
    de auditoria do RA, tempo de cadastro >6 meses e histórico de moderação),
    só indica se os indicadores numéricos estariam dentro do critério."""
    criteria = [
        ("Avaliações ≥ 50", n_evaluated, n_evaluated >= 50, f"{n_evaluated}"),
        ("Índice de Resposta ≥ 90%", ir, ir is not None and ir >= 90, f"{ir}%" if ir is not None else "—"),
        ("Índice de Solução ≥ 90%", is_pct, is_pct is not None and is_pct >= 90, f"{is_pct}%" if is_pct is not None else "—"),
        ("Nota do Consumidor ≥ 7", ma, ma is not None and ma >= 7, f"{ma}" if ma is not None else "—"),
        ("Voltaria a Negócio ≥ 70%", in_pct, in_pct is not None and in_pct >= 70, f"{in_pct}%" if in_pct is not None else "—"),
    ]
    met = sum(1 for _, _, ok, _ in criteria if ok)
    return {
        "criteria": [{"label": c[0], "ok": c[2], "value": c[3]} for c in criteria],
        "met": met,
        "total": len(criteria),
        "eligible_numerically": met == len(criteria),
    }


def compute_monthly_evolution(db: dict, now: datetime, n_months: int = 7) -> list[dict]:
    complaints = list(db["complaints"].values())
    months = trailing_months(now.year, now.month, n_months)
    overrides = db.get("monthly_overrides", {})

    rows = []
    for (y, m) in months:
        key = mk(y, m)
        in_month = [c for c in complaints if month_key(c.get("created")) == key]
        active = [c for c in in_month if not c.get("deactivated_at")]
        removed = [c for c in in_month if c.get("deactivated_at")]
        stats = stats_for_group(active)
        rows.append({
            "month_key": key,
            "is_current": (y, m) == (now.year, now.month),
            "total_raw": len(in_month),
            "removed": len(removed),
            "active": len(active),
            **stats,
            "month_label": month_label(key),
            "eval_rate": round(100 * stats["n_evaluated"] / len(active), 1) if active else None,
            "sla_manual": overrides.get(key, {}).get("sla"),
            "origin_counts": dict(Counter(c.get("tag_origem") or "Sem tag" for c in active)),
            "categoria_counts": dict(Counter(c.get("categoria") or "Sem motivo" for c in active)),
        })
    return rows


def shift_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Desloca (ano, mês) por delta meses (delta pode ser negativo)."""
    idx = (year * 12 + (month - 1)) - delta
    return idx // 12, idx % 12 + 1


def compute_ar_window(db: dict, now: datetime, n_months: int = AR_WINDOW_MONTHS) -> dict:
    complaints = list(db["complaints"].values())
    # janela "fechada" pela RA: termina AR_WINDOW_LAG_MONTHS meses atrás de hoje,
    # não no mês corrente (veja o comentário em AR_WINDOW_LAG_MONTHS acima).
    end_year, end_month = shift_months(now.year, now.month, AR_WINDOW_LAG_MONTHS)
    months = trailing_months(end_year, end_month, n_months)
    keys = {mk(y, m) for (y, m) in months}
    active = [
        c for c in complaints
        if month_key(c.get("created")) in keys and not c.get("deactivated_at")
    ]
    result = stats_for_group(active)
    result["window_start"] = month_label(mk(*months[0]))
    result["window_end"] = month_label(mk(*months[-1]))
    return result


def compute_ar_projection(db: dict, now: datetime, n_months: int = AR_WINDOW_MONTHS) -> dict:
    """Janela "de virada de mês": os mesmos N meses do AR oficial, mas terminando
    no mês corrente (em andamento) em vez de defasada em AR_WINDOW_LAG_MONTHS meses
    — ou seja, os últimos (N-1) meses fechados + o mês atual. É a janela que o RA
    vai passar a usar assim que o mês virar, então mostra pra onde a nota está
    caminhando antes disso acontecer."""
    complaints = list(db["complaints"].values())
    months = trailing_months(now.year, now.month, n_months)
    keys = {mk(y, m) for (y, m) in months}
    active = [
        c for c in complaints
        if month_key(c.get("created")) in keys and not c.get("deactivated_at")
    ]
    result = stats_for_group(active)
    result["window_start"] = month_label(mk(*months[0]))
    result["window_end"] = month_label(mk(*months[-1]))

    days_in_month = calendar.monthrange(now.year, now.month)[1]
    result["day_of_month"] = now.day
    result["days_in_month"] = days_in_month
    result["month_progress_pct"] = round(100 * now.day / days_in_month, 1)
    return result


def compute_origin_breakdown(db: dict) -> dict:
    active = [c for c in db["complaints"].values() if not c.get("deactivated_at")]
    counter = Counter(c.get("tag_origem") or "Sem tag" for c in active)
    return dict(counter)


def compute_channel_breakdown(db: dict) -> dict:
    active = [c for c in db["complaints"].values() if not c.get("deactivated_at")]
    counter = Counter(c.get("complainOrigin") or "DESCONHECIDA" for c in active)
    return dict(counter)


def compute_status_breakdown(db: dict) -> dict:
    active = [c for c in db["complaints"].values() if not c.get("deactivated_at")]
    counter = Counter(c.get("status") or "DESCONHECIDO" for c in active)
    return dict(counter)


def compute_daily_volume(db: dict) -> list[dict]:
    active = [c for c in db["complaints"].values() if not c.get("deactivated_at")]
    by_day = defaultdict(lambda: {"count": 0, "scores": []})
    for c in active:
        created = c.get("created")
        if not created:
            continue
        day = created[:10]
        by_day[day]["count"] += 1
        if c.get("score") is not None:
            by_day[day]["scores"].append(c["score"])

    daily = []
    for day in sorted(by_day.keys()):
        info = by_day[day]
        daily.append({
            "date": day,
            "count": info["count"],
            "avg_score": round(statistics.mean(info["scores"]), 2) if info["scores"] else None,
        })
    return daily[-14:]  # últimos 14 dias com movimento


def build_dashboard_data(db: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    active = [c for c in db["complaints"].values() if not c.get("deactivated_at")]
    deactivated_total = sum(1 for c in db["complaints"].values() if c.get("deactivated_at"))

    # "now" é usado pra calcular as janelas (AR, projeção, evolução mensal) —
    # tem que ser o instante atual mesmo, não a hora do último fetch. Já o
    # timestamp MOSTRADO como "atualizado em" precisa ser a hora real da
    # última busca (db["last_sync"]), senão uma página só visualizada (sem
    # apertar "Atualizar agora") sempre mostraria "agora", escondendo dado
    # velho — o que o aviso de "dado desatualizado" existe justamente pra pegar.
    last_sync_at = (db.get("last_sync") or {}).get("at")
    try:
        updated_at = datetime.fromisoformat(last_sync_at) if last_sync_at else now
    except ValueError:
        updated_at = now

    return {
        "updated_at": updated_at,
        "total_active": len(active),
        "total_deactivated": deactivated_total,
        "ar": compute_ar_window(db, now),
        "projection": compute_ar_projection(db, now),
        "monthly": compute_monthly_evolution(db, now),
        "origin": compute_origin_breakdown(db),
        "channel": compute_channel_breakdown(db),
        "status": compute_status_breakdown(db),
        "daily": compute_daily_volume(db),
        "tags": db.get("tags", list(DEFAULT_TAGS)),
        "categorias": db.get("categorias", list(DEFAULT_CATEGORIES)),
        "recent": sorted(active, key=lambda x: x.get("created", ""), reverse=True),
    }


# ---------------------------------------------------------------------------
# RENDER HTML
# ---------------------------------------------------------------------------
def score_color(score):
    if score is None:
        return "#8B7FA3"
    t = max(0, min(10, score)) / 10
    r1, g1, b1 = (0xFF, 0x6B, 0x6D)  # coral (ruim)
    r2, g2, b2 = (0x3D, 0xD6, 0x8C)  # mint (bom)
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    b = round(b1 + (b2 - b1) * t)
    return f"#{r:02X}{g:02X}{b:02X}"


def pct_color(pct):
    if pct is None:
        return "#8B7FA3"
    return score_color(pct / 10)


STATUS_LABELS = {
    "PENDING": "Pendente",
    "ANSWERED": "Respondida",
    "FINISHED": "Finalizada",
    "FINISHED_NOT_EVALUATED": "Finalizada (sem avaliação)",
    "IN_TREATMENT": "Em tratamento",
}
STATUS_COLORS = {
    "PENDING": "#FFC93D",
    "ANSWERED": "#B345F7",
    "FINISHED": "#3DD68C",
    "FINISHED_NOT_EVALUATED": "#C77DF0",
    "IN_TREATMENT": "#FF8F5C",
}


def fmt(v, suffix="", none="—"):
    return f"{v}{suffix}" if v is not None else none


def info_icon(text: str) -> str:
    return f'<span class="info" tabindex="0">?<span class="bubble">{esc(text)}</span></span>'


def esc(v, none: str = "") -> str:
    """BUGFIX #1 — escapa qualquer texto que venha da API ou do usuário antes de
    entrar no HTML. Título de reclamação é texto livre digitado por consumidor:
    um '<', um '&' ou uma aspa já quebravam o layout, e um título malicioso
    conseguia injetar markup/script no dashboard. Escapa também aspas, porque
    vários desses valores vão parar dentro de atributos (title="...").
    """
    if v is None:
        return none
    return html_lib.escape(str(v), quote=True)


FONT_LINKS = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Manrope:wght@700;800&display=swap" rel="stylesheet">"""

CSS = """
  :root {
    color-scheme: dark;
    /* Next Fit brand palette (extraído do site oficial nextfit.com.br), em versão escura */
    --ink: #F4EEFB; --ink-soft: #C6B8DA; --ink-dim: #8B7FA3;
    --canvas: #120B18; --card: #1C1225; --border: rgba(255,255,255,.08); --border-strong: rgba(255,255,255,.16);

    --violet-deep: #34004E; --violet: #C77DF0; --violet-bright: #B345F7; --violet-pale: rgba(179,69,247,.14);
    --mint: #3DD68C; --amber: #FFC93D; --orange: #FF8F5C; --coral: #FF6B6D;

    --shadow: 0 10px 30px rgba(0,0,0,.4);
    --glow: 0 0 0 1px rgba(179,69,247,.16), 0 10px 34px rgba(179,69,247,.16);
    --hover-surface: rgba(255,255,255,.06); --zebra: rgba(255,255,255,.02);
    --dock-bg: rgba(28,18,37,.78); --glow-op-1: .16; --glow-op-2: .08;
  }
  :root[data-theme="light"] {
    color-scheme: light;
    /* Paleta clara — mesma marca (violeta/menta), sem depender de "dark" */
    --ink: #201A29; --ink-soft: #4B3F5E; --ink-dim: #7C7091;
    --canvas: #F4F1FA; --card: #FFFFFF; --border: rgba(32,20,43,.09); --border-strong: rgba(32,20,43,.18);

    --shadow: 0 10px 26px rgba(32,20,43,.10);
    --glow: 0 0 0 1px rgba(179,69,247,.12), 0 10px 30px rgba(179,69,247,.10);
    --hover-surface: rgba(32,20,43,.05); --zebra: rgba(32,20,43,.025);
    --dock-bg: rgba(255,255,255,.82); --glow-op-1: .10; --glow-op-2: .06;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--canvas); color: var(--ink);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .mono { font-variant-numeric: tabular-nums; }
  .num { font-family: 'Manrope', 'Inter', sans-serif; font-weight: 800; }

  .bg-glow { position: fixed; inset: 0; z-index: -1; overflow: hidden; pointer-events: none; }
  .bg-glow::before, .bg-glow::after { content: ''; position: absolute; width: 640px; height: 640px; border-radius: 50%; filter: blur(130px); }
  .bg-glow::before { background: var(--violet-bright); opacity: var(--glow-op-1); top: -240px; left: -180px; }
  .bg-glow::after { background: var(--mint); opacity: var(--glow-op-2); bottom: -260px; right: -200px; }

  .wordmark { display: flex; align-items: center; gap: 8px; font-family: 'Manrope', sans-serif; font-weight: 800; font-size: 16px; color: var(--ink); }
  .wordmark .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--violet-bright); flex-shrink: 0; box-shadow: 0 0 10px var(--violet-bright); }

  .dock-wrap { position: sticky; top: 14px; z-index: 20; display: flex; justify-content: center; padding: 0 16px; margin-bottom: 22px; }
  .dock {
    display: flex; align-items: center; gap: 20px; background: var(--dock-bg); backdrop-filter: blur(16px);
    border: 1px solid var(--border-strong); border-radius: 999px; padding: 8px 8px 8px 20px;
    box-shadow: var(--glow); max-width: 100%; overflow-x: auto;
  }
  .dock-tabs { display: flex; align-items: center; gap: 2px; }
  .dock-tabs .tab-btn {
    appearance: none; background: none; border: none; cursor: pointer; font: inherit;
    height: 34px; padding: 0 15px; border-radius: 999px; font-size: 13px; font-weight: 600;
    line-height: 1; color: var(--ink-dim); white-space: nowrap;
    display: inline-flex; align-items: center; justify-content: center; box-sizing: border-box;
  }
  .dock-tabs .tab-btn:hover { color: var(--ink); }
  .dock-tabs .tab-btn.active { background: var(--violet-bright); color: #1B0929; }
  .tab-panel[hidden] { display: none; }
  .dock-actions { display: flex; align-items: center; padding-right: 4px; position: relative; }

  .kebab-btn {
    appearance: none; background: none; border: 1px solid var(--border-strong); cursor: pointer;
    width: 34px; height: 34px; border-radius: 50%; color: var(--ink-soft); font-size: 17px; font-weight: 700;
    display: inline-flex; align-items: center; justify-content: center; box-sizing: border-box; flex-shrink: 0;
  }
  .kebab-btn:hover { color: var(--ink); background: var(--hover-surface); }

  .kebab-menu {
    position: fixed; z-index: 60; width: 220px;
    background: var(--card); border: 1px solid var(--border-strong); border-radius: 16px;
    box-shadow: var(--shadow); padding: 10px; display: flex; flex-direction: column; gap: 4px;
  }
  .kebab-menu[hidden] { display: none; }
  .kebab-brand { padding: 4px 8px 8px; border-bottom: 1px solid var(--border); margin-bottom: 4px; }
  .kebab-item {
    appearance: none; background: none; border: none; cursor: pointer; font: inherit; text-align: left;
    color: var(--ink-soft); font-size: 13px; font-weight: 600; padding: 9px 8px; border-radius: 8px; width: 100%;
  }
  .kebab-item:hover { color: var(--ink); background: var(--hover-surface); }
  .kebab-menu .btn { width: 100%; margin: 2px 0; }
  .kebab-links { display: flex; flex-direction: column; }
  .kebab-links a, .kebab-links .link-btn {
    display: block; color: var(--ink-soft); font-size: 13px; font-weight: 600; padding: 9px 8px; border-radius: 8px; text-decoration: none;
  }
  .kebab-links a:hover, .kebab-links .link-btn:hover { color: var(--ink); background: var(--hover-surface); }
  .link-btn { appearance: none; background: none; border: none; font: inherit; cursor: pointer; text-align: left; }

  .wrap { max-width: 1220px; margin: 0 auto; padding: 0 24px 64px; position: relative; }
  .status-line { font-size: 12px; color: var(--ink-dim); margin-bottom: 18px; }
  .status-line b { color: var(--ink-soft); font-weight: 700; }

  .btn {
    background: var(--violet-bright); color: #1B0929; border: none; border-radius: 999px;
    padding: 9px 18px; font-weight: 700; font-size: 13px; cursor: pointer; font-family: inherit;
  }
  .btn-refresh::before { content: '⟳ '; }
  .btn:hover { filter: brightness(1.08); }
  .btn:disabled { opacity: .55; cursor: default; }

  .hero-strip { display: grid; grid-template-columns: 1.5fr 1fr 1fr 1fr; gap: 14px; margin: 0 0 22px; align-items: stretch; }
  @media (max-width: 900px) { .hero-strip { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 540px) { .hero-strip { grid-template-columns: 1fr; } }

  .hero-ar {
    background: linear-gradient(150deg, var(--violet-deep) 0%, #4C0B70 60%, var(--violet-bright) 160%);
    color: #fff; border-radius: 20px; padding: 20px 22px; display: flex; flex-direction: column;
    justify-content: center; gap: 6px; box-shadow: var(--glow);
  }
  .hero-ar .label { font-size: 13px; color: #E3C6F5; font-weight: 600; }
  .hero-ar .value { font-size: 46px; line-height: 1; }
  .hero-ar .window { font-size: 12px; color: #E3C6F5; }
  .hero-ar .window .pill { background: rgba(255,255,255,.18) !important; }

  .kpi { background: var(--card); border: 1px solid var(--border); border-radius: 18px; padding: 16px 18px; box-shadow: var(--shadow); display: flex; flex-direction: column; justify-content: center; }
  .kpi .label { font-size: 12.5px; color: var(--ink-soft); margin-bottom: 8px; font-weight: 600; }
  .kpi .value { font-size: 27px; line-height: 1; color: var(--ink); }
  .kpi .sub { font-size: 11.5px; color: var(--ink-dim); margin-top: 8px; }

  dialog.modal { border: none; border-radius: 20px; padding: 0; width: min(460px, 92vw); box-shadow: var(--glow); background: var(--card); color: var(--ink); }
  dialog.modal::backdrop { background: rgba(6,3,10,.6); backdrop-filter: blur(6px); }
  dialog.modal[open] { animation: modal-in .15s ease-out; }
  @keyframes modal-in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .modal-form { padding: 26px 28px; }
  .modal-form textarea {
    width: 100%; min-height: 90px; resize: vertical; border: 1px solid var(--border-strong); border-radius: 10px;
    background: var(--canvas); color: var(--ink);
    padding: 9px 11px; font: inherit; font-size: 13px; margin: 10px 0 16px; box-sizing: border-box;
  }
  .modal-form textarea:focus { outline: 2px solid var(--violet-bright); outline-offset: 1px; }
  .modal-actions { display: flex; justify-content: flex-end; gap: 10px; }
  .btn-ghost { background: none; color: var(--ink-soft); box-shadow: none; }
  .btn-ghost:hover { background: var(--hover-surface); filter: none; }

  .panel { background: var(--card); border: 1px solid var(--border); border-radius: 18px; padding: 18px 20px 20px; margin-bottom: 18px; box-shadow: var(--shadow); }
  .panel-title { font-size: 14.5px; font-weight: 700; margin: 0 0 3px; color: var(--ink); }
  .panel-note { font-size: 12.5px; color: var(--ink-dim); margin: 0 0 16px; }
  .cur-badge { font-size: 11px; color: var(--amber); background: rgba(255,201,61,.16); border: 1px solid rgba(255,201,61,.35); border-radius: 4px; padding: 1px 7px; margin-left: 6px; }

  .proj-grid { display: grid; grid-template-columns: 1fr auto 1fr auto; gap: 14px; align-items: center; margin-bottom: 18px; }
  @media (max-width: 820px) { .proj-grid { grid-template-columns: 1fr 1fr; } .proj-arrow { display: none; } }
  .proj-card { background: var(--canvas); border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px; }
  .proj-card--future { background: var(--violet-pale); border-color: var(--border-strong); }
  .proj-card .label { font-size: 12px; color: var(--ink-soft); margin-bottom: 8px; font-weight: 600; }
  .proj-card .value { font-size: 26px; line-height: 1; color: var(--ink); }
  .proj-card .sub { font-size: 12px; color: var(--ink-soft); margin-top: 8px; }
  .proj-arrow { font-size: 20px; color: var(--ink-dim); }
  .proj-delta { text-align: center; }
  .proj-delta .label { font-size: 12px; color: var(--ink-soft); margin-bottom: 8px; font-weight: 600; }
  .proj-progress-top { display: flex; justify-content: space-between; font-size: 12px; color: var(--ink-soft); margin-bottom: 6px; }

  .timeline { display: flex; align-items: center; gap: 4px; margin-bottom: 18px; overflow-x: auto; padding-bottom: 4px; }
  .chip { flex: 1; min-width: 62px; display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 10px 4px;
    background: var(--canvas); border: 1px solid var(--border); border-radius: 14px; position: relative; }
  .chip--fade { opacity: .55; }
  .chip-dot { width: 12px; height: 12px; border-radius: 50%; }
  .chip-label { font-size: 11px; color: var(--ink-soft); font-weight: 600; }
  .chip-tag { font-size: 9.5px; font-weight: 700; padding: 1px 6px; border-radius: 20px; }
  .chip-tag--out { background: rgba(255,107,109,.16); color: var(--coral); }
  .chip-tag--in { background: var(--violet-pale); color: var(--violet-bright); }

  .turnover-grid { display: grid; grid-template-columns: 1fr auto 1fr; gap: 14px; align-items: center; margin-bottom: 8px; }
  @media (max-width: 820px) { .turnover-grid { grid-template-columns: 1fr 1fr; } .turnover-grid .proj-arrow { display: none; } }

  .info { position: relative; display: inline-flex; align-items: center; justify-content: center;
    width: 14px; height: 14px; border-radius: 50%; background: var(--violet); color: #1B0929;
    font-size: 10px; font-weight: 700; cursor: default; margin-left: 5px; vertical-align: middle; }
  .info .bubble {
    display: none; position: absolute; bottom: 130%; left: 50%; transform: translateX(-50%);
    background: #0C0710; border: 1px solid var(--border-strong); color: var(--ink); font-size: 11.5px; font-weight: 400; line-height: 1.4;
    padding: 8px 10px; border-radius: 8px; width: 200px; text-align: left; z-index: 5; box-shadow: var(--shadow);
  }
  .info:hover .bubble, .info:focus .bubble { display: block; }

  .toast-wrap { position: fixed; bottom: 20px; right: 20px; display: flex; flex-direction: column; gap: 8px; z-index: 50; }
  .toast {
    background: var(--card); color: var(--ink); border: 1px solid var(--border-strong); font-size: 13px; padding: 11px 16px; border-radius: 12px;
    box-shadow: var(--shadow); max-width: 320px; animation: toast-in .18s ease-out;
  }
  .toast--error { background: var(--coral); color: #340708; border-color: transparent; }
  .toast--ok { background: var(--mint); color: #05230F; border-color: transparent; }
  @keyframes toast-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }

  .bars { display: flex; align-items: flex-end; gap: 6px; height: 140px; padding-top: 10px; border-bottom: 1px solid var(--border);
    background-image: repeating-linear-gradient(to top, var(--border) 0, var(--border) 1px, transparent 1px, transparent 25%); }
  .bar { flex: 1; height: 100%; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; position: relative; }
  .bar-fill { width: 100%; max-width: 22px; height: var(--h); background: var(--c); border-radius: 6px 6px 0 0; opacity: 0.92; transition: opacity .15s; }
  .bar:hover .bar-fill { opacity: 1; outline: 1px solid var(--ink); outline-offset: -1px; }
  .bar-label { font-size: 9px; color: var(--ink-dim); margin-top: 6px; white-space: nowrap; }

  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 820px) { .two-col { grid-template-columns: 1fr; } }
  .status-row { margin-bottom: 14px; }
  .status-row:last-child { margin-bottom: 0; }
  .status-row-top { display: flex; align-items: center; gap: 8px; font-size: 13px; margin-bottom: 6px; }
  .status-row-top .dot { width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }
  .status-count { margin-left: auto; color: var(--ink-soft); font-weight: 600; }
  .status-track { height: 8px; background: var(--canvas); border: 1px solid var(--border); border-radius: 4px; overflow: hidden; }
  .status-fill { height: 100%; border-radius: 4px; }
  .origin-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; font-size: 13px; }
  .origin-label { width: 110px; color: var(--ink-soft); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .origin-track { flex: 1; height: 8px; background: var(--canvas); border: 1px solid var(--border); border-radius: 4px; overflow: hidden; }
  .origin-fill { height: 100%; background: var(--violet-bright); border-radius: 4px; }
  .origin-count { color: var(--ink-soft); font-weight: 600; width: 28px; text-align: right; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-size: 12px; color: var(--ink); font-weight: 700; padding: 8px 10px; border-bottom: 2px solid var(--border-strong); }
  td { padding: 9px 10px; border-bottom: 1px solid var(--border); vertical-align: top; color: var(--ink-soft); }
  tbody tr:nth-child(even) { background: var(--zebra); }
  tbody tr:hover { background: var(--violet-pale); }
  tr:last-child td { border-bottom: none; }
  .title-cell { max-width: 340px; color: var(--ink); }
  .score-cell { text-align: right; }
  .pill { display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 11px; font-weight: 700; }
  .tag-select { background: var(--canvas); color: var(--ink); border: 1px solid var(--border-strong); border-radius: 6px; padding: 4px 6px; font-size: 12px; font-family: inherit; }
  .empty { color: var(--ink-dim); font-size: 13px; }
  footer { margin-top: 8px; font-size: 11px; color: var(--ink-dim); line-height: 1.6; background: var(--card); border: 1px solid var(--border); border-radius: 18px; padding: 14px 18px; box-shadow: var(--shadow); }

  .filter-bar { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  .filter-bar select, .filter-bar input[type="search"] {
    background: var(--canvas); color: var(--ink); border: 1px solid var(--border-strong); border-radius: 8px;
    padding: 7px 10px; font-size: 12.5px; font-family: inherit;
  }
  .filter-bar input[type="search"] { flex: 1; min-width: 160px; }
  .filter-bar select { min-width: 130px; }
  .filter-count { font-size: 12px; color: var(--ink-dim); margin: -8px 0 14px; }

  .pagination { display: flex; align-items: center; justify-content: center; gap: 6px; margin-top: 16px; flex-wrap: wrap; }
  .pagination button {
    appearance: none; background: var(--canvas); color: var(--ink-soft); border: 1px solid var(--border-strong);
    border-radius: 8px; padding: 6px 12px; font-size: 12.5px; font-weight: 600; cursor: pointer; font-family: inherit; min-width: 34px;
  }
  .pagination button:hover { color: var(--ink); background: var(--hover-surface); }
  .pagination button.active { background: var(--violet-bright); color: #1B0929; border-color: transparent; }
  .pagination button:disabled { opacity: .4; cursor: default; }
  .pagination .pg-ellipsis { color: var(--ink-dim); font-size: 12px; padding: 0 2px; }

  .manage-tags { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 4px; }
  @media (max-width: 640px) { .manage-tags { grid-template-columns: 1fr; } }
  .manage-tags-col p.small-label { font-size: 11.5px; font-weight: 700; color: var(--ink-soft); margin: 0 0 8px; }
  .manage-tags-form { display: flex; gap: 8px; }
  .manage-tags-form input[type="text"] {
    flex: 1; background: var(--canvas); color: var(--ink); border: 1px solid var(--border-strong); border-radius: 8px;
    padding: 7px 10px; font-size: 12.5px; font-family: inherit; min-width: 0;
  }
  .manage-tags-form button { padding: 7px 14px; font-size: 12.5px; }

  .ra1000-hero { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
  .ra1000-hero-badge {
    width: 64px; height: 64px; border-radius: 50%; flex-shrink: 0; font-size: 28px;
    display: flex; align-items: center; justify-content: center;
    background: var(--violet-pale); filter: grayscale(1) opacity(.7);
  }
  .ra1000-hero-badge--ok { background: rgba(61,214,140,.16); filter: none; }
  .ra1000-hero-value { font-family: 'Manrope', sans-serif; font-weight: 800; font-size: 36px; line-height: 1; color: var(--ink); }
  .ra1000-hero-label { font-size: 12.5px; color: var(--ink-soft); margin-top: 6px; }
  .ra1000-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 14px; }
  .ra1000-card {
    background: var(--canvas); border: 1px solid var(--border); border-radius: 14px; padding: 16px 12px;
    display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center;
  }
  .ra1000-card--ok { border-color: rgba(61,214,140,.35); background: rgba(61,214,140,.06); }
  .ra1000-icon {
    width: 28px; height: 28px; border-radius: 50%; font-size: 14px; font-weight: 800;
    display: flex; align-items: center; justify-content: center; background: var(--border-strong); color: var(--ink-dim);
  }
  .ra1000-icon--ok { background: var(--mint); color: #05230F; }
  .ra1000-card-value { font-family: 'Manrope', sans-serif; font-weight: 800; font-size: 19px; color: var(--ink); }
  .ra1000-card-label { font-size: 11.5px; color: var(--ink-soft); line-height: 1.35; }

  .cmp-select-row { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; flex-wrap: wrap; }
  .cmp-select-row select {
    background: var(--canvas); color: var(--ink); border: 1px solid var(--border-strong); border-radius: 8px;
    padding: 7px 10px; font-size: 12.5px; font-family: inherit; min-width: 140px;
  }
  .cmp-vs { font-size: 12px; color: var(--ink-dim); font-weight: 700; }
  .cmp-section-title { font-size: 12.5px; font-weight: 700; color: var(--ink-soft); margin: 18px 0 8px; }
  .cmp-section-title:first-child { margin-top: 0; }
  .delta-up { color: var(--mint); font-weight: 600; }
  .delta-down { color: var(--coral); font-weight: 600; }
  .delta-flat { color: var(--ink-dim); }
"""

TABS_JS = """
<script>
function selectTab(name){
  document.querySelectorAll('.tab-panel').forEach(function(p){ p.hidden = (p.dataset.tab !== name); });
  document.querySelectorAll('.tab-btn').forEach(function(b){ b.classList.toggle('active', b.dataset.tab === name); });
  try { localStorage.setItem('nextRaTab', name); } catch (e) {}
}
document.querySelectorAll('.tab-btn').forEach(function(btn){
  btn.addEventListener('click', function(){ selectTab(btn.dataset.tab); });
});
(function(){
  var saved = null;
  try { saved = localStorage.getItem('nextRaTab'); } catch (e) {}
  if (saved && document.querySelector('.tab-panel[data-tab="' + saved + '"]')) selectTab(saved);
})();

function applyThemeIcon(){
  var label = document.getElementById('themeToggleLabel');
  if (!label) return;
  var isLight = document.documentElement.dataset.theme === 'light';
  label.textContent = isLight ? 'Mudar para tema escuro' : 'Mudar para tema claro';
}
function toggleTheme(){
  var isLight = document.documentElement.dataset.theme === 'light';
  var next = isLight ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('nextRaTheme', next); } catch (e) {}
  applyThemeIcon();
}
applyThemeIcon();

function positionKebabMenu(){
  var menu = document.getElementById('kebabMenu');
  var btn = document.getElementById('kebabToggle');
  if (!menu || !btn) return;
  var r = btn.getBoundingClientRect();
  var menuWidth = 220;
  var right = Math.max(8, window.innerWidth - r.right);
  if (right + menuWidth > window.innerWidth - 8) right = window.innerWidth - menuWidth - 8;
  menu.style.top = (r.bottom + 10) + 'px';
  menu.style.right = right + 'px';
}
function toggleKebabMenu(force){
  var menu = document.getElementById('kebabMenu');
  var btn = document.getElementById('kebabToggle');
  if (!menu || !btn) return;
  var show = typeof force === 'boolean' ? force : menu.hidden;
  if (show) positionKebabMenu();
  menu.hidden = !show;
  btn.setAttribute('aria-expanded', show ? 'true' : 'false');
}
document.addEventListener('click', function(e){
  var menu = document.getElementById('kebabMenu');
  var btn = document.getElementById('kebabToggle');
  if (!menu || menu.hidden) return;
  if (menu.contains(e.target) || btn.contains(e.target)) return;
  toggleKebabMenu(false);
});
document.addEventListener('keydown', function(e){
  if (e.key === 'Escape') toggleKebabMenu(false);
});
window.addEventListener('scroll', function(){ toggleKebabMenu(false); }, true);
window.addEventListener('resize', function(){ toggleKebabMenu(false); });

var RC_PAGE_SIZE = 15;
var rcPage = 1;
function getComplaintRows(){
  var tbody = document.getElementById('rcTableBody');
  return tbody ? Array.prototype.slice.call(tbody.querySelectorAll('tr[data-title]')) : [];
}
function applyComplaintFilters(){
  var titleEl = document.getElementById('rcFilterTitle');
  var statusEl = document.getElementById('rcFilterStatus');
  var origemEl = document.getElementById('rcFilterOrigem');
  var categoriaEl = document.getElementById('rcFilterCategoria');
  if (!titleEl) return;
  var titleQ = (titleEl.value || '').toLowerCase().trim();
  var statusQ = statusEl.value, origemQ = origemEl.value, categoriaQ = categoriaEl.value;
  var rows = getComplaintRows();
  var visible = [];
  rows.forEach(function(tr){
    var ok = true;
    if (titleQ && tr.dataset.title.indexOf(titleQ) === -1) ok = false;
    if (ok && statusQ && tr.dataset.status !== statusQ) ok = false;
    if (ok && origemQ && tr.dataset.origem !== origemQ) ok = false;
    if (ok && categoriaQ && tr.dataset.categoria !== categoriaQ) ok = false;
    if (ok) visible.push(tr);
  });
  rcPage = 1;
  renderComplaintPage(rows, visible);
}
function renderComplaintPage(allRows, visibleRows){
  var totalPages = Math.max(1, Math.ceil(visibleRows.length / RC_PAGE_SIZE));
  if (rcPage > totalPages) rcPage = totalPages;
  var start = (rcPage - 1) * RC_PAGE_SIZE, end = start + RC_PAGE_SIZE;
  var shown = visibleRows.slice(start, end);
  allRows.forEach(function(tr){ tr.style.display = shown.indexOf(tr) === -1 ? 'none' : ''; });
  var countEl = document.getElementById('rcFilterCount');
  if (countEl) {
    countEl.textContent = visibleRows.length === 0
      ? 'Nenhuma reclamação encontrada com esses filtros.'
      : visibleRows.length + ' reclamação(ões) · página ' + rcPage + ' de ' + totalPages;
  }
  renderPagination(allRows, visibleRows, totalPages);
}
function renderPagination(allRows, visibleRows, totalPages){
  var wrap = document.getElementById('rcPagination');
  if (!wrap) return;
  wrap.innerHTML = '';
  if (totalPages <= 1) return;
  function addBtn(label, page, opts){
    opts = opts || {};
    var b = document.createElement('button');
    b.type = 'button'; b.textContent = label;
    if (opts.active) b.classList.add('active');
    if (opts.disabled) b.disabled = true;
    b.addEventListener('click', function(){ rcPage = page; renderComplaintPage(allRows, visibleRows); });
    wrap.appendChild(b);
  }
  addBtn('‹', Math.max(1, rcPage - 1), {disabled: rcPage === 1});
  var maxButtons = 7;
  var startP = Math.max(1, rcPage - 3);
  var endP = Math.min(totalPages, startP + maxButtons - 1);
  startP = Math.max(1, endP - maxButtons + 1);
  if (startP > 1) {
    addBtn('1', 1);
    if (startP > 2) { var e = document.createElement('span'); e.className = 'pg-ellipsis'; e.textContent = '…'; wrap.appendChild(e); }
  }
  for (var p = startP; p <= endP; p++) addBtn(String(p), p, {active: p === rcPage});
  if (endP < totalPages) {
    if (endP < totalPages - 1) { var e2 = document.createElement('span'); e2.className = 'pg-ellipsis'; e2.textContent = '…'; wrap.appendChild(e2); }
    addBtn(String(totalPages), totalPages);
  }
  addBtn('›', Math.min(totalPages, rcPage + 1), {disabled: rcPage === totalPages});
}
if (document.getElementById('rcTableBody')) applyComplaintFilters();

function escHtml(s){
  var d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}
function cmpFmtVal(v, decimals, suffix){
  suffix = suffix || '';
  return (v === null || v === undefined) ? '—' : v.toFixed(decimals) + suffix;
}
function cmpDeltaHtml(a, b, opts){
  opts = opts || {};
  var decimals = opts.decimals === undefined ? 2 : opts.decimals;
  var suffix = opts.suffix || '';
  if (a === null || a === undefined || b === null || b === undefined) return '<span class="delta-flat">—</span>';
  var d = Number((b - a).toFixed(decimals));
  if (Math.abs(d) < Math.pow(10, -decimals) / 2) return '<span class="delta-flat">sem variação</span>';
  var up = d > 0;
  var good = opts.higherIsBetter === false ? !up : up;
  var cls = good ? 'delta-up' : 'delta-down';
  var arrow = up ? '▲' : '▼';
  var sign = up ? '+' : '';
  return '<span class="' + cls + '">' + arrow + ' ' + sign + d.toFixed(decimals) + suffix + '</span>';
}
function cmpBreakdownRows(dictA, dictB){
  dictA = dictA || {}; dictB = dictB || {};
  var keys = {};
  Object.keys(dictA).forEach(function(k){ keys[k] = true; });
  Object.keys(dictB).forEach(function(k){ keys[k] = true; });
  var names = Object.keys(keys).sort(function(x, y){
    return ((dictB[y] || 0) + (dictA[y] || 0)) - ((dictB[x] || 0) + (dictA[x] || 0));
  });
  if (!names.length) return '<tr><td colspan="4" class="empty">Sem dados.</td></tr>';
  return names.map(function(name){
    var va = dictA[name] || 0, vb = dictB[name] || 0;
    return '<tr><td>' + escHtml(name) + '</td><td class="mono">' + va + '</td><td class="mono">' + vb + '</td><td>' +
      cmpDeltaHtml(va, vb, {decimals: 0, higherIsBetter: false}) + '</td></tr>';
  }).join('');
}
function renderMonthComparison(){
  var dataEl = document.getElementById('monthlyCompareData');
  var resultEl = document.getElementById('cmpResult');
  var selA = document.getElementById('cmpMonthA');
  var selB = document.getElementById('cmpMonthB');
  if (!dataEl || !resultEl || !selA || !selB) return;
  var months;
  try { months = JSON.parse(dataEl.textContent); } catch (e) { return; }
  var a = months.filter(function(m){ return m.key === selA.value; })[0];
  var b = months.filter(function(m){ return m.key === selB.value; })[0];
  if (!a || !b) { resultEl.innerHTML = '<p class="empty">Selecione dois meses.</p>'; return; }

  var kpiRows = [
    {label: 'Reclamações', a: a.total, b: b.total, decimals: 0, higherIsBetter: false},
    {label: 'AR', a: a.ar, b: b.ar, decimals: 2, higherIsBetter: true},
    {label: 'Nota média (MA)', a: a.ma, b: b.ma, decimals: 2, higherIsBetter: true},
    {label: 'Índice de resposta (IR)', a: a.ir, b: b.ir, decimals: 1, suffix: '%', higherIsBetter: true},
    {label: 'Solução (IS)', a: a.is_pct, b: b.is_pct, decimals: 1, suffix: '%', higherIsBetter: true},
    {label: 'Voltaria (IN)', a: a.in_pct, b: b.in_pct, decimals: 1, suffix: '%', higherIsBetter: true}
  ];
  var kpiHtml = kpiRows.map(function(r){
    return '<tr><td>' + r.label + '</td>' +
      '<td class="mono">' + cmpFmtVal(r.a, r.decimals, r.suffix) + '</td>' +
      '<td class="mono">' + cmpFmtVal(r.b, r.decimals, r.suffix) + '</td>' +
      '<td>' + cmpDeltaHtml(r.a, r.b, {decimals: r.decimals, suffix: r.suffix, higherIsBetter: r.higherIsBetter}) + '</td></tr>';
  }).join('');

  var labelA = escHtml(a.label), labelB = escHtml(b.label);
  resultEl.innerHTML =
    '<p class="cmp-section-title">KPIs</p>' +
    '<table><thead><tr><th></th><th>' + labelA + '</th><th>' + labelB + '</th><th>Variação</th></tr></thead><tbody>' + kpiHtml + '</tbody></table>' +
    '<p class="cmp-section-title">Origem</p>' +
    '<table><thead><tr><th>Origem</th><th>' + labelA + '</th><th>' + labelB + '</th><th>Variação</th></tr></thead><tbody>' + cmpBreakdownRows(a.origin, b.origin) + '</tbody></table>' +
    '<p class="cmp-section-title">Motivo</p>' +
    '<table><thead><tr><th>Motivo</th><th>' + labelA + '</th><th>' + labelB + '</th><th>Variação</th></tr></thead><tbody>' + cmpBreakdownRows(a.categoria, b.categoria) + '</tbody></table>';
}
(function(){
  var selA = document.getElementById('cmpMonthA'), selB = document.getElementById('cmpMonthB');
  if (!selA || !selB) return;
  selA.addEventListener('change', renderMonthComparison);
  selB.addEventListener('change', renderMonthComparison);
  renderMonthComparison();
})();
</script>
"""

TOKEN_MODAL_HTML = """
<dialog id="tokenModal" class="modal">
  <form class="modal-form" id="tokenForm">
    <p class="panel-title" style="margin-bottom:6px">Trocar token</p>
    <p class="panel-note">Cole o novo refresh_token — ele substitui o atual e dispara uma atualização imediata.</p>
    <p style="color:var(--coral);font-size:12px;margin:0 0 10px" id="tokenModalError" hidden></p>
    <textarea id="tokenModalInput" placeholder="Cole o refresh_token aqui"></textarea>
    <div class="modal-actions">
      <button type="button" class="btn btn-ghost" onclick="closeTokenModal()">Cancelar</button>
      <button type="submit" class="btn" id="tokenModalSubmit">Conectar</button>
    </div>
  </form>
</dialog>
<div id="toastWrap" class="toast-wrap" aria-live="polite"></div>
"""

SCRIPT_JS = """
<script>
function queueToast(message, kind){
  try { sessionStorage.setItem('nextRaToast', JSON.stringify({message: message, kind: kind})); } catch (e) {}
}
(function(){
  var raw = null;
  try {
    raw = sessionStorage.getItem('nextRaToast');
    sessionStorage.removeItem('nextRaToast');
  } catch (e) {}
  if (!raw) return;
  try {
    var t = JSON.parse(raw);
    showToast(t.message, t.kind);
  } catch (e) {}
})();
(function(){
  var params = new URLSearchParams(location.search);
  if (params.get('toast') === 'token_ok') {
    showToast('Token conectado com sucesso.', 'ok');
    params.delete('toast');
    var qs = params.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  }
})();
function openTokenModal(){
  var dlg = document.getElementById('tokenModal');
  document.getElementById('tokenModalError').hidden = true;
  document.getElementById('tokenModalInput').value = '';
  dlg.showModal();
  document.getElementById('tokenModalInput').focus();
}
function closeTokenModal(){
  document.getElementById('tokenModal').close();
}
(function(){
  var dlg = document.getElementById('tokenModal');
  dlg.addEventListener('click', function(e){
    if (e.target === dlg) closeTokenModal();
  });
  document.getElementById('tokenForm').addEventListener('submit', async function(e){
    e.preventDefault();
    var btn = document.getElementById('tokenModalSubmit');
    var errEl = document.getElementById('tokenModalError');
    var token = document.getElementById('tokenModalInput').value.trim();
    errEl.hidden = true;
    btn.disabled = true; btn.textContent = 'Conectando...';
    try {
      const r = await fetch('/api/token', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({refresh_token: token})
      });
      const j = await r.json();
      if (j.ok) { queueToast('Token atualizado com sucesso.', 'ok'); location.reload(); }
      else { errEl.textContent = j.error || 'Erro desconhecido'; errEl.hidden = false; }
    } catch (err) {
      errEl.textContent = 'Erro de rede: ' + err;
      errEl.hidden = false;
    } finally {
      btn.disabled = false; btn.textContent = 'Conectar';
    }
  });
})();
function showToast(message, kind){
  var wrap = document.getElementById('toastWrap');
  if (!wrap) return;
  var el = document.createElement('div');
  el.className = 'toast' + (kind ? ' toast--' + kind : '');
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(function(){ el.remove(); }, 4500);
}
async function refreshData(){
  const btn = document.getElementById('refreshBtn');
  btn.disabled = true; btn.textContent = 'Atualizando...';
  try {
    const r = await fetch('/api/refresh', {method:'POST'});
    const j = await r.json();
    if (j.ok) { queueToast('Dados atualizados com sucesso.', 'ok'); location.reload(); }
    else { showToast('Erro ao atualizar: ' + (j.error || 'desconhecido'), 'error'); btn.disabled = false; btn.textContent = 'Atualizar agora'; }
  } catch (e) {
    showToast('Erro ao atualizar: ' + e, 'error');
    btn.disabled = false; btn.textContent = 'Atualizar agora';
  }
}
async function setTag(id, select){
  const tag = select.value || null;
  try {
    const r = await fetch('/api/tag', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, tag})
    });
    const j = await r.json();
    if (j.ok) { showToast(tag ? 'Tag salva: ' + tag : 'Tag removida', 'ok'); }
    else { showToast('Erro ao salvar tag: ' + (j.error || 'desconhecido'), 'error'); }
  } catch (e) {
    showToast('Erro ao salvar tag: ' + e, 'error');
  }
}
async function setCategoria(id, select){
  const categoria = select.value || null;
  try {
    const r = await fetch('/api/categoria', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, categoria})
    });
    const j = await r.json();
    if (j.ok) { showToast(categoria ? 'Motivo salvo: ' + categoria : 'Motivo removido', 'ok'); }
    else { showToast('Erro ao salvar motivo: ' + (j.error || 'desconhecido'), 'error'); }
  } catch (e) {
    showToast('Erro ao salvar motivo: ' + e, 'error');
  }
}
function appendSelectOptions(kind, names){
  var handlerPrefix = kind === 'origem' ? 'setTag(' : 'setCategoria(';
  var filterSelect = document.getElementById(kind === 'origem' ? 'rcFilterOrigem' : 'rcFilterCategoria');
  function addMissing(selectEl){
    var existing = {};
    Array.prototype.forEach.call(selectEl.options, function(o){ existing[o.value] = true; });
    names.forEach(function(n){
      if (!existing[n]) {
        var o = document.createElement('option'); o.value = n; o.textContent = n; selectEl.appendChild(o);
      }
    });
  }
  if (filterSelect) addMissing(filterSelect);
  document.querySelectorAll('#rcTableBody select.tag-select').forEach(function(sel){
    var onchange = sel.getAttribute('onchange') || '';
    if (onchange.indexOf(handlerPrefix) === 0) addMissing(sel);
  });
}
async function addTagOption(evt, kind){
  evt.preventDefault();
  var input = document.getElementById(kind === 'origem' ? 'newOrigemInput' : 'newCategoriaInput');
  var name = (input.value || '').trim();
  if (!name) return false;
  var endpoint = kind === 'origem' ? '/api/tags' : '/api/categorias';
  try {
    const r = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name})
    });
    const j = await r.json();
    if (j.ok) {
      appendSelectOptions(kind, kind === 'origem' ? j.tags : j.categorias);
      input.value = '';
      showToast((kind === 'origem' ? 'Origem' : 'Motivo') + ' criado(a): ' + name, 'ok');
    } else {
      showToast('Erro ao criar: ' + (j.error || 'desconhecido'), 'error');
    }
  } catch (e) {
    showToast('Erro ao criar: ' + e, 'error');
  }
  return false;
}
</script>
"""


def render_dashboard(
    data: dict,
    interactive: bool,
    extra_panel_html: str = "",
    extra_header_html: str = "",
) -> str:
    ar = data["ar"]
    proj = data["projection"]
    monthly = data["monthly"]
    daily = data["daily"]

    max_count = max((d["count"] for d in daily), default=1) or 1
    bars_html = []
    for d in daily:
        h = round(6 + 84 * (d["count"] / max_count))
        color = score_color(d["avg_score"])
        label = datetime.fromisoformat(d["date"]).strftime("%d/%m")
        tip = f"{label} · {d['count']} reclamação(ões)"
        if d["avg_score"] is not None:
            tip += f" · nota média {d['avg_score']}"
        bars_html.append(
            f'<div class="bar" style="--h:{h}%; --c:{color}" title="{esc(tip)}">'
            f'<span class="bar-fill"></span><span class="bar-label">{label}</span></div>'
        )
    bars_html = "\n".join(bars_html) if bars_html else '<p class="empty">Sem dados suficientes ainda.</p>'

    status_rows = []
    total_status = sum(data["status"].values()) or 1
    for status, count in sorted(data["status"].items(), key=lambda x: -x[1]):
        pct = round(100 * count / total_status, 1)
        label = STATUS_LABELS.get(status, status)
        color = STATUS_COLORS.get(status, "#605E5C")
        status_rows.append(
            f'<div class="status-row">'
            f'<div class="status-row-top"><span class="dot" style="background:{color}"></span>'
            f'<span>{esc(label)}</span><span class="status-count">{count}</span></div>'
            f'<div class="status-track"><div class="status-fill" style="width:{pct}%;background:{color}"></div></div>'
            f"</div>"
        )
    status_html = "\n".join(status_rows) or '<p class="empty">Sem dados.</p>'

    origin_rows = []
    max_origin = max(data["origin"].values(), default=1)
    for origin, count in sorted(data["origin"].items(), key=lambda x: -x[1]):
        pct = round(100 * count / max_origin) if max_origin else 0
        dim = ' style="opacity:.55"' if origin == "Sem tag" else ""
        origin_rows.append(
            f'<div class="origin-row"{dim}><span class="origin-label" title="{esc(origin)}">{esc(origin)}</span>'
            f'<div class="origin-track"><div class="origin-fill" style="width:{pct}%"></div></div>'
            f'<span class="origin-count">{count}</span></div>'
        )
    origin_html = "\n".join(origin_rows) or '<p class="empty">Nenhuma tag atribuída ainda.</p>'

    channel_rows = []
    max_channel = max(data["channel"].values(), default=1)
    for ch, count in sorted(data["channel"].items(), key=lambda x: -x[1]):
        pct = round(100 * count / max_channel) if max_channel else 0
        channel_rows.append(
            f'<div class="origin-row"><span class="origin-label" title="{esc(ch)}">{esc(ch)}</span>'
            f'<div class="origin-track"><div class="origin-fill" style="width:{pct}%;background:var(--violet-deep)"></div></div>'
            f'<span class="origin-count">{count}</span></div>'
        )
    channel_html = "\n".join(channel_rows) or '<p class="empty">Sem dados.</p>'

    # --- tabela evolução mensal ---
    monthly_rows = []
    for row in monthly:
        cur_badge = ' <span class="cur-badge">em andamento</span>' if row["is_current"] else ""
        removed_note = f" ({row['removed']} desativadas)" if row["removed"] else ""
        ma_html = f'<span style="color:{score_color(row["ma"])}">{row["ma"]:.2f}</span>' if row["ma"] is not None else "—"
        sla = esc(row["sla_manual"]) or '<span class="mono" style="color:var(--ink-dim)">— (manual)</span>'
        monthly_rows.append(
            f"<tr><td class='mono'>{row['month_label']}{cur_badge}</td>"
            f"<td class='mono'>{row['active']}{removed_note}</td>"
            f"<td class='mono'>{ma_html}</td>"
            f"<td class='mono'>{fmt(row['eval_rate'], '%')}</td>"
            f"<td class='mono'>{fmt(row['is_pct'], '%')}</td>"
            f"<td class='mono'>{fmt(row['in_pct'], '%')}</td>"
            f"<td class='mono'>{sla}</td></tr>"
        )
    monthly_html = "\n".join(monthly_rows)

    # --- payload pro comparativo de meses (client-side, sem round-trip) ---
    monthly_compare_payload = [
        {
            "key": row["month_key"],
            "label": row["month_label"],
            "total": row["active"],
            "ar": row["ar"],
            "ma": row["ma"],
            "ir": row["ir"],
            "is_pct": row["is_pct"],
            "in_pct": row["in_pct"],
            "origin": row["origin_counts"],
            "categoria": row["categoria_counts"],
        }
        for row in monthly
    ]
    monthly_compare_json = json.dumps(monthly_compare_payload, ensure_ascii=False).replace("</", "<\\/")

    def month_options(selected_key: str) -> str:
        return "".join(
            f'<option value="{esc(row["month_key"])}"{" selected" if row["month_key"] == selected_key else ""}>'
            f'{esc(row["month_label"])}</option>'
            for row in monthly
        )

    cmp_default_b = monthly[-1]["month_key"] if monthly else ""
    cmp_default_a = monthly[-2]["month_key"] if len(monthly) >= 2 else cmp_default_b
    monthly_opts_a = month_options(cmp_default_a)
    monthly_opts_b = month_options(cmp_default_b)

    # --- projeção (virada de mês) ---
    def delta_html(cur, new, suffix="", higher_is_better=True):
        if cur is None or new is None:
            return '<span class="mono" style="color:var(--ink-dim)">—</span>'
        d = round(new - cur, 2)
        if abs(d) < 0.005:
            return '<span class="mono" style="color:var(--ink-dim)">sem variação</span>'
        up = d > 0
        good = up if higher_is_better else not up
        color = "var(--mint)" if good else "var(--coral)"
        arrow = "▲" if up else "▼"
        sign = "+" if up else ""
        return f'<span class="mono" style="color:{color}">{arrow} {sign}{d}{suffix}</span>'

    proj_ar_html = f'{proj["ar"]:.2f}' if proj["ar"] is not None else "—"
    proj_label_html = (
        f'<span class="pill" style="background:{proj["label_color"]}22;color:{proj["label_color"]}">{esc(proj["label"])}</span>'
    )
    proj_components = [
        ("Nota do Consumidor (MA)", ar["ma"], proj["ma"], ""),
        ("Índice de Resposta (IR)", ar["ir"], proj["ir"], "%"),
        ("Solução (IS)", ar["is_pct"], proj["is_pct"], "%"),
        ("Voltaria a fazer negócio (IN)", ar["in_pct"], proj["in_pct"], "%"),
    ]
    proj_table_html = "\n".join(
        f"<tr><td>{esc(label)}</td>"
        f"<td class='mono'>{fmt(cur, suf)}</td>"
        f"<td class='mono'>{fmt(new, suf)}</td>"
        f"<td>{delta_html(cur, new, suf)}</td></tr>"
        for label, cur, new, suf in proj_components
    )

    # --- janela de 6 meses: o que sai e o que entra na virada ---
    # monthly[0] é sempre o mês mais antigo da janela oficial do AR (o próximo a
    # cair fora quando o mês virar) e monthly[-1] é sempre o mês corrente
    # (parcial, o que está entrando) — mesma matemática de compute_ar_window()
    # e compute_monthly_evolution(), só reaproveitada aqui pra explicar a causa
    # por trás do número, não só o resultado agregado.
    outgoing, incoming = monthly[0], monthly[-1]

    def month_ref(row):
        return row["ar"] if row["ar"] is not None else row["ma"]

    def month_color(row):
        ref = month_ref(row)
        return score_color(ref) if ref is not None else "#8B7FA3"

    timeline_chips = []
    for i, row in enumerate(monthly):
        tag = ""
        extra = ""
        if i == 0:
            tag = '<span class="chip-tag chip-tag--out">sai</span>'
            extra = " chip--fade"
        elif row["is_current"]:
            tag = '<span class="chip-tag chip-tag--in">parcial</span>'
        timeline_chips.append(
            f'<div class="chip{extra}"><div class="chip-dot" style="background:{month_color(row)}"></div>'
            f'<div class="chip-label">{esc(row["month_label"])}</div>{tag}</div>'
        )
    timeline_html = "".join(timeline_chips)

    out_ref, win_ref = month_ref(outgoing), ar["ar"] if ar["ar"] is not None else ar["ma"]
    if out_ref is None or win_ref is None:
        turnover_insight = "Ainda não há avaliações suficientes no mês que sai pra estimar o efeito na nota."
    elif out_ref < win_ref - 0.05:
        turnover_insight = f"{outgoing['month_label']} está abaixo da média da janela atual — ao sair, tende a puxar o AR pra cima."
    elif out_ref > win_ref + 0.05:
        turnover_insight = f"{outgoing['month_label']} está acima da média da janela atual — ao sair, tende a puxar o AR pra baixo."
    else:
        turnover_insight = f"{outgoing['month_label']} está próximo da média da janela atual — a saída dele tende a ter pouco efeito."

    turnover_cards_html = f"""
    <div class="turnover-grid">
      <div class="proj-card">
        <div class="label">Sai da janela — {esc(outgoing['month_label'])}</div>
        <div class="value" style="font-size:22px;color:{month_color(outgoing)}">{fmt(outgoing['ar']) if outgoing['ar'] is not None else fmt(outgoing['ma'])}</div>
        <div class="sub">{outgoing['active']} reclamações · nota {fmt(outgoing['ma'])} · {fmt(outgoing['eval_rate'],'%')} avaliadas</div>
      </div>
      <div class="proj-arrow">→</div>
      <div class="proj-card proj-card--future">
        <div class="label">Entra (parcial) — {esc(incoming['month_label'])}</div>
        <div class="value" style="font-size:22px;color:{month_color(incoming)}">{fmt(incoming['ar']) if incoming['ar'] is not None else fmt(incoming['ma'])}</div>
        <div class="sub">{incoming['active']} reclamações até agora · nota {fmt(incoming['ma'])}</div>
      </div>
    </div>
    <p class="panel-note" style="margin:0 0 16px">&#128161; {esc(turnover_insight)}</p>"""

    # --- tabela de reclamações (com tag de origem e motivo/categoria) ---
    def build_select(cid: str, current: str, options: list[str], empty_label: str, css_class: str, handler: str) -> str:
        # antes isso era montado com um .replace() em cima da string de options
        # pra injetar o "selected" — quebrava se o nome de uma tag fosse prefixo
        # de outra ("CSM" x "CSM Pleno") e agora quebraria de vez com o escaping.
        opts = [f'<option value=""{" selected" if not current else ""}>{esc(empty_label)}</option>']
        for t in options:
            sel = " selected" if t == current else ""
            opts.append(f'<option value="{esc(t)}"{sel}>{esc(t)}</option>')
        cid_js = json.dumps(str(cid))  # id seguro pra dentro do onchange
        return (
            f'<select class="{css_class}" onchange="{handler}({esc(cid_js)}, this)">'
            f'{"".join(opts)}</select>'
        )

    table_rows = []
    for c in data["recent"]:
        created = c.get("created", "")
        try:
            created_fmt = datetime.fromisoformat(created).strftime("%d/%m/%Y %H:%M")
        except ValueError:
            created_fmt = created
        status = c.get("status") or "DESCONHECIDO"
        status_label = STATUS_LABELS.get(status, status)
        status_color = STATUS_COLORS.get(status, "#605E5C")
        score = c.get("score")
        score_html = f'<span style="color:{score_color(score)}">{score:.1f}</span>' if score is not None else "—"
        city = c.get("userCity") or "—"
        state = c.get("userState") or ""
        title_full = (c.get("title") or "").strip()
        title = title_full
        if len(title) > 60:
            title = title[:57] + "…"
        cid = c.get("id")
        current_tag = c.get("tag_origem") or ""
        current_categoria = c.get("categoria") or ""
        if interactive:
            tag_cell = build_select(cid, current_tag, data["tags"], "Sem tag", "tag-select", "setTag")
            categoria_cell = build_select(cid, current_categoria, data["categorias"], "Sem motivo", "tag-select", "setCategoria")
        else:
            tag_cell = esc(current_tag) or '<span style="color:var(--ink-dim)">—</span>'
            categoria_cell = esc(current_categoria) or '<span style="color:var(--ink-dim)">—</span>'
        row_attrs = (
            f' data-status="{esc(status)}" data-origem="{esc(current_tag)}"'
            f' data-categoria="{esc(current_categoria)}" data-title="{esc(title_full.lower())}"'
        )
        table_rows.append(
            f"<tr{row_attrs}><td class='title-cell'>{esc(title)}</td>"
            f"<td>{esc(city)}{'/' + esc(state) if state else ''}</td>"
            f"<td class='mono'>{esc(created_fmt)}</td>"
            f"<td><span class='pill' style='background:{status_color}22;color:{status_color}'>{esc(status_label)}</span></td>"
            f"<td class='mono score-cell'>{score_html}</td>"
            f"<td>{tag_cell}</td>"
            f"<td>{categoria_cell}</td></tr>"
        )
    table_html = "\n".join(table_rows) or '<tr><td colspan="7" class="empty">Nenhuma reclamação ativa.</td></tr>'

    status_filter_opts = "".join(
        f'<option value="{esc(s)}">{esc(STATUS_LABELS.get(s, s))}</option>'
        for s in sorted(data["status"].keys())
    )
    origem_filter_opts = "".join(f'<option value="{esc(t)}">{esc(t)}</option>' for t in data["tags"])
    categoria_filter_opts = "".join(f'<option value="{esc(t)}">{esc(t)}</option>' for t in data["categorias"])

    updated_str = data["updated_at"].strftime("%d/%m/%Y às %H:%M")
    stale_hours = (datetime.now() - data["updated_at"]).total_seconds() / 3600
    if stale_hours >= 72:
        stale_html = f'<span class="pill" style="background:#FF6B6D22;color:var(--coral)">dado desatualizado há {int(stale_hours // 24)}d</span>'
    elif stale_hours >= 24:
        stale_html = f'<span class="pill" style="background:#FFC93D22;color:var(--amber)">atualize os dados — {int(stale_hours // 24)}d sem atualizar</span>'
    else:
        stale_html = ""
    ar_html = f'{ar["ar"]:.2f}' if ar["ar"] is not None else "—"
    label_html = (
        f'<span class="pill" style="background:{ar["label_color"]}22;color:{ar["label_color"]}">{esc(ar["label"])}</span>'
    )

    ra1000_cards = []
    for crit in ar["ra1000"]["criteria"]:
        ok = crit["ok"]
        icon = '<span class="ra1000-icon ra1000-icon--ok">✓</span>' if ok else '<span class="ra1000-icon ra1000-icon--pending">–</span>'
        ra1000_cards.append(
            f'<div class="ra1000-card{" ra1000-card--ok" if ok else ""}">'
            f'{icon}<div class="ra1000-card-value">{esc(crit["value"])}</div>'
            f'<div class="ra1000-card-label">{esc(crit["label"])}</div></div>'
        )
    ra1000_html = "\n".join(ra1000_cards)
    ra1000_met, ra1000_total = ar["ra1000"]["met"], ar["ra1000"]["total"]
    ra1000_eligible = ar["ra1000"]["eligible_numerically"]
    ra1000_pill_html = (
        '<span class="pill" style="background:var(--mint);color:#05230F">Elegível numericamente</span>'
        if ra1000_eligible else
        '<span class="pill" style="background:var(--border-strong);color:var(--ink-soft)">Ainda não elegível</span>'
    )
    ra1000_summary = (
        f'{ra1000_met}/{ra1000_total} critérios numéricos batem'
        + (" · elegível ao RA1000 (falta auditoria/cadastro do RA)" if ra1000_eligible else "")
    )

    refresh_button = (
        '<button id="refreshBtn" class="btn btn-refresh" onclick="refreshData()">Atualizar agora</button>'
        if interactive else ""
    )
    token_modal_html = TOKEN_MODAL_HTML if interactive else ""
    manage_tags_html = f"""
  <div class="panel">
    <p class="panel-title">Gerenciar origens e categorias</p>
    <p class="panel-note">Cria novas opções pros seletores de Origem e Motivo acima.</p>
    <div class="manage-tags">
      <div class="manage-tags-col">
        <p class="small-label">Nova origem</p>
        <form class="manage-tags-form" onsubmit="return addTagOption(event, 'origem')">
          <input type="text" id="newOrigemInput" placeholder="Ex.: Financeiro" maxlength="60">
          <button type="submit" class="btn btn-ghost">Adicionar</button>
        </form>
      </div>
      <div class="manage-tags-col">
        <p class="small-label">Novo motivo</p>
        <form class="manage-tags-form" onsubmit="return addTagOption(event, 'categoria')">
          <input type="text" id="newCategoriaInput" placeholder="Ex.: Cobrança indevida" maxlength="60">
          <button type="submit" class="btn btn-ghost">Adicionar</button>
        </form>
      </div>
    </div>
  </div>""" if interactive else ""
    script = TABS_JS + (SCRIPT_JS if interactive else "")

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Next RA</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
{FONT_LINKS}
<style>
{CSS}
</style>
<script>
(function(){{
  try {{
    var saved = localStorage.getItem('nextRaTheme');
    if (saved === 'light' || saved === 'dark') document.documentElement.dataset.theme = saved;
  }} catch (e) {{}}
}})();
</script>
</head>
<body>

<div class="bg-glow"></div>

<div class="dock-wrap">
  <nav class="dock">
    <div class="dock-tabs">
      <button type="button" class="tab-btn active" data-tab="geral">Visão geral</button>
      <button type="button" class="tab-btn" data-tab="mensal">Evolução mensal</button>
      <button type="button" class="tab-btn" data-tab="volume">Volume &amp; canais</button>
      <button type="button" class="tab-btn" data-tab="reclamacoes">Reclamações</button>
      <button type="button" class="tab-btn" data-tab="ra1000">RA1000</button>
    </div>
    <div class="dock-actions">
      <button type="button" class="kebab-btn" id="kebabToggle" onclick="toggleKebabMenu()" title="Mais opções" aria-label="Mais opções" aria-haspopup="true" aria-expanded="false">&#8942;</button>
      <div class="kebab-menu" id="kebabMenu" hidden>
        <div class="kebab-brand"><span class="wordmark"><span class="dot"></span>Next RA</span></div>
        <button type="button" class="kebab-item" onclick="toggleTheme()"><span id="themeToggleLabel">Mudar para tema claro</span></button>
        {refresh_button}
        <div class="kebab-links">{extra_header_html}</div>
      </div>
    </div>
  </nav>
</div>

<div class="wrap">

  <div class="status-line">
    Atualizado às <b>{updated_str}</b>{(' ' + stale_html) if stale_html else ''} ·
    <b>{data['total_active']}</b> ativas · <b>{data['total_deactivated']}</b> desativadas (histórico)
  </div>

  <div class="hero-strip">
    <div class="hero-ar">
      <div class="label">AR &middot; janela oficial ({ar['window_start']}–{ar['window_end']}){info_icon("Nota oficial de reputação do Reclame Aqui, de 0 a 10. Combina Índice de Resposta, Nota do Consumidor, Índice de Solução e Voltaria a Fazer Negócio numa única fórmula.")}</div>
      <div class="value num">{ar_html}</div>
      <div class="window">{label_html} · defasagem de {AR_WINDOW_LAG_MONTHS} mês(es) (não conta o mês corrente)</div>
    </div>
    <div class="kpi">
      <div class="label">Nota média (MA){info_icon("Média das notas de 0 a 10 dadas pelos consumidores nas avaliações da janela.")}</div>
      <div class="value num" style="color:{score_color(ar['ma'])}">{fmt(ar['ma'])}</div>
      <div class="sub">{ar['n_evaluated']} de {ar['total']} avaliadas na janela</div>
    </div>
    <div class="kpi">
      <div class="label">Índice de resposta (IR){info_icon("Percentual de reclamações da janela que receberam alguma resposta da empresa.")}</div>
      <div class="value num">{fmt(ar['ir'], '%')}</div>
      <div class="sub">{ar['answered']} respondidas na janela</div>
    </div>
    <div class="kpi">
      <div class="label">Solução (IS) · Voltaria (IN){info_icon("IS = % de reclamações que o consumidor marcou como resolvida. IN = % que disse que voltaria a fazer negócio com a empresa.")}</div>
      <div class="value num" style="font-size:21px">{fmt(ar['is_pct'],'%')} · {fmt(ar['in_pct'],'%')}</div>
      <div class="sub">componentes do AR na janela</div>
    </div>
  </div>

  <div class="tab-panel" data-tab="geral">

  <div class="panel">
    <p class="panel-title">Projeção — virada de mês</p>
    <p class="panel-note">
      Simula a janela que o RA passa a usar quando o mês fechar: os últimos {AR_WINDOW_MONTHS} meses
      terminando em {proj['window_end']} (em andamento), contra a janela oficial vigente hoje
      ({ar['window_start']}–{ar['window_end']}).
    </p>

    <div class="timeline">{timeline_html}</div>
    {turnover_cards_html}

    <p class="panel-title" style="font-size:12.5px;color:var(--ink-soft);margin:4px 0 10px">Impacto agregado no AR</p>
    <div class="proj-grid">
      <div class="proj-card">
        <div class="label">AR oficial hoje ({ar['window_start']}–{ar['window_end']})</div>
        <div class="value" style="color:{score_color(ar['ar'])}">{ar_html}</div>
        <div class="sub">{label_html}</div>
      </div>
      <div class="proj-arrow">→</div>
      <div class="proj-card proj-card--future">
        <div class="label">Projeção na virada ({proj['window_start']}–{proj['window_end']})</div>
        <div class="value" style="color:{score_color(proj['ar'])}">{proj_ar_html}</div>
        <div class="sub">{proj_label_html}</div>
      </div>
      <div class="proj-delta">
        <div class="label">Variação estimada</div>
        <div class="value" style="font-size:22px">{delta_html(ar['ar'], proj['ar'])}</div>
      </div>
    </div>
    <div class="proj-progress-top">
      <span>Mês corrente ({proj['window_end']}) em andamento</span>
      <span class="mono">dia {proj['day_of_month']}/{proj['days_in_month']} · {proj['month_progress_pct']}% decorrido</span>
    </div>
    <div class="status-track"><div class="status-fill" style="width:{proj['month_progress_pct']}%;background:var(--violet-bright)"></div></div>
    <p class="panel-note" style="margin:10px 0 0">
      Quanto mais cedo no mês, mais a projeção ainda pode mudar — poucas avaliações do mês corrente pesam
      bastante na média. A janela de projeção só vira a janela oficial de fato quando o mês fechar.
    </p>
    <table style="margin-top:16px">
      <thead><tr><th>Componente</th><th>Oficial hoje</th><th>Projeção</th><th>Variação</th></tr></thead>
      <tbody>{proj_table_html}</tbody>
    </table>
  </div>

  {extra_panel_html}

  </div>

  <div class="tab-panel" data-tab="mensal" hidden>

  <div class="panel">
    <p class="panel-title">Evolução mensal</p>
    <p class="panel-note">Últimos {len(monthly)} meses · reclamações desativadas não entram nos índices, mas são contadas à parte</p>
    <table>
      <thead><tr><th>Mês</th><th>Reclamações</th><th>Nota</th><th>Avaliações</th><th>Solução</th><th>Voltaria</th><th>SLA médio</th></tr></thead>
      <tbody>{monthly_html}</tbody>
    </table>
  </div>

  <div class="panel">
    <p class="panel-title">Comparar dois meses</p>
    <p class="panel-note">KPIs, origem e motivo lado a lado &mdash; o que subiu e o que caiu de um mês pro outro.</p>
    <div class="cmp-select-row">
      <select id="cmpMonthA">{monthly_opts_a}</select>
      <span class="cmp-vs">vs</span>
      <select id="cmpMonthB">{monthly_opts_b}</select>
    </div>
    <div id="cmpResult"></div>
  </div>
  <script type="application/json" id="monthlyCompareData">{monthly_compare_json}</script>

  </div>

  <div class="tab-panel" data-tab="volume" hidden>

  <div class="panel">
    <p class="panel-title">Volume diário de reclamações</p>
    <p class="panel-note">Últimos 14 dias com movimento · cor = nota média das avaliações do dia</p>
    <div class="bars">
      {bars_html}
    </div>
  </div>

  <div class="two-col">
    <div class="panel">
      <p class="panel-title">Status</p>
      <p class="panel-note">Distribuição das reclamações ativas</p>
      {status_html}
    </div>
    <div class="panel">
      <p class="panel-title">Canal de abertura</p>
      <p class="panel-note">site / app / mobile (campo da API)</p>
      {channel_html}
    </div>
  </div>

  </div>

  <div class="tab-panel" data-tab="ra1000" hidden>

  <div class="panel">
    <div class="ra1000-hero">
      <div class="ra1000-hero-badge{' ra1000-hero-badge--ok' if ra1000_eligible else ''}">🏆</div>
      <div>
        <div class="ra1000-hero-value">{ra1000_met}/{ra1000_total}</div>
        <div class="ra1000-hero-label">critérios do Selo RA1000 batendo</div>
        <div style="margin-top:8px">{ra1000_pill_html}</div>
      </div>
    </div>
    <p class="panel-note" style="margin:16px 0 0">calculado na mesma janela de 6 meses do AR (blog.reclameaqui.com.br/selo-ra1000) · falta auditoria e cadastro pelo próprio Reclame Aqui pra confirmar o selo</p>
  </div>

  <div class="panel">
    <p class="panel-title">Critérios</p>
    <p class="panel-note">{esc(ra1000_summary)}</p>
    <div class="ra1000-grid">
      {ra1000_html}
    </div>
  </div>

  </div>

  <div class="tab-panel" data-tab="reclamacoes" hidden>

  <div class="panel">
    <p class="panel-title">Origem interna</p>
    <p class="panel-note">Tags atribuídas manualmente (Suporte Técnico, CSM, Aluno, Comercial, Produto...)</p>
    {origin_html}
  </div>

  <div class="panel">
    <p class="panel-title">Reclamações</p>
    <p class="panel-note">Todas as ativas{' · clique numa tag pra classificar origem/motivo' if interactive else ''}</p>

    <div class="filter-bar">
      <input type="search" id="rcFilterTitle" placeholder="Buscar por título…" oninput="applyComplaintFilters()">
      <select id="rcFilterStatus" onchange="applyComplaintFilters()">
        <option value="">Todos os status</option>
        {status_filter_opts}
      </select>
      <select id="rcFilterOrigem" onchange="applyComplaintFilters()">
        <option value="">Toda origem</option>
        {origem_filter_opts}
      </select>
      <select id="rcFilterCategoria" onchange="applyComplaintFilters()">
        <option value="">Todo motivo</option>
        {categoria_filter_opts}
      </select>
    </div>
    <p class="filter-count" id="rcFilterCount"></p>

    <table id="rcTable">
      <thead><tr><th>Título</th><th>Cidade/UF</th><th>Criada em</th><th>Status</th><th>Nota</th><th>Origem</th><th>Motivo</th></tr></thead>
      <tbody id="rcTableBody">
        {table_html}
      </tbody>
    </table>
    <div class="pagination" id="rcPagination"></div>
  </div>

  {manage_tags_html}

  </div>

  <footer>
    <strong>AR</strong> = ((IR×2) + (MA×10×3) + (IS×3) + (IN×2)) ÷ 100 — fórmula oficial do Reclame Aqui
    (blog.reclameaqui.com.br/conheca-os-indicadores-de-reputacao-do-reclame-aqui), calculada sobre uma janela
    móvel de {AR_WINDOW_MONTHS} meses que termina {AR_WINDOW_LAG_MONTHS} mês(es) atrás de hoje — confirmado
    comparando com o painel oficial em 01/09/2026 (janela mostrada lá: 01/03–31/08/2026, sem
    setembro). Faixas: Ótima 8–10 · Boa 7–7,9 · Regular 6–6,9 · Ruim 5–5,9 · Não Recomendada (nota &lt;5
    ou resposta &lt;50%, independente da nota) · Sem Reputação Definida com menos de 10 avaliações na janela.
    Isso é uma reprodução da regra pública, não o painel oficial — o RA pode considerar detalhes internos de
    auditoria e moderação que não aparecem na API. SLA médio de resposta ainda é manual: o payload não traz o
    horário da resposta, só se ela existe. Tags de origem também são manuais — a API não classifica isso.
  </footer>

</div>
{token_modal_html}
{script}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# SERVIDOR LOCAL
# ---------------------------------------------------------------------------
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt_str, *args):
        print(f"  [http] {fmt_str % args}")

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with DB_LOCK:
                db = load_db()
                data = build_dashboard_data(db)
            html = render_dashboard(data, interactive=True)
            self._send_html(html)
        elif self.path == "/api/tags":
            with DB_LOCK:
                db = load_db()
            self._send_json({"tags": db.get("tags", list(DEFAULT_TAGS))})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = {}

        if self.path == "/api/refresh":
            try:
                # a chamada à API do RA fica FORA do lock (é a parte lenta) —
                # só o ciclo load/merge/save é serializado.
                fetched, complete = fetch_complaints_now()
                with DB_LOCK:
                    db = load_db()
                    db = merge_complaints(db, fetched, complete=complete)
                    save_db(db)
                    data = build_dashboard_data(db)
                OUTPUT_HTML.write_text(render_dashboard(data, interactive=False), encoding="utf-8")
                self._send_json({
                    "ok": True,
                    "total_ativas": data["total_active"],
                    "coleta_completa": complete,
                })
            except Exception as e:
                print(f"  [refresh] erro: {e}")
                self._send_json({"ok": False, "error": str(e)}, 500)

        elif self.path == "/api/tag":
            cid = str(body.get("id", ""))
            tag = body.get("tag") or None
            with DB_LOCK:
                db = load_db()
                if cid in db["complaints"]:
                    db["complaints"][cid]["tag_origem"] = tag
                    if tag and tag not in db["tags"]:
                        db["tags"].append(tag)
                    save_db(db)
                    ok = True
                else:
                    ok = False
            if ok:
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": "reclamação não encontrada"}, 404)

        elif self.path == "/api/tags":
            name = (body.get("name") or "").strip()[:60]
            with DB_LOCK:
                db = load_db()
                if name and name not in db["tags"]:
                    db["tags"].append(name)
                    save_db(db)
                tags = list(db["tags"])
            self._send_json({"ok": True, "tags": tags})

        else:
            self._send_json({"error": "not found"}, 404)


def run_server(port: int, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\nServidor rodando em {url}  (Ctrl+C pra parar)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando...")
        server.shutdown()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    global DEMO_MODE

    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="usa sample_response.json em vez da API real")
    parser.add_argument("--serve", action="store_true", help="sobe servidor local interativo (botão refresh + tags)")
    parser.add_argument("--port", type=int, default=8765, help="porta do servidor local (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="não abre o navegador automaticamente com --serve")
    args = parser.parse_args()

    DEMO_MODE = args.demo

    if args.serve:
        # garante que já existe um banco (mesmo vazio) antes de subir o servidor
        db = load_db()
        try:
            fetched, complete = fetch_complaints_now()
            db = merge_complaints(db, fetched, complete=complete)
            save_db(db)
            print(f"  banco atualizado: {len(db['complaints'])} reclamações no histórico")
        except Exception as e:
            print(f"  aviso: não consegui buscar dados agora ({e}). Servindo com o banco existente.")
        run_server(args.port, open_browser=not args.no_browser)
        return

    # modo antigo: gera dashboard.html uma vez e sai (sem botões, sem edição de tags)
    fetched, complete = fetch_complaints_now()
    db = load_db()
    db = merge_complaints(db, fetched, complete=complete)
    save_db(db)

    data = build_dashboard_data(db)
    html = render_dashboard(data, interactive=False)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"\nDashboard gerado: {OUTPUT_HTML}")
    print("(dica: rode com --serve pra ter botão de atualizar e edição de tags de origem)")


if __name__ == "__main__":
    main()
