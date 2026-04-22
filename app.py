"""
ARB TRIANGULAR BOT v6 - SEM CCXT PARA DADOS DE MERCADO
=======================================================
Problema raiz identificado: o ccxt.fetch_order_book() estava a falhar
porque o Render (AWS us-east-1) e detectado pela Binance como datacenter
e algumas chamadas sao bloqueadas ou retornam vazio.

SOLUCAO: todas as chamadas a Binance sao feitas via requests directos
para a API REST publica - sem ccxt para dados de mercado.
  - Orderbook: GET /api/v3/depth (publico, sem auth, sem restricao geo)
  - Preco BTC: GET /api/v3/ticker/price (publico)
  - Saldo real: GET /api/v3/account (privado, HMAC, sem restricao geo)
  - Ordens reais: ainda usa ccxt (so necessario no modo real)

Saldo:
  - Simulacao: saldo virtual = capital inicial, cresce a cada arb simulada
  - Real: lido de /api/v3/account (Conta a Vista / Spot USDT)
"""
from flask import Flask, jsonify, request
import ccxt, threading, time, os, traceback
import hmac, hashlib
import requests as req
from datetime import datetime

app   = Flask(__name__)
ex    = None   # so usado para ordens reais
_lock = threading.Lock()

# ── Sessao HTTP com retry e timeout ──────────────────────────────────────────
SESSION = req.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

BINANCE_BASE = "https://api.binance.com"

BOT = {
    "running": False, "paper": True,
    "capital": 10.0, "cap_inicial": 10.0, "cap_base": 10.0,
    "saldo_conta": 0.0,
    "lucro_total": 0.0, "lucro_ciclo": 0.0,
    "ciclos_jc": 0, "gatilho_jc": 10.0,
    "arbs_exec": 0, "arbs_achadas": 0, "arbs_rejeit": 0,
    "scans": 0, "melhor": 0.0, "drawdown": 0.0,
    "lucro_min": 0.20, "slip_max": 0.05, "liq_min": 500, "max_dd": 10.0,
    "api_key":    os.getenv("BINANCE_API_KEY", ""),
    "api_secret": os.getenv("BINANCE_API_SECRET", ""),
    "cooldowns": {}, "logs": [], "scan_data": [],
    "last_arb": None, "marcos": [],
    "arbs_hora": 0, "hora_atual": datetime.now().hour,
    "btc_preco": 0.0,
}

TAXA = 0.00075

TRIANGULOS = [
    ["USDT","BTC","ETH"],  ["USDT","BTC","BNB"],  ["USDT","ETH","BNB"],
    ["USDT","BTC","SOL"],  ["USDT","ETH","SOL"],  ["USDT","BNB","SOL"],
    ["USDT","BTC","XRP"],  ["USDT","ETH","XRP"],  ["USDT","BTC","ADA"],
    ["USDT","BTC","DOGE"], ["USDT","ETH","DOGE"], ["USDT","BNB","XRP"],
    ["USDT","BTC","AVAX"], ["USDT","ETH","AVAX"], ["USDT","BTC","LINK"],
    ["USDT","ETH","LINK"], ["USDT","BTC","MATIC"],["USDT","ETH","MATIC"],
    ["USDT","BNB","MATIC"],["USDT","BTC","DOT"],
]

# ── Logging ───────────────────────────────────────────────────────────────────
def add_log(msg, t="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        BOT["logs"].insert(0, {"ts": ts, "msg": msg, "t": t})
        if len(BOT["logs"]) > 300:
            BOT["logs"] = BOT["logs"][:300]
    print(f"{ts} [{t}] {msg}")

# ═════════════════════════════════════════════════════════════════════════════
#  BINANCE API REST DIRECTA — sem ccxt, sem restricoes
# =============================================================================

def binance_get_public(path, params=None):
    """
    Chamada GET publica a Binance (sem autenticacao).
    Endpoint /api/v3/* e global — sem restricao geografica.
    """
    try:
        url = BINANCE_BASE + path
        r   = SESSION.get(url, params=params, timeout=6)
        if r.status_code == 200:
            return r.json()
        add_log(f"Binance HTTP {r.status_code}: {r.text[:60]}", "warn")
        return None
    except Exception as e:
        add_log(f"REST public: {str(e)[:60]}", "warn")
        return None

def binance_get_private(path, api_key, api_secret, params=None):
    """
    Chamada GET privada a Binance (HMAC-SHA256).
    Usa /api/v3/account — nao e geo-restrito.
    O /sapi/v1/capital/* e que e restrito (para saques).
    """
    try:
        p  = dict(params or {})
        p["timestamp"]  = int(time.time() * 1000)
        p["recvWindow"] = 10000
        qs  = "&".join(f"{k}={v}" for k, v in p.items())
        sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = BINANCE_BASE + path + "?" + qs + "&signature=" + sig
        r   = SESSION.get(url, headers={"X-MBX-APIKEY": api_key}, timeout=8)
        return r.json()
    except Exception as e:
        add_log(f"REST private: {str(e)[:60]}", "warn")
        return None

def get_preco_btc():
    """Testa conectividade e retorna preco BTC."""
    data = binance_get_public("/api/v3/ticker/price", {"symbol": "BTCUSDT"})
    if data and "price" in data:
        return float(data["price"])
    return None

def get_orderbook(symbol_ccxt, limit=10):
    """
    Busca orderbook via REST directo.
    symbol_ccxt: ex "BTC/USDT" -> converte para "BTCUSDT"
    Retorna {"bids": [[preco, qty],...], "asks": [...]}
    """
    symbol = symbol_ccxt.replace("/", "")
    data   = binance_get_public("/api/v3/depth", {"symbol": symbol, "limit": limit})
    return data  # {"bids":[], "asks":[]} ou None

def get_saldo_usdt(api_key, api_secret):
    """
    Le saldo USDT da Conta a Vista (Spot) da Binance.
    Usa /api/v3/account que e endpoint global (nao geo-restrito).
    """
    data = binance_get_private("/api/v3/account", api_key, api_secret)
    if data and "balances" in data:
        for b in data["balances"]:
            if b["asset"] == "USDT":
                return float(b["free"])
        add_log("USDT nao encontrado nos balances", "warn")
        return 0.0
    if data:
        msg = data.get("msg", str(data))[:80]
        add_log(f"Saldo erro: {msg}", "warn")
    return None

# ═════════════════════════════════════════════════════════════════════════════
#  CALCULAR ARBITRAGEM (usando orderbook REST directo)
# =============================================================================

def preco_ob_rest(symbol_ccxt, lado, usdt):
    """
    Calcula preco medio de execucao a partir do orderbook real.
    lado="c" -> comprar (usa asks), lado="v" -> vender (usa bids)
    """
    ob = get_orderbook(symbol_ccxt, limit=10)
    if ob is None:
        return None, None, None

    ns = ob.get("asks" if lado == "c" else "bids", [])
    if not ns:
        return None, None, None

    try:
        best = float(ns[0][0])
        liq  = sum(float(p) * float(q) for p, q in ns)
        if liq < BOT["liq_min"]:
            return None, None, liq

        acum = custo = 0.0
        for p, q in ns:
            p, q = float(p), float(q)
            v    = p * q
            if acum + v >= usdt:
                custo += usdt - acum
                acum   = usdt
                break
            custo += v
            acum  += v

        if acum < usdt * 0.90:
            return None, None, liq

        med  = custo / (acum / best)
        slip = abs(med - best) / best * 100
        return med, slip, liq
    except:
        return None, None, None

def calcular(tri, capital):
    base, A, B = tri
    try:
        # Perna 1: USDT -> A  (comprar A com USDT)
        p1, s1, l1 = preco_ob_rest(A + "/" + base, "c", capital)
        if p1 is None: return None
        qa = (capital / p1) * (1 - TAXA)

        # Perna 2: A -> B  (comprar B com A)
        p2, s2, l2 = preco_ob_rest(B + "/" + A, "c", qa * p1 * (1 - TAXA))
        if p2 is None: return None
        qb = (qa / p2) * (1 - TAXA)

        # Perna 3: B -> USDT  (vender B por USDT)
        p3, s3, l3 = preco_ob_rest(B + "/" + base, "v", qb * p2 * p1 * (1 - TAXA))
        if p3 is None: return None
        final = qb * p3 * (1 - TAXA)

        lucro = final - capital
        pct   = lucro / capital * 100
        slip  = (s1 or 0) + (s2 or 0) + (s3 or 0)
        lmin  = min(l1 or 0, l2 or 0, l3 or 0)

        return {
            "tri":    base + ">" + A + ">" + B + ">" + base,
            "label":  A + " > " + B,
            "pares":  [A + "/" + base, B + "/" + A, B + "/" + base],
            "precos": [p1, p2, p3],
            "qtds":   [qa, qb, final],
            "capital": capital,
            "lucro":  round(lucro, 8),
            "pct":    round(pct, 6),
            "slip":   round(slip, 6),
            "lmin":   round(lmin, 2),
            "ok": (pct  >= BOT["lucro_min"] and
                   slip <= BOT["slip_max"]  and
                   lmin >= BOT["liq_min"]),
        }
    except:
        return None

# ── Juros compostos ───────────────────────────────────────────────────────────
def registar_lucro(lucro):
    BOT["capital"]     += lucro
    BOT["lucro_total"] += lucro
    BOT["lucro_ciclo"] += lucro
    if BOT["paper"]:
        BOT["saldo_conta"] += lucro
    g = BOT["cap_base"] * (BOT["gatilho_jc"] / 100)
    if BOT["lucro_ciclo"] >= g:
        antes  = BOT["cap_base"]
        depois = BOT["capital"]
        lc     = BOT["lucro_ciclo"]
        ganho  = lc / antes * 100
        BOT["ciclos_jc"]  += 1
        BOT["cap_base"]    = depois
        BOT["lucro_ciclo"] = 0.0
        BOT["marcos"].insert(0, {
            "ciclo": BOT["ciclos_jc"],
            "antes": round(antes, 6), "depois": round(depois, 6),
            "lucro": round(lc, 6), "ganho": round(ganho, 4),
            "data":  datetime.now().strftime("%d/%m %H:%M"),
        })
        BOT["marcos"] = BOT["marcos"][:20]
        add_log(
            "JUROS COMPOSTOS #" + str(BOT["ciclos_jc"]) +
            " | $" + "%.4f" % antes + " -> $" + "%.4f" % depois +
            " (+" + "%.4f" % ganho + "%)",
            "compound"
        )

# ── Executar arb ──────────────────────────────────────────────────────────────
def executar_arb(res):
    if BOT["paper"]:
        add_log(
            "SIM | " + res["label"] +
            " | +$" + "%.6f" % res["lucro"] +
            " (+" + "%.4f" % res["pct"] + "%)" +
            " | slip " + "%.4f" % res["slip"] + "%",
            "success"
        )
        return True, res["lucro"]

    # MODO REAL — usa ccxt para criar ordens
    global ex
    if ex is None:
        add_log("ccxt nao inicializado para modo real", "error")
        return False, 0

    par1, par2, par3 = res["pares"]
    p1,   p2,   _    = res["precos"]
    qa,   qb,   _    = res["qtds"]
    try:
        t0 = time.time()
        o1 = ex.create_market_order(par1, "buy",  res["capital"] / p1)
        time.sleep(0.08)
        o2 = ex.create_market_order(par2, "buy",  float(o1.get("filled", qa)) / p2)
        time.sleep(0.08)
        o3 = ex.create_market_order(par3, "sell", float(o2.get("filled", qb)))
        lr = float(o3.get("cost", 0)) - res["capital"]
        add_log("ARB REAL " + "%.2fs" % (time.time()-t0) + " | Lucro: $" + "%+.6f" % lr, "success")
        return True, lr
    except ccxt.InsufficientFunds:
        add_log("Saldo insuficiente (minimo recomendado: $10 USDT)", "error")
        return False, 0
    except Exception as e:
        add_log("Erro execucao: " + str(e)[:80], "error")
        return False, 0

# ═════════════════════════════════════════════════════════════════════════════
#  LOOP PRINCIPAL
# =============================================================================
def bot_loop():
    global ex

    modo = "SIMULACAO" if BOT["paper"] else "REAL"
    add_log("Bot iniciado | $" + str(BOT["capital"]) + " | JC " + str(BOT["gatilho_jc"]) + "% | " + modo, "success")

    # 1. Teste de conectividade
    preco = get_preco_btc()
    if preco is None:
        add_log("Falha ao conectar a Binance. Verifica a rede do servidor.", "error")
        BOT["running"] = False
        return
    BOT["btc_preco"] = preco
    add_log("Binance conectada | BTC/USDT = $" + "{:,.2f}".format(preco), "success")

    # 2. Saldo inicial
    if not BOT["paper"]:
        if not BOT["api_key"] or not BOT["api_secret"]:
            add_log("Modo REAL requer chaves API — vai a Config e insere as chaves!", "error")
            BOT["running"] = False
            return
        # Inicializar ccxt para ordens reais
        ex = ccxt.binance({
            "apiKey": BOT["api_key"], "secret": BOT["api_secret"],
            "enableRateLimit": True, "options": {"defaultType": "spot"},
        })
        usdt = get_saldo_usdt(BOT["api_key"], BOT["api_secret"])
        if usdt is not None:
            BOT["saldo_conta"] = usdt
            add_log(
                "Saldo Spot Binance: $" + "%.4f" % usdt + " USDT" +
                (" | ATENCAO: saldo insuficiente para arbs (min $10)" if usdt < 10 else " | OK"),
                "info" if usdt >= 10 else "warn"
            )
        else:
            BOT["saldo_conta"] = 0.0
            add_log("Nao foi possivel ler saldo — verifica as chaves API", "warn")
    else:
        # Simulacao: saldo virtual começa igual ao capital
        BOT["saldo_conta"] = BOT["capital"]
        add_log("Modo simulacao | Saldo virtual: $" + "%.4f" % BOT["saldo_conta"] + " USDT", "info")
        add_log("Os dados de mercado sao reais (orderbook Binance ao vivo)", "info")

    add_log("Scan a iniciar | " + str(len(TRIANGULOS)) + " triangulos | 1 scan a cada 2s", "info")

    ult_saldo = time.time()

    while BOT["running"]:
        try:
            # Drawdown
            if BOT["cap_inicial"] > 0:
                dd = (BOT["cap_inicial"] - BOT["capital"]) / BOT["cap_inicial"] * 100
                BOT["drawdown"] = max(0.0, dd)
                if dd >= BOT["max_dd"]:
                    add_log("DRAWDOWN " + "%.2f" % dd + "% — Bot parado automaticamente!", "error")
                    BOT["running"] = False
                    break

            # Rate limit
            h = datetime.now().hour
            if h != BOT["hora_atual"]:
                BOT["hora_atual"] = h
                BOT["arbs_hora"]  = 0
            if BOT["arbs_hora"] >= 20:
                time.sleep(60)
                continue

            # Sync saldo real cada 2 min
            if not BOT["paper"] and time.time() - ult_saldo > 120:
                usdt = get_saldo_usdt(BOT["api_key"], BOT["api_secret"])
                if usdt is not None:
                    BOT["saldo_conta"] = usdt
                ult_saldo = time.time()

            # ────────────────────────────────────────────
            #  SCAN DOS 20 TRIANGULOS
            # ────────────────────────────────────────────
            BOT["scans"] += 1
            scan_todos    = []
            ops           = []
            erros_ob      = 0

            for tri in TRIANGULOS:
                if not BOT["running"]:
                    break
                tri_key = tri[0] + ">" + tri[1] + ">" + tri[2] + ">" + tri[0]
                em_cd   = (time.time() - BOT["cooldowns"].get(tri_key, 0)) < 30

                res = calcular(tri, BOT["capital"])

                if res is None:
                    erros_ob += 1
                    continue

                if res["pct"] > BOT["melhor"]:
                    BOT["melhor"] = res["pct"]
                scan_todos.append(res)

                if res["ok"] and not em_cd:
                    ops.append(res)
                    BOT["arbs_achadas"] += 1
                elif res["pct"] > 0 and not res["ok"]:
                    BOT["arbs_rejeit"] += 1

            scan_todos.sort(key=lambda x: x["pct"], reverse=True)
            BOT["scan_data"] = scan_todos[:20]

            # Log a cada 5 scans
            if BOT["scans"] % 5 == 0:
                if scan_todos:
                    top = scan_todos[0]
                    add_log(
                        "Scan #" + str(BOT["scans"]) +
                        " | " + str(len(scan_todos)) + "/" + str(len(TRIANGULOS)) + " pares" +
                        " | Melhor: " + top["label"] + " " + ("%+.4f" % top["pct"]) + "%" +
                        " | Validos: " + str(len(ops)),
                        "info"
                    )
                else:
                    add_log(
                        "Scan #" + str(BOT["scans"]) +
                        " | " + str(erros_ob) + " pares sem dados" +
                        " | Verifica conexao Binance",
                        "warn"
                    )

            # ────────────────────────────────────────────
            #  EXECUTAR MELHOR OPORTUNIDADE
            # ────────────────────────────────────────────
            if ops:
                ops.sort(key=lambda x: x["pct"] - x["slip"] * 2, reverse=True)
                melhor = ops[0]

                # Validacao dupla (re-verifica preco)
                partes = melhor["tri"].split(">")
                check  = calcular(partes[:3], melhor["capital"])
                if check and check["ok"] and abs(check["pct"] - melhor["pct"]) < 0.3:
                    ok, lucro = executar_arb(melhor)
                    if ok:
                        registar_lucro(lucro)
                        BOT["arbs_exec"]  += 1
                        BOT["arbs_hora"]  += 1
                        BOT["last_arb"]    = melhor
                        BOT["cooldowns"][melhor["tri"]] = time.time()

            time.sleep(2)

        except Exception as e:
            add_log("Erro loop: " + str(e)[:80], "error")
            print(traceback.format_exc())
            time.sleep(10)

    add_log("Bot parado", "warn")

# ── API Routes ────────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    roi  = (BOT["capital"] / BOT["cap_inicial"] - 1) * 100 if BOT["cap_inicial"] > 0 else 0
    g    = BOT["cap_base"] * BOT["gatilho_jc"] / 100
    prog = min(100, BOT["lucro_ciclo"] / g * 100) if g > 0 else 0
    return jsonify({
        "running":      BOT["running"],
        "paper":        BOT["paper"],
        "capital":      round(BOT["capital"],       6),
        "saldo_conta":  round(BOT["saldo_conta"],   4),
        "lucro_total":  round(BOT["lucro_total"],   6),
        "lucro_ciclo":  round(BOT["lucro_ciclo"],   6),
        "roi":          round(roi,   4),
        "ciclos_jc":    BOT["ciclos_jc"],
        "prog_ciclo":   round(prog,  2),
        "gatilho_usdt": round(g,     6),
        "falta":        round(max(0, g - BOT["lucro_ciclo"]), 6),
        "arbs_exec":    BOT["arbs_exec"],
        "arbs_achadas": BOT["arbs_achadas"],
        "arbs_rejeit":  BOT["arbs_rejeit"],
        "melhor":       round(BOT["melhor"],    4),
        "drawdown":     round(BOT["drawdown"],  2),
        "scans":        BOT["scans"],
        "last_arb":     BOT["last_arb"],
        "marcos":       BOT["marcos"][:5],
        "max_dd":       BOT["max_dd"],
        "gatilho_jc":   BOT["gatilho_jc"],
        "btc_preco":    BOT["btc_preco"],
    })

@app.route("/api/logs")
def api_logs():
    return jsonify(BOT["logs"][:100])

@app.route("/api/scan")
def api_scan():
    return jsonify(BOT["scan_data"][:20])

@app.route("/api/start", methods=["POST"])
def api_start():
    if BOT["running"]:
        return jsonify({"ok": False, "msg": "Ja esta a correr"})
    d = request.json or {}
    with _lock:
        BOT.update({
            "paper":       bool(d.get("paper", True)),
            "capital":     float(d.get("capital",     10.0)),
            "cap_inicial": float(d.get("capital",     10.0)),
            "cap_base":    float(d.get("capital",     10.0)),
            "saldo_conta": float(d.get("saldo_conta", 0.0)),
            "gatilho_jc":  float(d.get("gatilho_jc",  10.0)),
            "lucro_min":   float(d.get("lucro_min",   0.20)),
            "slip_max":    float(d.get("slip_max",    0.05)),
            "max_dd":      float(d.get("max_dd",      10.0)),
            "api_key":     d.get("api_key",    BOT["api_key"]),
            "api_secret":  d.get("api_secret", BOT["api_secret"]),
            "lucro_total": 0.0, "lucro_ciclo": 0.0, "ciclos_jc": 0,
            "arbs_exec": 0, "arbs_achadas": 0, "arbs_rejeit": 0,
            "scans": 0, "melhor": 0.0, "drawdown": 0.0, "arbs_hora": 0,
            "cooldowns": {}, "logs": [], "scan_data": [],
            "last_arb": None, "marcos": [], "running": True,
            "btc_preco": 0.0,
        })
    threading.Thread(target=bot_loop, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    BOT["running"] = False
    return jsonify({"ok": True})

@app.route("/api/config", methods=["POST"])
def api_config():
    d = request.json or {}
    allowed = {"api_key", "api_secret", "paper", "gatilho_jc", "lucro_min", "slip_max", "max_dd"}
    for k, v in d.items():
        if k in allowed:
            BOT[k] = v
    return jsonify({"ok": True})

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>ARB Bot</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap');
:root{--bg:#060910;--s1:#0b0e1a;--s2:#0f1422;--s3:#141c2e;--s4:#1a2438;--b:#1f2e48;--b2:#283a58;--cy:#00ccff;--gr:#00e09a;--grdk:#00a870;--gd:#f0bc10;--rd:#ff3868;--or:#ff7a18;--sk:#3db0ff;--tx:#d5e8f8;--t2:#748aaa;--mu:#2c3e58;--mu2:#445e80}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;background:var(--bg)}
body{font-family:'Syne',sans-serif;color:var(--tx);font-size:14px;display:flex;flex-direction:column;max-width:480px;margin:0 auto}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;background:radial-gradient(ellipse 70% 40% at 50% -5%,#00ccff10 0%,transparent 70%),linear-gradient(var(--b)10 1px,transparent 1px),linear-gradient(90deg,var(--b)10 1px,transparent 1px);background-size:100%,42px 42px,42px 42px}
#app{position:relative;z-index:1;display:flex;flex-direction:column;height:100vh;width:100%}
#top{flex-shrink:0;height:52px;background:var(--s1)ee;backdrop-filter:blur(24px);border-bottom:1px solid var(--b);padding:0 16px;display:flex;align-items:center;justify-content:space-between}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:18px;letter-spacing:2px;background:linear-gradient(135deg,var(--cy),var(--gr));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.logo small{font-size:10px;-webkit-text-fill-color:var(--mu2);color:var(--mu2);margin-left:4px;font-weight:400}
#tr{display:flex;align-items:center;gap:8px}
#ts{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:var(--cy);opacity:0;transition:opacity .3s}
.pill{display:flex;align-items:center;gap:5px;font-size:10px;font-weight:700;letter-spacing:1px;padding:5px 11px;border-radius:20px;transition:all .3s}
.poff{border:1px solid var(--mu)50;background:var(--mu)15;color:var(--mu2)}
.pon{border:1px solid var(--gr)55;background:var(--gr)12;color:var(--gr)}
.pd{width:6px;height:6px;border-radius:50%;transition:all .3s}
.pdoff{background:var(--mu2)}
.pdon{background:var(--gr);box-shadow:0 0 8px var(--gr);animation:bk 1.4s infinite}
@keyframes bk{0%,100%{opacity:1}50%{opacity:.35}}
#sc{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch}
#sc::-webkit-scrollbar{width:2px}
#sc::-webkit-scrollbar-thumb{background:var(--b2)}
.pg{display:none;padding:14px 14px 16px;animation:fu .2s ease}
.pg.show{display:block}
@keyframes fu{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}
#nav{flex-shrink:0;background:var(--s1)f0;backdrop-filter:blur(24px);border-top:1px solid var(--b);display:grid;grid-template-columns:repeat(4,1fr);height:56px;position:relative;z-index:50}
.nb{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;background:none;border:none;cursor:pointer;outline:none;color:var(--mu2);transition:color .2s;border-top:2px solid transparent;padding:6px 4px 4px}
.nb svg{width:20px;height:20px;stroke-width:1.8;fill:none;stroke:currentColor}
.nbl{font-size:8px;font-weight:700;letter-spacing:1px;text-transform:uppercase;font-family:'Syne',sans-serif}
.nb.act{color:var(--cy);border-top-color:var(--cy)}
.nb.act svg{stroke:var(--cy)}
.tag{display:inline-flex;align-items:center;gap:4px;font-size:9px;font-weight:700;letter-spacing:1px;text-transform:uppercase;padding:3px 8px;border-radius:20px;white-space:nowrap}
.tcy{background:#00ccff14;color:var(--cy);border:1px solid #00ccff30}
.tgr{background:#00e09a14;color:var(--gr);border:1px solid #00e09a30}
.tgd{background:#f0bc1014;color:var(--gd);border:1px solid #f0bc1030}
.trd{background:#ff386814;color:var(--rd);border:1px solid #ff386830}
.tor{background:#ff7a1814;color:var(--or);border:1px solid #ff7a1830}
#ds{display:grid;grid-template-columns:1fr 1fr;gap:0;background:var(--b);border-radius:14px;overflow:hidden;border:1px solid var(--b);margin-bottom:10px}
.scc{padding:14px;transition:background .4s}
.scc:first-child{background:var(--s2);border-right:1px solid var(--b)}
.scc:last-child{background:var(--s1)}
.slb{font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--mu2);margin-bottom:5px;display:flex;align-items:center;gap:4px}
.svl{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:17px;line-height:1.15;transition:text-shadow .4s,color .3s}
.ssb{font-size:9px;color:var(--mu2);margin-top:3px}
.ld{width:5px;height:5px;border-radius:50%;background:var(--cy);animation:bk 1.5s infinite;display:none}
#bm{width:100%;padding:14px;border-radius:11px;border:none;cursor:pointer;font-family:'Syne',sans-serif;font-weight:800;font-size:14px;transition:all .2s;outline:none;margin-bottom:10px}
#bm:active{transform:scale(.98)}
.bgo{background:linear-gradient(135deg,var(--gr),var(--grdk));color:#000;box-shadow:0 4px 24px #00e09a30}
.bst{background:linear-gradient(135deg,var(--rd),#cc0040);color:#fff;box-shadow:0 4px 24px #ff386830}
#ag{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px}
.ab{border-radius:11px;padding:13px 10px;text-align:center}
.abm{background:linear-gradient(135deg,#00e09a14,var(--s3));border:1px solid #00e09a32}
.abs{background:var(--s3);border:1px solid var(--b)}
.an{font-family:'JetBrains Mono',monospace;font-weight:800;line-height:1;transition:text-shadow .4s}
.al{font-size:8px;letter-spacing:1px;text-transform:uppercase;margin-top:5px;font-weight:700}
.m2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
.mc{background:var(--s2);border:1px solid var(--b);border-radius:11px;padding:13px 14px}
.mv{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:18px;line-height:1.15}
.ml{font-size:9px;color:var(--mu2);letter-spacing:1.2px;text-transform:uppercase;margin-top:4px}
#jc{background:linear-gradient(135deg,#f0bc1009,var(--s2));border:1px solid #f0bc1030;border-radius:12px;padding:16px;margin-bottom:10px}
#jcr{display:flex;align-items:center;gap:14px;margin-bottom:12px}
.ji{font-size:12px;color:var(--t2);line-height:2.1;flex:1}
.jv{display:flex;justify-content:space-between;align-items:center}
.jv strong{font-family:'JetBrains Mono',monospace;font-size:12px}
#pb{height:6px;background:var(--s4);border-radius:3px;overflow:hidden;margin-bottom:3px}
#pf{height:100%;border-radius:3px;background:linear-gradient(90deg,var(--gd),var(--or));transition:width .6s cubic-bezier(.4,0,.2,1)}
.pe{display:flex;justify-content:space-between;margin-top:2px}
.pe span{font-size:9px;font-family:'JetBrains Mono',monospace}
#la{border-radius:12px;padding:14px 16px;margin-bottom:10px;display:none;border:1px solid #00e09a28;background:linear-gradient(135deg,#00e09a0c,var(--s2));transition:border-color .4s}
#la.fl{border-color:#00e09a70}
.lah{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.lap{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:var(--tx);margin-bottom:9px;word-break:break-all}
.lag{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.lab{background:var(--s3);border-radius:8px;padding:8px;text-align:center}
.lav{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700}
.lal{font-size:8px;color:var(--mu2);margin-top:2px;text-transform:uppercase;letter-spacing:1px}
#stbl{background:var(--s1);border:1px solid var(--b);border-radius:12px;overflow:hidden;margin-bottom:10px}
.sth{display:grid;grid-template-columns:1fr 68px 58px 30px;padding:9px 12px;background:var(--s3);border-bottom:1px solid var(--b)}
.sth span{font-size:8px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--mu2)}
.sth span:not(:first-child){text-align:right}
.str{display:grid;grid-template-columns:1fr 68px 58px 30px;padding:9px 12px;border-bottom:1px solid var(--b)15;align-items:center}
.str:last-child{border:none}
.strok{background:#00e09a06}
.stc{font-family:'JetBrains Mono',monospace;font-size:10.5px}
.stc:not(:first-child){text-align:right}
#ll{background:var(--s1);border:1px solid var(--b);border-radius:12px;max-height:calc(100vh - 180px);overflow-y:auto}
#ll::-webkit-scrollbar{width:2px}
#ll::-webkit-scrollbar-thumb{background:var(--b2)}
.lr{padding:7px 13px;border-bottom:1px solid var(--b)18;font-family:'JetBrains Mono',monospace;font-size:10.5px;line-height:1.5}
.lr:last-child{border:none}
.lt{color:var(--mu2);margin-right:7px}
.cs{background:var(--s2);border:1px solid var(--b);border-radius:12px;padding:16px;margin-bottom:10px}
.fl{font-size:9px;color:var(--mu2);letter-spacing:1.2px;text-transform:uppercase;margin-bottom:6px;display:block}
.inp{background:var(--s4);border:1px solid var(--b2);border-radius:8px;padding:11px 14px;color:var(--tx);font-size:14px;width:100%;outline:none;font-family:'JetBrains Mono',monospace;font-weight:700;transition:border-color .2s}
.inp:focus{border-color:var(--cy)60}
.fr{margin-bottom:12px}
.ig{display:flex;gap:8px;align-items:center}
.qb{padding:7px 12px;border-radius:8px;cursor:pointer;font-weight:700;font-size:11px;font-family:'JetBrains Mono',monospace;background:var(--s4);border:1px solid var(--b);color:var(--mu2);transition:all .15s;flex-shrink:0}
.trow{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid var(--b)25}
.trow:last-child{border:none}
.trow h4{font-size:13px;font-weight:700;margin-bottom:2px}
.trow p{font-size:11px;color:var(--mu2)}
.tgl{width:48px;height:27px;border-radius:14px;position:relative;cursor:pointer;border:none;outline:none;flex-shrink:0;transition:background .25s}
.tk{position:absolute;top:3px;width:21px;height:21px;border-radius:50%;background:#fff;transition:left .25s;box-shadow:0 1px 6px rgba(0,0,0,.5)}
.sh{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.sh h2{font-size:16px;font-weight:800}
.mini{background:var(--s3);border:1px solid var(--b);color:var(--t2);padding:6px 12px;border-radius:7px;font-size:10px;font-weight:700;cursor:pointer;font-family:'Syne',sans-serif}
.bsv{width:100%;padding:14px;border-radius:11px;border:none;cursor:pointer;font-family:'Syne',sans-serif;font-weight:800;font-size:14px;background:linear-gradient(135deg,var(--gr),var(--grdk));color:#000;box-shadow:0 4px 20px #00e09a25;margin-bottom:10px}
.bsv:active{transform:scale(.98)}
.ib{background:#00ccff08;border:1px solid #00ccff25;border-radius:9px;padding:10px 13px;font-size:12px;color:var(--t2);margin-top:8px;line-height:1.6}
.wb{background:#ff386810;border:1px solid #ff386830;border-radius:9px;padding:10px 13px;font-size:12px;color:var(--rd);margin-top:10px;line-height:1.6}
#ml{text-align:center;padding:6px 0 2px}
.emp{padding:44px 16px;text-align:center;color:var(--mu2)}
.eic{font-size:40px;margin-bottom:10px}
.eh{font-size:14px;font-weight:700;color:var(--t2);margin-bottom:6px}
.ep{font-size:12px;line-height:1.6}
.agd{background:#f0bc1018!important;border-color:#f0bc1050!important;color:var(--gd)!important}
.aor{background:#ff7a1818!important;border-color:#ff7a1850!important;color:var(--or)!important}
.ard{background:#ff386818!important;border-color:#ff386850!important;color:var(--rd)!important}
</style>
</head>
<body>
<div id="app">
<div id="top">
  <div class="logo">ARB <span style="-webkit-text-fill-color:var(--cy)">&#9651;</span><small>TRIANGULAR</small></div>
  <div id="tr">
    <span id="ts"></span>
    <div class="pill poff" id="pill"><div class="pd pdoff" id="pd"></div><span id="pt">OFFLINE</span></div>
  </div>
</div>
<div id="sc">

<!-- DASHBOARD -->
<div class="pg show" id="pgDash">
  <div id="ds">
    <div class="scc" id="sca">
      <div class="slb"><span class="ld" id="ldt"></span>Conta Binance<span class="tag tcy" id="ltg" style="display:none;font-size:8px">LIVE</span></div>
      <div class="svl" id="vc" style="color:var(--cy)">&#8212;</div>
      <div class="ssb" id="subc">aguardando</div>
    </div>
    <div class="scc" id="scb">
      <div class="slb"><span id="bd" style="width:5px;height:5px;border-radius:50%;background:var(--mu2);display:inline-block;transition:all .3s"></span>Capital Bot</div>
      <div class="svl" id="vb" style="color:var(--gr)">&#8212;</div>
      <div class="ssb" id="subb">apos reinvestimento</div>
    </div>
  </div>
  <button id="bm" class="bgo" onclick="tBot()">&#9654;&#160; Iniciar Bot</button>
  <div id="ag">
    <div class="ab abm"><div class="an" id="nE" style="font-size:36px;color:var(--gr)">0</div><div class="al" style="color:var(--gr)">Executadas</div></div>
    <div class="ab abs"><div class="an" id="nA" style="font-size:24px;color:var(--sk)">0</div><div class="al" style="color:var(--mu2)">Achadas</div></div>
    <div class="ab abs"><div class="an" id="nR" style="font-size:24px;color:var(--mu2)">0</div><div class="al" style="color:var(--mu2)">Rejeitadas</div></div>
  </div>
  <div class="m2">
    <div class="mc" style="border-top:2px solid var(--gr)"><div class="mv" id="mL" style="color:var(--gr)">+$0.000000</div><div class="ml">Lucro Total</div></div>
    <div class="mc" style="border-top:2px solid var(--gd)"><div class="mv" id="mS" style="color:var(--gd)">0.0000%</div><div class="ml">Melhor Spread</div></div>
    <div class="mc" style="border-top:2px solid var(--gd)"><div class="mv" id="mC" style="color:var(--gd)">0</div><div class="ml">Ciclos JC</div></div>
    <div class="mc" style="border-top:2px solid var(--mu2)"><div class="mv" id="mD" style="color:var(--mu2)">0.00%</div><div class="ml">Drawdown</div></div>
  </div>
  <div id="jc">
    <div id="jcr">
      <div style="position:relative;width:82px;height:82px;flex-shrink:0">
        <svg width="82" height="82" style="transform:rotate(-90deg)">
          <circle cx="41" cy="41" r="34" fill="none" stroke="var(--s4)" stroke-width="7"/>
          <circle id="rc" cx="41" cy="41" r="34" fill="none" stroke="var(--gd)" stroke-width="7" stroke-dasharray="0 214" stroke-linecap="round" style="transition:stroke-dasharray .5s cubic-bezier(.4,0,.2,1)"/>
        </svg>
        <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;text-align:center">
          <div id="rp" style="font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:700;color:var(--gd)">0%</div>
          <div style="font-size:7px;color:var(--mu2);letter-spacing:1px">CICLO</div>
        </div>
      </div>
      <div style="flex:1">
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
          <span class="tag tgd">&#128260; Juros Compostos</span>
          <span class="tag tgr" id="jct" style="font-size:8px">#0</span>
        </div>
        <div class="ji">
          <div class="jv"><span>Lucro ciclo</span><strong id="jL" style="color:var(--gd)">$0.000000</strong></div>
          <div class="jv"><span>Gatilho</span><strong id="jG">$0.0000</strong></div>
          <div class="jv"><span>Falta</span><strong id="jF" style="color:var(--t2)">$0.000000</strong></div>
        </div>
      </div>
    </div>
    <div id="pb"><div id="pf" style="width:0%"></div></div>
    <div class="pe"><span style="color:var(--mu)">$0</span><span id="pp" style="color:var(--gd);font-weight:700">0%</span><span id="pm" style="color:var(--mu)">$0</span></div>
  </div>
  <div id="la">
    <div class="lah"><span class="tag tgr" id="lat">Arb #0</span><span id="lal" style="font-family:'JetBrains Mono',monospace;color:var(--gr);font-weight:700;font-size:14px">+$0</span></div>
    <div class="lap" id="lap">&#8212;</div>
    <div class="lag">
      <div class="lab"><div class="lav" id="laspd" style="color:var(--gd)">0%</div><div class="lal">Spread</div></div>
      <div class="lab"><div class="lav" id="laslp" style="color:var(--or)">0%</div><div class="lal">Slippage</div></div>
      <div class="lab"><div class="lav" id="laliq" style="color:var(--sk)">$0</div><div class="lal">Liquidez</div></div>
    </div>
  </div>
  <div id="ml"></div>
</div>

<!-- SCAN -->
<div class="pg" id="pgScan">
  <div class="sh"><h2>Scan ao Vivo</h2>
    <div style="display:flex;gap:8px;align-items:center">
      <span id="scc" style="font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--mu2)">0 scans</span>
      <span class="tag tgr" id="slv" style="display:none;font-size:8px">&#9679; LIVE</span>
    </div>
  </div>
  <div id="stbl">
    <div class="sth"><span>Triangulo</span><span>Spread</span><span>Slip</span><span></span></div>
    <div id="sb"><div class="emp"><div class="eic">&#9651;</div><div class="eh">Bot parado</div><div class="ep">Inicia o bot para ver os triangulos em tempo real.</div></div></div>
  </div>
</div>

<!-- LOGS -->
<div class="pg" id="pgLogs">
  <div class="sh"><h2>Registo</h2><button class="mini" onclick="document.getElementById('ll').innerHTML='<div class=emp><div class=eic>&#8801;</div><div class=eh>Limpo</div></div>'">Limpar</button></div>
  <div id="ll"><div class="emp"><div class="eic">&#8801;</div><div class="eh">Sem registos</div></div></div>
</div>

<!-- CONFIG -->
<div class="pg" id="pgConfig">
  <div class="sh"><h2>Configuracoes</h2></div>
  <div class="cs">
    <div style="margin-bottom:14px"><span class="tag tcy">Chaves API Binance</span></div>
    <div class="fr"><label class="fl">API Key</label><input class="inp" type="text" id="ck" placeholder="Cole a tua API Key" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false"></div>
    <div class="fr"><label class="fl">API Secret</label><input class="inp" type="password" id="cs" placeholder="Cole o teu Secret Key" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false">
    <button onclick="var e=document.getElementById('cs');e.type=e.type=='password'?'text':'password'" style="margin-top:6px;background:var(--s4);border:1px solid var(--b);color:var(--mu2);padding:5px 10px;border-radius:6px;font-size:10px;cursor:pointer">Mostrar / Ocultar</button></div>
    <div class="ib">Na Binance activa <strong style="color:var(--gr)">Spot Trading</strong> apenas &#8212; <strong style="color:var(--rd)">NUNCA Withdrawals</strong><br>O saldo deve estar em <strong style="color:var(--cy)">USDT na Conta a Vista (Spot)</strong></div>
  </div>
  <div class="cs">
    <div style="margin-bottom:14px"><span class="tag tcy">Capital</span></div>
    <div class="fr"><label class="fl">Capital operacional do bot (USDT)</label>
      <div class="ig">
        <button class="qb" onclick="document.getElementById('cc').value=10;upG()" style="background:#00ccff10;border-color:#00ccff30;color:var(--cy)">MIN<br><small>$10</small></button>
        <input class="inp" type="number" id="cc" value="10" min="1" step="1" oninput="upG()" style="flex:1;text-align:right;font-size:16px">
        <button class="qb" onclick="document.getElementById('cc').value=document.getElementById('cco').value;upG()" style="background:#00e09a10;border-color:#00e09a30;color:var(--gr)">MAX<br><small id="mx">$?</small></button>
      </div>
    </div>
    <div class="fr"><label class="fl">Saldo total da conta Binance (USDT)</label><input class="inp" type="number" id="cco" value="0.93" min="0" step="0.01" oninput="document.getElementById('mx').textContent='$'+this.value;upG()"></div>
  </div>
  <div class="cs">
    <div style="margin-bottom:14px"><span class="tag tgr">Modo de Operacao</span></div>
    <div class="trow">
      <div><h4>Simulacao (Paper Trading)</h4><p>Orderbook real Binance &#183; sem ordens reais</p></div>
      <button class="tgl" id="tp" onclick="tgP()" style="background:var(--gr)"><div class="tk" id="tpk" style="left:24px"></div></button>
    </div>
    <div id="wr" style="display:none" class="wb">MODO REAL activo. Com $0.93 USDT o bot nao consegue executar arbs &#8212; minimo recomendado: $10 USDT na Conta Spot.</div>
  </div>
  <div class="cs">
    <div style="margin-bottom:14px"><span class="tag tgd">Juros Compostos</span></div>
    <div class="fr"><label class="fl">Gatilho de reinvestimento</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px" id="gb">
        <button class="qb" onclick="sG(this,5)">5%</button>
        <button class="qb agd" onclick="sG(this,10)">10%</button>
        <button class="qb" onclick="sG(this,15)">15%</button>
        <button class="qb" onclick="sG(this,20)">20%</button>
        <button class="qb" onclick="sG(this,25)">25%</button>
      </div>
      <div class="ib" id="gi">Reinveste quando lucrar <strong style="color:var(--gd);font-family:'JetBrains Mono',monospace">$1.0000 USDT</strong></div>
    </div>
  </div>
  <div class="cs">
    <div style="margin-bottom:14px"><span class="tag tor">Parametros</span></div>
    <div class="fr"><label class="fl">Lucro minimo por arb</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap" id="lb">
        <button class="qb" onclick="sL(this,0.10)">0.10%</button>
        <button class="qb" onclick="sL(this,0.15)">0.15%</button>
        <button class="qb aor" onclick="sL(this,0.20)">0.20%</button>
        <button class="qb" onclick="sL(this,0.30)">0.30%</button>
        <button class="qb" onclick="sL(this,0.50)">0.50%</button>
      </div>
    </div>
    <div class="fr" style="margin-top:10px"><label class="fl">Max Drawdown</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap" id="db">
        <button class="qb" onclick="sD(this,5)">5%</button>
        <button class="qb ard" onclick="sD(this,10)">10%</button>
        <button class="qb" onclick="sD(this,15)">15%</button>
        <button class="qb" onclick="sD(this,20)">20%</button>
        <button class="qb" onclick="sD(this,30)">30%</button>
      </div>
    </div>
  </div>
  <button class="bsv" onclick="svC()">Guardar e Aplicar</button>
  <div style="height:16px"></div>
</div>

</div>
<nav id="nav">
  <button class="nb act" id="nD" onclick="gP('Dash',this)">
    <svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
    <span class="nbl">Dashboard</span>
  </button>
  <button class="nb" id="nS" onclick="gP('Scan',this)">
    <svg viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
    <span class="nbl">Scan</span>
  </button>
  <button class="nb" id="nL" onclick="gP('Logs',this)">
    <svg viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="3" cy="6" r="1" fill="currentColor"/><circle cx="3" cy="12" r="1" fill="currentColor"/><circle cx="3" cy="18" r="1" fill="currentColor"/></svg>
    <span class="nbl">Logs</span>
  </button>
  <button class="nb" id="nC" onclick="gP('Config',this)">
    <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    <span class="nbl">Config</span>
  </button>
</nav>
</div>
<script>
const S={paper:true,g:10,l:0.20,d:10,pE:0,pC:0};
function gP(id,btn){document.querySelectorAll('.pg').forEach(p=>p.classList.remove('show'));document.querySelectorAll('.nb').forEach(b=>b.classList.remove('act'));document.getElementById('pg'+id).classList.add('show');btn.classList.add('act');document.getElementById('sc').scrollTop=0}
function upG(){const c=parseFloat(document.getElementById('cc').value)||10;const g=(c*S.g/100).toFixed(4);document.getElementById('gi').innerHTML='Reinveste quando lucrar <strong style="color:var(--gd);font-family:\'JetBrains Mono\',monospace">$'+g+' USDT</strong>'}
function tgP(){S.paper=!S.paper;document.getElementById('tp').style.background=S.paper?'var(--gr)':'var(--mu)';document.getElementById('tpk').style.left=S.paper?'24px':'3px';document.getElementById('wr').style.display=S.paper?'none':'block'}
function sG(b,v){S.g=v;document.querySelectorAll('#gb .qb').forEach(x=>x.classList.remove('agd'));b.classList.add('agd');upG()}
function sL(b,v){S.l=v;document.querySelectorAll('#lb .qb').forEach(x=>x.classList.remove('aor'));b.classList.add('aor')}
function sD(b,v){S.d=v;document.querySelectorAll('#db .qb').forEach(x=>x.classList.remove('ard'));b.classList.add('ard')}
function svC(){fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:document.getElementById('ck').value.trim(),api_secret:document.getElementById('cs').value.trim(),paper:S.paper,gatilho_jc:S.g,lucro_min:S.l,max_dd:S.d})});gP('Dash',document.getElementById('nD'))}
function tBot(){
  if(window._run){fetch('/api/stop',{method:'POST'});}
  else{fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paper:S.paper,capital:parseFloat(document.getElementById('cc').value)||10,saldo_conta:parseFloat(document.getElementById('cco').value)||0,gatilho_jc:S.g,lucro_min:S.l,slip_max:0.05,max_dd:S.d,api_key:document.getElementById('ck').value.trim(),api_secret:document.getElementById('cs').value.trim()})});}
}
window._run=false;
function poll(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    window._run=d.running;
    const bm=document.getElementById('bm'),pill=document.getElementById('pill'),pd=document.getElementById('pd');
    bm.innerHTML=d.running?'&#9632;&#160; Parar Bot':'&#9654;&#160; Iniciar Bot';
    bm.className=d.running?'bst':'bgo';
    pill.className=d.running?'pill pon':'pill poff';
    pd.className=d.running?'pd pdon':'pd pdoff';
    document.getElementById('pt').textContent=d.running?'ONLINE':'OFFLINE';
    const ts=document.getElementById('ts');ts.textContent=d.running?'$'+d.saldo_conta.toFixed(4):'';ts.style.opacity=d.running?'1':'0';
    document.getElementById('ldt').style.display=d.running?'inline-block':'none';
    document.getElementById('ltg').style.display=d.running?'inline-flex':'none';
    document.getElementById('slv').style.display=d.running?'inline-flex':'none';
    document.getElementById('vc').textContent=d.running?'$'+d.saldo_conta.toFixed(4):'&#8212;';
    document.getElementById('subc').textContent=d.paper?'simulado \xb7 arb virtual':'saldo real Spot Binance';
    document.getElementById('vb').textContent=d.running?'$'+d.capital.toFixed(6):'&#8212;';
    document.getElementById('vb').style.color=d.roi>=0?'var(--gr)':'var(--rd)';
    document.getElementById('subb').textContent=d.running?Math.abs(d.roi).toFixed(4)+'% ROI':'apos reinvestimento';
    if(d.arbs_exec>S.pE){S.pE=d.arbs_exec;const c=document.getElementById('sca'),n=document.getElementById('nE');c.style.background='linear-gradient(135deg,#00ccff14,var(--s2))';n.style.textShadow='0 0 20px var(--gr)';document.getElementById('la').classList.add('fl');setTimeout(()=>{c.style.background='';n.style.textShadow='';document.getElementById('la').classList.remove('fl')},700)}
    if(d.ciclos_jc>S.pC){S.pC=d.ciclos_jc;const bc=document.getElementById('scb'),vb=document.getElementById('vb'),bd=document.getElementById('bd');bc.style.background='linear-gradient(135deg,#00e09a14,var(--s1))';vb.style.textShadow='0 0 20px var(--gr)';bd.style.background='var(--gr)';bd.style.boxShadow='0 0 8px var(--gr)';setTimeout(()=>{bc.style.background='';vb.style.textShadow='';bd.style.background='var(--mu2)';bd.style.boxShadow=''},1400)}
    document.getElementById('nE').textContent=d.arbs_exec;
    document.getElementById('nA').textContent=d.arbs_achadas;
    document.getElementById('nR').textContent=d.arbs_rejeit;
    document.getElementById('mL').textContent='+$'+d.lucro_total.toFixed(6);
    document.getElementById('mS').textContent=d.melhor.toFixed(4)+'%';
    document.getElementById('mC').textContent=d.ciclos_jc;
    const dd=document.getElementById('mD');dd.textContent=d.drawdown.toFixed(2)+'%';dd.style.color=d.drawdown>d.max_dd*.7?'var(--rd)':d.drawdown>d.max_dd*.4?'var(--or)':'var(--mu2)';
    const circ=2*Math.PI*34,prog=d.prog_ciclo;
    document.getElementById('rc').setAttribute('stroke-dasharray',`${circ*prog/100} ${circ}`);
    document.getElementById('rp').textContent=prog.toFixed(0)+'%';
    document.getElementById('jct').textContent='#'+d.ciclos_jc;
    document.getElementById('pf').style.width=prog+'%';
    document.getElementById('pp').textContent=prog.toFixed(1)+'%';
    document.getElementById('pm').textContent='$'+d.gatilho_usdt.toFixed(4);
    document.getElementById('jL').textContent='$'+d.lucro_ciclo.toFixed(6);
    document.getElementById('jG').textContent='$'+d.gatilho_usdt.toFixed(4);
    const jf=document.getElementById('jF');jf.textContent='$'+d.falta.toFixed(6);jf.style.color=d.falta<d.gatilho_usdt*.15?'var(--gr)':'var(--t2)';
    if(d.last_arb){const la=d.last_arb;document.getElementById('la').style.display='block';document.getElementById('lat').textContent='Arb #'+d.arbs_exec;document.getElementById('lal').textContent='+$'+la.lucro.toFixed(6);document.getElementById('lap').textContent=la.tri.replace(/>/g,' > ');document.getElementById('laspd').textContent=la.pct.toFixed(4)+'%';document.getElementById('laslp').textContent=(la.slip||0).toFixed(4)+'%';document.getElementById('laliq').textContent='$'+Math.floor(la.lmin)}
    const mlEl=document.getElementById('ml');mlEl.innerHTML=d.running?(d.paper?'<span class="tag tor">Simulacao \xb7 Orderbook Real Binance</span>':'<span class="tag trd">Modo Real \xb7 Ordens Reais Binance</span>'):'';
    document.getElementById('scc').textContent=d.scans+' scans';
  }).catch(()=>{});
  fetch('/api/scan').then(r=>r.json()).then(data=>{
    if(!data||!data.length)return;
    const html=data.map(r=>{const col=r.pct>=0.20?'var(--gr)':r.pct>0?'var(--gd)':'var(--rd)';const lbl=(r.label||'').replace(/>/g,' ');const ok=r.ok?'<span style="color:var(--gr);font-weight:800;font-size:14px">&#10003;</span>':'<span style="color:var(--mu)">&#8722;</span>';return '<div class="str '+(r.ok?'strok':'')+'"><span class="stc" style="color:var(--tx)">'+lbl+'</span><span class="stc" style="color:'+col+';font-weight:700">'+(r.pct>=0?'+':'')+r.pct.toFixed(3)+'%</span><span class="stc" style="color:var(--mu2)">'+(r.slip||0).toFixed(3)+'%</span><span class="stc">'+ok+'</span></div>'}).join('');
    document.getElementById('sb').innerHTML=html;
  }).catch(()=>{});
  fetch('/api/logs').then(r=>r.json()).then(logs=>{
    if(!logs||!logs.length)return;
    const cols={success:'var(--gr)',compound:'var(--gd)',warn:'var(--or)',error:'var(--rd)',info:'var(--mu2)'};
    const bgs={success:'#00e09a05',compound:'#f0bc1008',error:'#ff386805'};
    const html=logs.map(l=>'<div class="lr" style="color:'+(cols[l.t]||cols.info)+';background:'+(bgs[l.t]||'transparent')+'"><span class="lt">'+l.ts+'</span>'+l.msg+'</div>').join('');
    document.getElementById('ll').innerHTML=html||'<div class="emp"><div class="eic">&#8801;</div><div class="eh">Sem registos</div></div>';
  }).catch(()=>{});
}
upG();poll();setInterval(poll,2000);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return DASHBOARD

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
