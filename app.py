"""
Bot Arbitragem Triangular — Servidor Web Completo
Abre no browser: http://localhost:5000
No Render.com: deploy automático, sem configuração
"""
from flask import Flask, jsonify, request, render_template_string
import ccxt, threading, time, os, json, logging
from datetime import datetime
from collections import defaultdict

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ═══════════════════════════════════════════════════
#  ESTADO GLOBAL DO BOT
# ═══════════════════════════════════════════════════
BOT = {
    "running":      False,
    "paper":        True,
    "capital":      10.0,
    "cap_inicial":  10.0,
    "cap_base":     10.0,
    "saldo_conta":  10.0,
    "lucro_total":  0.0,
    "lucro_ciclo":  0.0,
    "ciclos_jc":    0,
    "arbs_exec":    0,
    "arbs_achadas": 0,
    "arbs_rejeit":  0,
    "scans":        0,
    "melhor":       0.0,
    "drawdown":     0.0,
    "cooldowns":    {},
    "blacklist":    {},
    "erros":        defaultdict(int),
    "logs":         [],
    "scan_data":    [],
    "last_arb":     None,
    "marcos":       [],
    # Config
    "gatilho_jc":   10.0,
    "lucro_min":    0.20,
    "slip_max":     0.05,
    "liq_min":      500,
    "max_dd":       10.0,
    "api_key":      os.getenv("BINANCE_API_KEY", ""),
    "api_secret":   os.getenv("BINANCE_API_SECRET", ""),
}

TRIANGULOS = [
    ["USDT","BTC","ETH"],["USDT","BTC","BNB"],["USDT","ETH","BNB"],
    ["USDT","BTC","SOL"],["USDT","ETH","SOL"],["USDT","BNB","SOL"],
    ["USDT","BTC","XRP"],["USDT","ETH","XRP"],["USDT","BTC","ADA"],
    ["USDT","BTC","DOGE"],["USDT","ETH","DOGE"],["USDT","BNB","XRP"],
    ["USDT","BTC","AVAX"],["USDT","ETH","AVAX"],["USDT","BTC","LINK"],
    ["USDT","ETH","LINK"],["USDT","BTC","MATIC"],["USDT","ETH","MATIC"],
    ["USDT","BNB","MATIC"],["USDT","BTC","DOT"],
]

ex = None
bot_thread = None
TAXA = 0.00075

def add_log(msg, tipo="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    BOT["logs"].insert(0, {"ts": ts, "msg": msg, "tipo": tipo})
    BOT["logs"] = BOT["logs"][:150]
    print(f"{ts} | {msg}")

# ═══════════════════════════════════════════════════
#  ARBITRAGEM
# ═══════════════════════════════════════════════════
def preco_ob(par, lado, usdt):
    try:
        ob = ex.fetch_order_book(par, limit=10)
        niveis = ob["asks"] if lado == "c" else ob["bids"]
        if not niveis: return None, None, None
        best = niveis[0][0]
        liq  = sum(float(p)*float(q) for p,q in niveis)
        if liq < BOT["liq_min"]: return None, None, liq
        acum = custo = 0.0
        for p, q in niveis:
            p, q = float(p), float(q)
            v = p * q
            if acum + v >= usdt:
                custo += usdt - acum; acum = usdt; break
            custo += v; acum += v
        if acum < usdt * 0.99: return None, None, liq
        medio = custo / (acum / best)
        slip  = abs(medio - best) / best * 100
        return medio, slip, liq
    except Exception as e:
        return None, None, None

def calcular(tri, capital):
    base, A, B = tri
    try:
        p1, s1, l1 = preco_ob(f"{A}/{base}", "c", capital)
        if p1 is None: return None
        qa = (capital / p1) * (1 - TAXA)

        p2, s2, l2 = preco_ob(f"{B}/{A}", "c", qa * p1 * (1 - TAXA))
        if p2 is None: return None
        qb = (qa / p2) * (1 - TAXA)

        p3, s3, l3 = preco_ob(f"{B}/{base}", "v", qb * p2 * p1 * (1 - TAXA))
        if p3 is None: return None
        final = qb * p3 * (1 - TAXA)

        lucro = final - capital
        pct   = lucro / capital * 100
        slip  = (s1 or 0) + (s2 or 0) + (s3 or 0)
        lmin  = min(l1 or 0, l2 or 0, l3 or 0)
        return {
            "tri":     f"{base}→{A}→{B}→{base}",
            "pares":   [f"{A}/{base}", f"{B}/{A}", f"{B}/{base}"],
            "precos":  [p1, p2, p3],
            "qtds":    [qa, qb, final],
            "capital": capital,
            "lucro":   lucro,
            "pct":     round(pct, 6),
            "slip":    round(slip, 6),
            "lmin":    round(lmin, 2),
            "ok":      pct >= BOT["lucro_min"] and slip <= BOT["slip_max"] and lmin >= BOT["liq_min"],
        }
    except:
        return None

def registar_lucro(lucro):
    BOT["capital"]      += lucro
    BOT["lucro_total"]  += lucro
    BOT["lucro_ciclo"]  += lucro
    BOT["saldo_conta"]  += lucro
    gatilho = BOT["cap_base"] * (BOT["gatilho_jc"] / 100)
    if BOT["lucro_ciclo"] >= gatilho:
        juros_compostos()

def juros_compostos():
    antes  = BOT["cap_base"]
    depois = BOT["capital"]
    lucro  = BOT["lucro_ciclo"]
    ganho  = lucro / antes * 100
    BOT["ciclos_jc"]  += 1
    BOT["cap_base"]    = depois
    BOT["lucro_ciclo"] = 0.0
    BOT["marcos"].insert(0, {
        "ciclo":  BOT["ciclos_jc"],
        "antes":  round(antes, 6),
        "depois": round(depois, 6),
        "lucro":  round(lucro, 6),
        "ganho":  round(ganho, 4),
        "data":   datetime.now().strftime("%d/%m %H:%M"),
    })
    BOT["marcos"] = BOT["marcos"][:20]
    add_log(f"🔁 JUROS COMPOSTOS #{BOT['ciclos_jc']}! ${antes:.4f}→${depois:.4f} (+{ganho:.4f}%)", "compound")

def executar_arb(res):
    if BOT["paper"]:
        add_log(f"🧪 SIM | {res['tri']} | +${res['lucro']:.6f} (+{res['pct']:.4f}%) | slip {res['slip']:.4f}%", "success")
        return True, res["lucro"]
    par1, par2, par3 = res["pares"]
    p1, p2, _        = res["precos"]
    qa, qb, _        = res["qtds"]
    try:
        t0 = time.time()
        o1 = ex.create_market_order(par1, "buy",  res["capital"] / p1); time.sleep(0.08)
        o2 = ex.create_market_order(par2, "buy",  float(o1.get("filled", qa)) / p2); time.sleep(0.08)
        o3 = ex.create_market_order(par3, "sell", float(o2.get("filled", qb)))
        lr = float(o3.get("cost", 0)) - res["capital"]
        add_log(f"✅ ARB real em {time.time()-t0:.2f}s | Lucro: ${lr:+.6f}", "success")
        return True, lr
    except ccxt.InsufficientFunds:
        add_log("❌ Saldo insuficiente", "error"); return False, 0
    except Exception as e:
        add_log(f"❌ Erro: {e}", "error"); return False, 0

# ═══════════════════════════════════════════════════
#  LOOP DO BOT
# ═══════════════════════════════════════════════════
def bot_loop():
    global ex
    add_log("🚀 Bot iniciado", "success")

    ex = ccxt.binance({
        "apiKey":          BOT["api_key"],
        "secret":          BOT["api_secret"],
        "enableRateLimit": True,
        "options":         {"defaultType": "spot"},
    })

    # Saldo real
    if not BOT["paper"] and BOT["api_key"]:
        try:
            bal = ex.fetch_balance()
            usdt = float(bal.get("USDT", {}).get("free", BOT["capital"]))
            BOT["saldo_conta"] = usdt
            add_log(f"💳 Saldo Binance: ${usdt:.4f} USDT", "info")
        except Exception as e:
            add_log(f"Aviso saldo: {e}", "warn")

    ultimo_saldo = time.time()

    while BOT["running"]:
        try:
            # Drawdown check
            if BOT["cap_inicial"] > 0:
                dd = (BOT["cap_inicial"] - BOT["capital"]) / BOT["cap_inicial"] * 100
                BOT["drawdown"] = max(0, dd)
                if dd >= BOT["max_dd"]:
                    add_log(f"🛑 DRAWDOWN {dd:.2f}% — Bot parado!", "error")
                    BOT["running"] = False
                    break

            # Sync saldo real cada 2 min
            if not BOT["paper"] and time.time() - ultimo_saldo > 120:
                try:
                    bal = ex.fetch_balance()
                    BOT["saldo_conta"] = float(bal.get("USDT", {}).get("free", BOT["saldo_conta"]))
                except: pass
                ultimo_saldo = time.time()

            BOT["scans"] += 1
            ops = []

            for tri in TRIANGULOS:
                tri_str = f"{tri[0]}→{tri[1]}→{tri[2]}→{tri[0]}"
                # Cooldown
                cd = BOT["cooldowns"].get(tri_str, 0)
                if time.time() - cd < 30:
                    continue
                res = calcular(tri, BOT["capital"])
                if res is None: continue
                if res["pct"] > BOT["melhor"]: BOT["melhor"] = res["pct"]
                if res["ok"]:
                    ops.append(res)
                    BOT["arbs_achadas"] += 1
                elif res["pct"] > 0:
                    BOT["arbs_rejeit"] += 1

            # Guarda scan para o dashboard
            todos = []
            for tri in TRIANGULOS:
                r = calcular(tri, BOT["capital"])
                if r: todos.append(r)
            todos.sort(key=lambda x: x["pct"], reverse=True)
            BOT["scan_data"] = todos[:20]

            # Executa melhor
            if ops:
                ops.sort(key=lambda x: x["pct"] - x["slip"]*2, reverse=True)
                melhor = ops[0]
                sucesso, lucro = executar_arb(melhor)
                if sucesso:
                    registar_lucro(lucro)
                    BOT["arbs_exec"] += 1
                    BOT["last_arb"]  = melhor
                    BOT["cooldowns"][melhor["tri"]] = time.time()

            time.sleep(2)

        except ccxt.NetworkError as e:
            add_log(f"⚠️ Rede: {e}", "warn"); time.sleep(15)
        except ccxt.RateLimitExceeded:
            add_log("⏳ Rate limit", "warn"); time.sleep(30)
        except Exception as e:
            add_log(f"Erro: {e}", "error"); time.sleep(10)

    add_log("⛔ Bot parado", "warn")

# ═══════════════════════════════════════════════════
#  API REST
# ═══════════════════════════════════════════════════
@app.route("/api/status")
def api_status():
    roi = (BOT["capital"]/BOT["cap_inicial"]-1)*100 if BOT["cap_inicial"] > 0 else 0
    gatilho = BOT["cap_base"] * BOT["gatilho_jc"] / 100
    prog = min(100, BOT["lucro_ciclo"]/gatilho*100) if gatilho > 0 else 0
    return jsonify({
        "running":      BOT["running"],
        "paper":        BOT["paper"],
        "capital":      round(BOT["capital"], 6),
        "saldo_conta":  round(BOT["saldo_conta"], 4),
        "lucro_total":  round(BOT["lucro_total"], 6),
        "lucro_ciclo":  round(BOT["lucro_ciclo"], 6),
        "roi":          round(roi, 4),
        "ciclos_jc":    BOT["ciclos_jc"],
        "prog_ciclo":   round(prog, 2),
        "gatilho_usdt": round(gatilho, 6),
        "falta":        round(max(0, gatilho - BOT["lucro_ciclo"]), 6),
        "arbs_exec":    BOT["arbs_exec"],
        "arbs_achadas": BOT["arbs_achadas"],
        "arbs_rejeit":  BOT["arbs_rejeit"],
        "melhor":       round(BOT["melhor"], 4),
        "drawdown":     round(BOT["drawdown"], 2),
        "scans":        BOT["scans"],
        "marcos":       BOT["marcos"][:5],
        "last_arb":     BOT["last_arb"],
    })

@app.route("/api/logs")
def api_logs():
    return jsonify(BOT["logs"][:60])

@app.route("/api/scan")
def api_scan():
    return jsonify(BOT["scan_data"][:20])

@app.route("/api/start", methods=["POST"])
def api_start():
    global bot_thread
    if BOT["running"]:
        return jsonify({"ok": False, "msg": "Já está a correr"})
    data = request.json or {}
    BOT.update({
        "paper":        data.get("paper", True),
        "capital":      float(data.get("capital", 10)),
        "cap_inicial":  float(data.get("capital", 10)),
        "cap_base":     float(data.get("capital", 10)),
        "saldo_conta":  float(data.get("saldo_conta", data.get("capital", 10))),
        "gatilho_jc":   float(data.get("gatilho_jc", 10)),
        "lucro_min":    float(data.get("lucro_min", 0.20)),
        "slip_max":     float(data.get("slip_max", 0.05)),
        "max_dd":       float(data.get("max_dd", 10)),
        "api_key":      data.get("api_key", BOT["api_key"]),
        "api_secret":   data.get("api_secret", BOT["api_secret"]),
        "lucro_total":  0, "lucro_ciclo": 0, "ciclos_jc": 0,
        "arbs_exec": 0, "arbs_achadas": 0, "arbs_rejeit": 0,
        "scans": 0, "melhor": 0, "drawdown": 0,
        "cooldowns": {}, "logs": [], "scan_data": [],
        "last_arb": None, "marcos": [], "running": True,
    })
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    BOT["running"] = False
    return jsonify({"ok": True})

@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.json or {}
    for k in ["api_key","api_secret","capital","saldo_conta","gatilho_jc","lucro_min","slip_max","max_dd","paper"]:
        if k in data: BOT[k] = data[k]
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════
#  DASHBOARD HTML (enviado pelo servidor)
# ═══════════════════════════════════════════════════
HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>ARB Bot</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700;800&family=Outfit:wght@400;600;700;800;900&display=swap');
:root{--bg:#07090f;--s1:#0b0e18;--s2:#0f1322;--s3:#141b2e;--s4:#1a2338;--b:#1e2d44;
--cy:#00d4ff;--gr:#00e5a0;--grdk:#00b87d;--gd:#f5c118;--rd:#ff3d6b;--or:#ff7c1a;
--tx:#d8eaf8;--t2:#7a9ab8;--mu:#2e4060;--mu2:#486080;}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--tx);font-family:'Outfit',sans-serif;font-size:14px;
  display:flex;flex-direction:column;}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:linear-gradient(var(--b)14 1px,transparent 1px),
  linear-gradient(90deg,var(--b)14 1px,transparent 1px);
  background-size:44px 44px;}
.root{position:relative;z-index:1;max-width:480px;width:100%;margin:0 auto;
  display:flex;flex-direction:column;height:100vh;}
.top{background:var(--s1)f2;backdrop-filter:blur(20px);border-bottom:1px solid var(--b);
  padding:12px 16px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0;}
.logo{font-family:'JetBrains Mono',monospace;font-size:17px;font-weight:800;letter-spacing:3px;
  background:linear-gradient(135deg,var(--cy),var(--gr));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.pill{display:flex;align-items:center;gap:6px;font-size:11px;font-weight:700;
  letter-spacing:.8px;padding:5px 12px;border-radius:20px;transition:all .3s;}
.pill.off{border:1px solid var(--mu)44;background:var(--mu)18;color:var(--mu2);}
.pill.on{border:1px solid var(--gr)50;background:var(--gr)12;color:var(--gr);}
.dot{width:7px;height:7px;border-radius:50%;}
.dot.off{background:var(--mu2);}
.dot.on{background:var(--gr);box-shadow:0 0 8px var(--gr);animation:p 1.4s infinite;}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
.scroll{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;}
.scroll::-webkit-scrollbar{width:3px}
.scroll::-webkit-scrollbar-thumb{background:var(--b);border-radius:3px}
.nav{background:var(--s1)f5;backdrop-filter:blur(20px);border-top:1px solid var(--b);
  display:grid;grid-template-columns:repeat(4,1fr);flex-shrink:0;}
.nb{display:flex;flex-direction:column;align-items:center;padding:10px 4px 8px;gap:3px;
  background:none;border:none;cursor:pointer;outline:none;color:var(--mu2);
  transition:color .2s;border-top:2px solid transparent;}
.nb.a{color:var(--cy);border-top-color:var(--cy);}
.nb .ic{font-size:18px;line-height:1}
.nb .lb{font-size:9px;font-weight:700;letter-spacing:1px;text-transform:uppercase}
.pg{display:none;padding:14px 14px 0;}
.pg.a{display:block;}
.card{background:var(--s1);border:1px solid var(--b);border-radius:12px;padding:16px;margin-bottom:10px;}
.card-cy{background:linear-gradient(135deg,#00d4ff09,var(--s1));border-color:#00d4ff28;}
.card-gr{background:linear-gradient(135deg,#00e5a009,var(--s1));border-color:#00e5a028;}
.card-gd{background:linear-gradient(135deg,#f5c11809,var(--s1));border-color:#f5c11828;}
.badge{display:inline-flex;align-items:center;gap:4px;font-size:10px;font-weight:700;
  letter-spacing:.8px;text-transform:uppercase;padding:3px 9px;border-radius:20px;white-space:nowrap;}
.bdg-cy{background:#00d4ff14;color:var(--cy);border:1px solid #00d4ff30;}
.bdg-gr{background:#00e5a014;color:var(--gr);border:1px solid #00e5a030;}
.bdg-gd{background:#f5c11814;color:var(--gd);border:1px solid #f5c11830;}
.bdg-or{background:#ff7c1a14;color:var(--or);border:1px solid #ff7c1a30;}
.bdg-rd{background:#ff3d6b14;color:var(--rd);border:1px solid #ff3d6b30;}
.btn{width:100%;padding:14px;border-radius:10px;border:none;cursor:pointer;
  font-family:'Outfit',sans-serif;font-weight:800;font-size:14px;transition:all .2s;outline:none;}
.btn-gr{background:linear-gradient(135deg,var(--gr),var(--grdk));color:#000;box-shadow:0 4px 20px #00e5a035;}
.btn-rd{background:linear-gradient(135deg,var(--rd),#cc0040);color:#fff;box-shadow:0 4px 20px #ff3d6b35;}
.btn:active{transform:scale(.98);}
.inp{background:var(--s3);border:1px solid var(--b);border-radius:8px;padding:11px 14px;
  color:var(--tx);font-size:14px;width:100%;outline:none;
  font-family:'JetBrains Mono',monospace;font-weight:700;}
.inp:focus{border-color:var(--cy)60;}
.flbl{font-size:10px;color:var(--mu2);letter-spacing:1.2px;text-transform:uppercase;
  margin-bottom:6px;display:block;}
.frow{margin-bottom:14px;}
.saldo-block{border-radius:13px;overflow:hidden;margin-bottom:10px;border:1px solid var(--b);}
.saldo-top{padding:16px 18px;border-bottom:1px solid var(--b);transition:background .4s;}
.saldo-bot{padding:13px 18px;background:var(--s1);}
.slbl{font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--mu2);
  margin-bottom:5px;display:flex;align-items:center;gap:6px;}
.sval{font-family:'JetBrains Mono',monospace;font-weight:900;font-size:28px;line-height:1;}
.ssub{font-size:11px;color:var(--mu2);margin-top:4px;}
.ldot{width:6px;height:6px;border-radius:50%;background:var(--cy);
  box-shadow:0 0 6px var(--cy);animation:p 1.5s infinite;}
.m2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px;}
.mc{background:var(--s2);border:1px solid var(--b);border-radius:10px;padding:12px 13px;}
.mv{font-family:'JetBrains Mono',monospace;font-weight:900;font-size:20px;line-height:1.15;}
.ml{font-size:9px;color:var(--mu2);letter-spacing:1.2px;text-transform:uppercase;margin-top:4px;}
.ag{display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px;}
.ab{border-radius:10px;padding:13px 10px;text-align:center;}
.ab.m{background:linear-gradient(135deg,#00e5a014,var(--s3));border:1px solid #00e5a030;}
.ab.s{background:var(--s3);border:1px solid var(--b);}
.an{font-family:'JetBrains Mono',monospace;font-weight:900;line-height:1;}
.al{font-size:9px;letter-spacing:1px;text-transform:uppercase;margin-top:5px;font-weight:700;}
.jcb{background:linear-gradient(135deg,#f5c11809,var(--s2));
  border:1px solid #f5c11828;border-radius:12px;padding:16px;margin-bottom:10px;}
.jcr{display:flex;align-items:center;gap:14px;margin-bottom:12px;}
.pb{height:7px;background:var(--s4);border-radius:4px;overflow:hidden;margin-bottom:4px;}
.pf{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--gd),var(--or));
  transition:width .6s cubic-bezier(.4,0,.2,1);}
.ji{font-size:12px;color:var(--t2);line-height:2;}
.jkv{display:flex;justify-content:space-between;}
.la{border-radius:12px;padding:14px 16px;margin-bottom:10px;
  border:1px solid #00e5a025;background:linear-gradient(135deg,#00e5a00c,var(--s2));}
.lp{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;
  color:var(--tx);margin:7px 0;word-break:break-all;}
.ad{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;}
.adb{background:var(--s3);border-radius:7px;padding:7px 8px;text-align:center;}
.adv{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;}
.adl{font-size:9px;color:var(--mu2);margin-top:2px;text-transform:uppercase;letter-spacing:1px;}
.st{background:var(--s1);border:1px solid var(--b);border-radius:12px;overflow:hidden;margin-bottom:10px;}
.sh{display:grid;grid-template-columns:1fr 70px 60px 36px;
  padding:8px 12px;border-bottom:1px solid var(--b);}
.sh span{font-size:9px;color:var(--mu2);letter-spacing:1.5px;text-transform:uppercase;font-weight:700;}
.sh span:not(:first-child){text-align:right;}
.sr{display:grid;grid-template-columns:1fr 70px 60px 36px;
  padding:9px 12px;border-bottom:1px solid var(--b)16;align-items:center;}
.sr:last-child{border:none;}
.sr.ok{background:#00e5a006;}
.sc{font-family:'JetBrains Mono',monospace;font-size:11px;}
.sc:not(:first-child){text-align:right;}
.la2{background:var(--s1);border:1px solid var(--b);border-radius:11px;max-height:320px;overflow-y:auto;}
.le{padding:7px 13px;border-bottom:1px solid var(--b)20;
  font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.5;}
.le:last-child{border:none;}
.lt{color:var(--mu2);margin-right:7px;}
.cfg{background:var(--s2);border:1px solid var(--b);border-radius:12px;padding:16px;margin-bottom:10px;}
.tr{display:flex;justify-content:space-between;align-items:center;
  padding:10px 0;border-bottom:1px solid var(--b)28;}
.tr:last-child{border:none;}
.tgl{width:46px;height:26px;border-radius:13px;position:relative;
  cursor:pointer;transition:background .25s;border:none;outline:none;flex-shrink:0;}
.tk{position:absolute;top:3px;width:20px;height:20px;border-radius:50%;
  background:#fff;transition:left .25s;box-shadow:0 1px 5px rgba(0,0,0,.5);}
.qb{padding:6px 12px;border-radius:7px;cursor:pointer;font-weight:700;font-size:12px;
  font-family:'JetBrains Mono',monospace;background:var(--s3);
  border:1px solid var(--b);color:var(--mu2);transition:all .15s;}
.qb.a{background:#f5c11818;border-color:#f5c11850;color:var(--gd);}
.qb2{padding:5px 10px;border-radius:7px;cursor:pointer;font-weight:700;font-size:11px;
  font-family:'JetBrains Mono',monospace;background:var(--s3);
  border:1px solid var(--b);color:var(--mu2);}
.qb2.a{background:#ff7c1a18;border-color:#ff7c1a50;color:var(--or);}
.qb3{padding:5px 10px;border-radius:7px;cursor:pointer;font-weight:700;font-size:11px;
  font-family:'JetBrains Mono',monospace;background:var(--s3);
  border:1px solid var(--b);color:var(--mu2);}
.qb3.a{background:#ff3d6b18;border-color:#ff3d6b50;color:var(--rd);}
.empty{padding:44px 20px;text-align:center;color:var(--mu2);}
.ei{font-size:36px;margin-bottom:10px;}
.eh{font-size:14px;font-weight:700;margin-bottom:6px;color:var(--t2);}
.ep{font-size:12px;line-height:1.6;}
</style>
</head>
<body>
<div class="root">
<div class="top">
  <div class="logo">ARB △</div>
  <div class="pill off" id="pill">
    <div class="dot off" id="dot"></div>
    <span id="stxt">OFFLINE</span>
  </div>
</div>
<div class="scroll">

<!-- DASHBOARD -->
<div class="pg a" id="pgD">
  <div class="saldo-block">
    <div class="saldo-top" id="stop">
      <div class="slbl">
        <span class="ldot" id="ldot" style="display:none"></span>
        Saldo Conta Binance · USDT
        <span class="badge bdg-cy" id="ltag" style="display:none;font-size:8px">LIVE</span>
      </div>
      <div class="sval" id="sConta" style="color:var(--cy)">$0.0000</div>
      <div class="ssub" id="sContaSub">Inicia o bot para ver o saldo</div>
    </div>
    <div class="saldo-bot">
      <div class="slbl">
        <span id="bdot" style="width:6px;height:6px;border-radius:50%;background:var(--mu2);display:inline-block;transition:all .3s"></span>
        Capital Bot · Operacional
      </div>
      <div class="sval" id="sBot" style="color:var(--gr);font-size:24px">$0.000000</div>
      <div class="ssub" id="roiTxt">actualiza após reinvestimento</div>
    </div>
  </div>

  <button class="btn btn-gr" id="btnStart" onclick="toggleBot()">▶ Iniciar Bot</button>

  <div class="card" style="margin-bottom:10px">
    <div class="ml" style="margin-bottom:10px">Arbitragens</div>
    <div class="ag">
      <div class="ab m" id="boxE">
        <div class="an" style="font-size:34px;color:var(--gr)" id="cE">0</div>
        <div class="al" style="color:var(--gr)">Executadas</div>
      </div>
      <div class="ab s">
        <div class="an" style="font-size:24px;color:#4db8ff" id="cA">0</div>
        <div class="al" style="color:var(--mu2)">Achadas</div>
      </div>
      <div class="ab s">
        <div class="an" style="font-size:24px;color:var(--mu2)" id="cR">0</div>
        <div class="al" style="color:var(--mu2)">Rejeitadas</div>
      </div>
    </div>
  </div>

  <div class="m2">
    <div class="mc" style="border-top:2px solid var(--gr)">
      <div class="mv" id="mL" style="color:var(--gr)">+$0.000000</div>
      <div class="ml">Lucro Total</div>
    </div>
    <div class="mc" style="border-top:2px solid var(--gd)">
      <div class="mv" id="mS" style="color:var(--gd)">0.0000%</div>
      <div class="ml">Melhor Spread</div>
    </div>
    <div class="mc" style="border-top:2px solid var(--gd)">
      <div class="mv" id="mC" style="color:var(--gd)">0</div>
      <div class="ml">Ciclos JC</div>
    </div>
    <div class="mc" style="border-top:2px solid var(--mu2)">
      <div class="mv" id="mD" style="color:var(--mu2)">0.00%</div>
      <div class="ml">Drawdown</div>
    </div>
  </div>

  <div class="jcb">
    <div class="jcr">
      <div style="position:relative;width:84px;height:84px;flex-shrink:0">
        <svg width="84" height="84" style="transform:rotate(-90deg)">
          <circle cx="42" cy="42" r="35" fill="none" stroke="var(--s4)" stroke-width="8"/>
          <circle cx="42" cy="42" r="35" fill="none" stroke="var(--gd)" stroke-width="8"
            stroke-dasharray="0 220" stroke-linecap="round" id="ring"
            style="transition:stroke-dasharray .5s cubic-bezier(.4,0,.2,1)"/>
        </svg>
        <div style="position:absolute;inset:0;display:flex;flex-direction:column;
          align-items:center;justify-content:center;gap:1px;text-align:center">
          <div style="font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:900;
            color:var(--gd)" id="rPct">0%</div>
          <div style="font-size:8px;color:var(--mu2);letter-spacing:1px">CICLO</div>
        </div>
      </div>
      <div style="flex:1">
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
          <span class="badge bdg-gd">🔁 Juros Compostos</span>
          <span class="badge bdg-gr" style="font-size:9px" id="cNum">#0</span>
        </div>
        <div class="ji">
          <div class="jkv"><span>Lucro ciclo:</span>
            <strong id="jL" style="color:var(--gd);font-family:'JetBrains Mono',monospace">$0.000000</strong></div>
          <div class="jkv"><span>Gatilho:</span>
            <strong id="jG" style="color:var(--tx);font-family:'JetBrains Mono',monospace">$0.0000</strong></div>
          <div class="jkv"><span>Falta:</span>
            <strong id="jF" style="color:var(--t2);font-family:'JetBrains Mono',monospace">$0.000000</strong></div>
        </div>
      </div>
    </div>
    <div class="pb"><div class="pf" id="pFill" style="width:0%"></div></div>
    <div style="display:flex;justify-content:space-between;margin-top:4px">
      <span style="font-size:9px;color:var(--mu)">$0</span>
      <span style="font-size:9px;color:var(--gd);font-family:'JetBrains Mono',monospace;font-weight:700" id="pPct">0%</span>
      <span style="font-size:9px;color:var(--mu);font-family:'JetBrains Mono',monospace" id="pMax">$0</span>
    </div>
  </div>

  <div class="la" id="lastArb" style="display:none">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span class="badge bdg-gr" id="laBadge">✨ Arb #0</span>
      <span style="font-family:'JetBrains Mono',monospace;color:var(--gr);font-weight:800;font-size:14px" id="laLucro">+$0</span>
    </div>
    <div class="lp" id="laPath">—</div>
    <div class="ad">
      <div class="adb"><div class="adv" id="laSpread" style="color:var(--gd)">0%</div><div class="adl">Spread</div></div>
      <div class="adb"><div class="adv" id="laSlip" style="color:var(--or)">0%</div><div class="adl">Slippage</div></div>
      <div class="adb"><div class="adv" id="laLiq" style="color:#4db8ff">$0</div><div class="adl">Liquidez</div></div>
    </div>
  </div>
  <div style="text-align:center;padding:8px 0 4px">
    <span class="badge" id="modeBadge" style="background:#ff7c1a14;color:var(--or);border:1px solid #ff7c1a30">🧪 Simulação</span>
  </div>
</div>

<!-- SCAN -->
<div class="pg" id="pgS">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <div style="font-weight:800;font-size:16px">Scan ao Vivo</div>
    <div style="display:flex;gap:8px;align-items:center">
      <span style="font-size:10px;color:var(--mu2);font-family:'JetBrains Mono',monospace" id="scCount">0 scans</span>
      <span class="badge bdg-gr" id="scLive" style="display:none;font-size:9px">● LIVE</span>
    </div>
  </div>
  <div class="st">
    <div class="sh">
      <span>Triângulo</span><span>Spread</span><span>Slip</span><span></span>
    </div>
    <div id="scBody">
      <div class="empty"><div class="ei">△</div><div class="eh">Bot parado</div>
        <div class="ep">Inicia o bot para ver os triângulos.</div></div>
    </div>
  </div>
</div>

<!-- LOG -->
<div class="pg" id="pgL">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <div style="font-weight:800;font-size:16px">Registo</div>
    <button onclick="fetch('/api/logs').then(()=>{})" style="background:var(--s3);border:1px solid var(--b);
      color:var(--t2);padding:6px 12px;border-radius:7px;font-size:11px;font-weight:700;cursor:pointer">Actualizar</button>
  </div>
  <div class="la2" id="logArea">
    <div class="empty"><div class="ei">≡</div><div class="eh">Sem registos</div></div>
  </div>
</div>

<!-- CONFIG -->
<div class="pg" id="pgC">
  <div style="font-weight:800;font-size:16px;margin-bottom:14px">Configurações</div>
  <div class="cfg">
    <span class="badge bdg-cy" style="margin-bottom:14px">🔑 Chaves API Binance</span>
    <div class="frow" style="margin-top:14px">
      <label class="flbl">API Key</label>
      <input class="inp" type="text" id="iKey" placeholder="API Key" autocomplete="off" autocapitalize="none" spellcheck="false">
    </div>
    <div class="frow">
      <label class="flbl">API Secret</label>
      <input class="inp" type="password" id="iSec" placeholder="Secret Key" autocomplete="off" autocapitalize="none" spellcheck="false">
    </div>
    <div style="background:var(--s4);border-radius:8px;padding:10px 12px;font-size:12px;color:var(--t2)">
      🔒 Na Binance activa só <strong style="color:var(--gr)">Spot Trading</strong> — <strong style="color:var(--rd)">NUNCA Withdrawals</strong>
    </div>
  </div>
  <div class="cfg">
    <span class="badge bdg-cy" style="margin-bottom:14px">💰 Capital</span>
    <div class="frow" style="margin-top:14px">
      <label class="flbl">Capital operacional (USDT)</label>
      <div style="display:flex;gap:8px;align-items:center">
        <button onclick="document.getElementById('iCap').value=10" style="padding:9px 12px;background:var(--s4);border:1px solid var(--b);color:var(--t2);border-radius:8px;font-weight:700;font-size:11px;cursor:pointer;flex-shrink:0">MIN<br><span style="color:var(--cy);font-size:10px">$10</span></button>
        <input class="inp" type="number" id="iCap" value="10" min="10" step="1" style="flex:1;text-align:right;font-size:16px">
        <button onclick="document.getElementById('iCap').value=document.getElementById('iConta').value" style="padding:9px 12px;background:#00d4ff12;border:1px solid #00d4ff30;color:var(--cy);border-radius:8px;font-weight:700;font-size:11px;cursor:pointer;flex-shrink:0">MÁX<br><span style="font-size:10px" id="btnMx">$?</span></button>
      </div>
    </div>
    <div class="frow">
      <label class="flbl">Saldo total Binance (USDT)</label>
      <input class="inp" type="number" id="iConta" value="10" min="0" step="1" oninput="document.getElementById('btnMx').textContent='$'+this.value">
    </div>
  </div>
  <div class="cfg">
    <span class="badge bdg-gd" style="margin-bottom:14px">🔁 Juros Compostos</span>
    <div style="margin-top:14px">
      <label class="flbl">Gatilho de reinvestimento</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0">
        <button class="qb" data-v="5"  onclick="setQ(this,'qb',5)">5%</button>
        <button class="qb a" data-v="10" onclick="setQ(this,'qb',10)">10%</button>
        <button class="qb" data-v="15" onclick="setQ(this,'qb',15)">15%</button>
        <button class="qb" data-v="20" onclick="setQ(this,'qb',20)">20%</button>
        <button class="qb" data-v="25" onclick="setQ(this,'qb',25)">25%</button>
      </div>
      <div style="background:var(--s4);border-radius:8px;padding:10px 12px;font-size:12px;color:var(--t2)">
        Reinveste quando lucrar <strong id="gInfo" style="color:var(--gd);font-family:'JetBrains Mono',monospace">$1.0000</strong>
      </div>
    </div>
  </div>
  <div class="cfg">
    <span class="badge bdg-or" style="margin-bottom:14px">⚡ Parâmetros</span>
    <div style="margin-top:14px">
      <label class="flbl">Lucro mínimo por arb</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">
        <button class="qb2" data-l="0.10" onclick="setQ(this,'qb2',0.10)">0.10%</button>
        <button class="qb2" data-l="0.15" onclick="setQ(this,'qb2',0.15)">0.15%</button>
        <button class="qb2 a" data-l="0.20" onclick="setQ(this,'qb2',0.20)">0.20%</button>
        <button class="qb2" data-l="0.30" onclick="setQ(this,'qb2',0.30)">0.30%</button>
      </div>
      <label class="flbl">Max Drawdown</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="qb3" data-d="5"  onclick="setQ(this,'qb3',5)">5%</button>
        <button class="qb3 a" data-d="10" onclick="setQ(this,'qb3',10)">10%</button>
        <button class="qb3" data-d="15" onclick="setQ(this,'qb3',15)">15%</button>
        <button class="qb3" data-d="20" onclick="setQ(this,'qb3',20)">20%</button>
      </div>
    </div>
  </div>
  <div class="cfg">
    <span class="badge bdg-gr" style="margin-bottom:14px">⚙️ Modo</span>
    <div class="tr" style="margin-top:12px">
      <div><div style="font-size:13px;font-weight:600">Simulação (Paper Trading)</div>
        <div style="font-size:11px;color:var(--mu2);margin-top:2px">Testa sem dinheiro real</div></div>
      <button class="tgl" id="tglP" onclick="togglePaper()" style="background:var(--gr)">
        <div class="tk" id="tglPK" style="left:23px"></div>
      </button>
    </div>
  </div>
  <button class="btn btn-gr" onclick="saveConfig()" style="margin-bottom:10px">✓ Guardar e Aplicar</button>
</div>

</div><!-- scroll -->

<nav class="nav">
  <button class="nb a" onclick="showPg('D',this)"><span class="ic">◈</span><span class="lb">Dashboard</span></button>
  <button class="nb"   onclick="showPg('S',this)"><span class="ic">△</span><span class="lb">Scan</span></button>
  <button class="nb"   onclick="showPg('L',this)"><span class="ic">≡</span><span class="lb">Logs</span></button>
  <button class="nb"   onclick="showPg('C',this)"><span class="ic">⊙</span><span class="lb">Config</span></button>
</nav>
</div>

<script>
// Config state
let C = { gatilho_jc:10, lucro_min:0.20, slip_max:0.05, max_dd:10, paper:true };
let botRunning = false;

function showPg(id, btn) {
  document.querySelectorAll('.pg').forEach(p => p.classList.remove('a'));
  document.querySelectorAll('.nb').forEach(b => b.classList.remove('a'));
  document.getElementById('pg'+id).classList.add('a');
  btn.classList.add('a');
}

function setQ(el, cls, v) {
  document.querySelectorAll('.'+cls).forEach(b => b.classList.remove('a'));
  el.classList.add('a');
  if(cls==='qb')  C.gatilho_jc = v;
  if(cls==='qb2') C.lucro_min  = v;
  if(cls==='qb3') C.max_dd     = v;
  updateGInfo();
}

function updateGInfo() {
  const cap = parseFloat(document.getElementById('iCap').value)||10;
  document.getElementById('gInfo').textContent = '$'+(cap*C.gatilho_jc/100).toFixed(4);
}
document.getElementById('iCap').addEventListener('input', updateGInfo);

let paper = true;
function togglePaper() {
  paper = !paper; C.paper = paper;
  document.getElementById('tglP').style.background = paper ? 'var(--gr)' : 'var(--mu)';
  document.getElementById('tglPK').style.left = paper ? '23px' : '3px';
}

function saveConfig() {
  C.api_key    = document.getElementById('iKey').value.trim();
  C.api_secret = document.getElementById('iSec').value.trim();
  C.capital    = parseFloat(document.getElementById('iCap').value)||10;
  C.saldo_conta= parseFloat(document.getElementById('iConta').value)||10;
  C.paper      = paper;
  fetch('/api/config', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(C)});
  showPg('D', document.querySelectorAll('.nb')[0]);
  document.querySelectorAll('.nb').forEach((b,i)=>i===0?b.classList.add('a'):b.classList.remove('a'));
}

function toggleBot() {
  if (botRunning) {
    fetch('/api/stop',{method:'POST'});
  } else {
    const payload = {
      paper:       C.paper||paper,
      capital:     parseFloat(document.getElementById('iCap').value)||10,
      saldo_conta: parseFloat(document.getElementById('iConta').value)||10,
      gatilho_jc:  C.gatilho_jc,
      lucro_min:   C.lucro_min,
      slip_max:    C.slip_max,
      max_dd:      C.max_dd,
      api_key:     document.getElementById('iKey').value.trim(),
      api_secret:  document.getElementById('iSec').value.trim(),
    };
    fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  }
}

// Poll status every 2s
let prevExec = 0;
function poll() {
  fetch('/api/status').then(r=>r.json()).then(d => {
    botRunning = d.running;
    const btn = document.getElementById('btnStart');
    const pill= document.getElementById('pill');
    const dot = document.getElementById('dot');
    btn.textContent = d.running ? '⏹ Parar Bot' : '▶ Iniciar Bot';
    btn.className   = d.running ? 'btn btn-rd' : 'btn btn-gr';
    pill.className  = d.running ? 'pill on' : 'pill off';
    dot.className   = d.running ? 'dot on' : 'dot off';
    document.getElementById('stxt').textContent = d.running ? 'ONLINE' : 'OFFLINE';
    document.getElementById('ldot').style.display = d.running ? 'inline-block' : 'none';
    document.getElementById('ltag').style.display = d.running ? 'inline-flex' : 'none';
    document.getElementById('scLive').style.display = d.running ? 'inline-flex' : 'none';

    // Saldos
    document.getElementById('sConta').textContent = '$'+d.saldo_conta.toFixed(4);
    document.getElementById('sBot').textContent   = '$'+d.capital.toFixed(6);
    document.getElementById('sBot').style.color   = d.roi>=0 ? 'var(--gr)' : 'var(--rd)';
    document.getElementById('roiTxt').textContent = (d.roi>=0?'▲':'▼')+' '+Math.abs(d.roi).toFixed(4)+'% ROI';
    document.getElementById('sContaSub').textContent = (d.paper?'simulado':'saldo real')+' · actualizado ao vivo';

    // Flash se nova arb
    if(d.arbs_exec > prevExec) {
      prevExec = d.arbs_exec;
      const el = document.getElementById('cE');
      el.style.textShadow = '0 0 20px var(--gr)';
      setTimeout(()=>el.style.textShadow='',700);
    }

    // Contadores
    document.getElementById('cE').textContent = d.arbs_exec;
    document.getElementById('cA').textContent = d.arbs_achadas;
    document.getElementById('cR').textContent = d.arbs_rejeit;
    document.getElementById('mL').textContent = '+$'+d.lucro_total.toFixed(6);
    document.getElementById('mS').textContent = d.melhor.toFixed(4)+'%';
    document.getElementById('mC').textContent = d.ciclos_jc;
    const dd = document.getElementById('mD');
    dd.textContent = d.drawdown.toFixed(2)+'%';
    dd.style.color = d.drawdown>d.max_dd*.7?'var(--rd)':d.drawdown>d.max_dd*.4?'var(--or)':'var(--mu2)';

    // JC Ring
    const circ=220, prog=d.prog_ciclo;
    document.getElementById('ring').setAttribute('stroke-dasharray',`${circ*prog/100} ${circ}`);
    document.getElementById('rPct').textContent = prog.toFixed(0)+'%';
    document.getElementById('cNum').textContent = '#'+d.ciclos_jc;
    document.getElementById('pFill').style.width = prog+'%';
    document.getElementById('pPct').textContent = prog.toFixed(1)+'%';
    document.getElementById('pMax').textContent = '$'+d.gatilho_usdt.toFixed(4);
    document.getElementById('jL').textContent = '$'+d.lucro_ciclo.toFixed(6);
    document.getElementById('jG').textContent = '$'+d.gatilho_usdt.toFixed(4);
    const fEl = document.getElementById('jF');
    fEl.textContent = '$'+d.falta.toFixed(6);
    fEl.style.color = d.falta < d.gatilho_usdt*.15 ? 'var(--gr)' : 'var(--t2)';

    // Última arb
    if(d.last_arb) {
      const la = d.last_arb;
      document.getElementById('lastArb').style.display='block';
      document.getElementById('laBadge').textContent = '✨ Arb #'+d.arbs_exec;
      document.getElementById('laLucro').textContent = '+$'+la.lucro.toFixed(6);
      document.getElementById('laPath').textContent  = la.tri;
      document.getElementById('laSpread').textContent= la.pct.toFixed(4)+'%';
      document.getElementById('laSlip').textContent  = (la.slip||0).toFixed(4)+'%';
      document.getElementById('laLiq').textContent   = '$'+Math.floor(la.lmin);
    }

    document.getElementById('scCount').textContent = d.scans+' scans';
    const mb = document.getElementById('modeBadge');
    mb.textContent = d.paper ? '🧪 Simulação' : '🔴 Dinheiro Real';
    mb.style.color = d.paper ? 'var(--or)' : 'var(--rd)';
  }).catch(()=>{});

  // Scan
  fetch('/api/scan').then(r=>r.json()).then(data => {
    if(!data||!data.length) return;
    const html = data.slice(0,20).map(r => {
      const col = r.pct>=0.20?'var(--gr)':r.pct>0?'var(--gd)':'var(--rd)';
      const tri = r.tri.split('→').slice(1,3).join('→');
      const ok  = r.ok?`<span style="color:var(--gr);font-weight:800;font-size:14px">✓</span>`:`<span style="color:var(--mu)">–</span>`;
      return `<div class="sr ${r.ok?'ok':''}">
        <span class="sc" style="color:var(--tx)">${tri}</span>
        <span class="sc" style="color:${col};font-weight:700">${r.pct>=0?'+':''}${r.pct.toFixed(3)}%</span>
        <span class="sc" style="color:var(--mu2)">${(r.slip||0).toFixed(3)}%</span>
        <span class="sc">${ok}</span>
      </div>`;
    }).join('');
    document.getElementById('scBody').innerHTML = html;
  }).catch(()=>{});

  // Logs
  fetch('/api/logs').then(r=>r.json()).then(logs => {
    const cols = {success:'var(--gr)',compound:'var(--gd)',warn:'var(--or)',error:'var(--rd)',info:'var(--mu2)'};
    const bg   = {success:'#00e5a005',compound:'#f5c11808',error:'#ff3d6b05'};
    const html = logs.slice(0,60).map(l =>
      `<div class="le" style="color:${cols[l.tipo]||cols.info};background:${bg[l.tipo]||'transparent'}">
        <span class="lt">${l.ts}</span>${l.msg}
      </div>`
    ).join('');
    document.getElementById('logArea').innerHTML = html ||
      `<div class="empty"><div class="ei">≡</div><div class="eh">Sem registos</div></div>`;
  }).catch(()=>{});
}

setInterval(poll, 2000);
poll();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
