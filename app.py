"""
ARB TRIANGULAR BOT — Versão Final Profissional
Deploy: Render.com (free) — github → render → pronto
"""
from flask import Flask, jsonify, request, render_template_string
import ccxt, threading, time, os, json
from datetime import datetime
from collections import defaultdict

app = Flask(__name__)

BOT = {
    "running": False, "paper": True,
    "capital": 10.0, "cap_inicial": 10.0, "cap_base": 10.0,
    "saldo_conta": 10.0,
    "lucro_total": 0.0, "lucro_ciclo": 0.0,
    "ciclos_jc": 0, "gatilho_jc": 10.0,
    "arbs_exec": 0, "arbs_achadas": 0, "arbs_rejeit": 0,
    "scans": 0, "melhor": 0.0, "drawdown": 0.0,
    "lucro_min": 0.20, "slip_max": 0.05, "liq_min": 500, "max_dd": 10.0,
    "api_key": os.getenv("BINANCE_API_KEY", ""),
    "api_secret": os.getenv("BINANCE_API_SECRET", ""),
    "cooldowns": {}, "logs": [], "scan_data": [], "last_arb": None, "marcos": [],
    "arbs_hora": 0, "hora_atual": datetime.now().hour,
}
TAXA = 0.00075
ex = None
_lock = threading.Lock()

TRIANGULOS = [
    ["USDT","BTC","ETH"],["USDT","BTC","BNB"],["USDT","ETH","BNB"],
    ["USDT","BTC","SOL"],["USDT","ETH","SOL"],["USDT","BNB","SOL"],
    ["USDT","BTC","XRP"],["USDT","ETH","XRP"],["USDT","BTC","ADA"],
    ["USDT","BTC","DOGE"],["USDT","ETH","DOGE"],["USDT","BNB","XRP"],
    ["USDT","BTC","AVAX"],["USDT","ETH","AVAX"],["USDT","BTC","LINK"],
    ["USDT","ETH","LINK"],["USDT","BTC","MATIC"],["USDT","ETH","MATIC"],
    ["USDT","BNB","MATIC"],["USDT","BTC","DOT"],
]

def log(msg, t="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        BOT["logs"].insert(0, {"ts": ts, "msg": msg, "t": t})
        BOT["logs"] = BOT["logs"][:200]
    print(f"{ts} | {msg}")

def preco_ob(par, lado, usdt):
    try:
        ob = ex.fetch_order_book(par, limit=10)
        ns = ob["asks"] if lado == "c" else ob["bids"]
        if not ns: return None, None, None
        best = float(ns[0][0])
        liq  = sum(float(p)*float(q) for p,q in ns)
        if liq < BOT["liq_min"]: return None, None, liq
        acum = custo = 0.0
        for p,q in ns:
            p,q = float(p),float(q); v=p*q
            if acum+v >= usdt: custo+=usdt-acum; acum=usdt; break
            custo+=v; acum+=v
        if acum < usdt*0.99: return None, None, liq
        med  = custo/(acum/best)
        slip = abs(med-best)/best*100
        return med, slip, liq
    except: return None, None, None

def calcular(tri, capital):
    base,A,B = tri
    try:
        p1,s1,l1 = preco_ob(f"{A}/{base}","c",capital)
        if p1 is None: return None
        qa = (capital/p1)*(1-TAXA)
        p2,s2,l2 = preco_ob(f"{B}/{A}","c",qa*p1*(1-TAXA))
        if p2 is None: return None
        qb = (qa/p2)*(1-TAXA)
        p3,s3,l3 = preco_ob(f"{B}/{base}","v",qb*p2*p1*(1-TAXA))
        if p3 is None: return None
        final = qb*p3*(1-TAXA)
        lucro = final-capital; pct = lucro/capital*100
        slip  = (s1 or 0)+(s2 or 0)+(s3 or 0)
        lmin  = min(l1 or 0, l2 or 0, l3 or 0)
        return {
            "tri": f"{base}@{A}@{B}@{base}",
            "pares": [f"{A}/{base}",f"{B}/{A}",f"{B}/{base}"],
            "precos": [p1,p2,p3], "qtds": [qa,qb,final],
            "capital": capital, "lucro": round(lucro,8),
            "pct": round(pct,6), "slip": round(slip,6),
            "lmin": round(lmin,2),
            "ok": pct>=BOT["lucro_min"] and slip<=BOT["slip_max"] and lmin>=BOT["liq_min"],
        }
    except: return None

def registar_lucro(lucro):
    BOT["capital"]     += lucro
    BOT["lucro_total"] += lucro
    BOT["lucro_ciclo"] += lucro
    BOT["saldo_conta"] += lucro
    g = BOT["cap_base"]*(BOT["gatilho_jc"]/100)
    if BOT["lucro_ciclo"] >= g:
        antes  = BOT["cap_base"]
        depois = BOT["capital"]
        lc     = BOT["lucro_ciclo"]
        ganho  = lc/antes*100
        BOT["ciclos_jc"]  += 1
        BOT["cap_base"]    = depois
        BOT["lucro_ciclo"] = 0.0
        BOT["marcos"].insert(0,{
            "ciclo": BOT["ciclos_jc"],
            "antes": round(antes,6), "depois": round(depois,6),
            "lucro": round(lc,6), "ganho": round(ganho,4),
            "data": datetime.now().strftime("%d/%m %H:%M"),
        })
        BOT["marcos"] = BOT["marcos"][:20]
        log(f"JUROS COMPOSTOS #{BOT['ciclos_jc']} | ${antes:.4f}@${depois:.4f} (+{ganho:.4f}%)", "compound")

def executar_arb(res):
    if BOT["paper"]:
        log(f"SIM | {res['tri']} | +${res['lucro']:.6f} (+{res['pct']:.4f}%) | slip {res['slip']:.4f}%","success")
        return True, res["lucro"]
    par1,par2,par3 = res["pares"]
    p1,p2,_ = res["precos"]; qa,qb,_ = res["qtds"]
    try:
        t0 = time.time()
        o1 = ex.create_market_order(par1,"buy",res["capital"]/p1); time.sleep(0.08)
        o2 = ex.create_market_order(par2,"buy",float(o1.get("filled",qa))/p2); time.sleep(0.08)
        o3 = ex.create_market_order(par3,"sell",float(o2.get("filled",qb)))
        lr = float(o3.get("cost",0))-res["capital"]
        log(f"ARB REAL {time.time()-t0:.2f}s | Lucro: ${lr:+.6f}","success")
        return True, lr
    except ccxt.InsufficientFunds:
        log("Saldo insuficiente","error"); return False,0
    except Exception as e:
        log(f"Erro execucao: {e}","error"); return False,0

def bot_loop():
    global ex
    log(f"Bot iniciado | Capital ${BOT['capital']} | JC {BOT['gatilho_jc']}% | {'SIM' if BOT['paper'] else 'REAL'}","success")
    ex = ccxt.binance({
        "apiKey": BOT["api_key"], "secret": BOT["api_secret"],
        "enableRateLimit": True, "options": {"defaultType":"spot"},
    })
    if not BOT["paper"] and BOT["api_key"]:
        try:
            bal = ex.fetch_balance()
            BOT["saldo_conta"] = float(bal.get("USDT",{}).get("free",BOT["capital"]))
            log(f"Saldo Binance: ${BOT['saldo_conta']:.4f} USDT","info")
        except Exception as e:
            log(f"Aviso saldo: {e}","warn")
    ult_saldo = time.time()
    while BOT["running"]:
        try:
            if BOT["cap_inicial"] > 0:
                dd = (BOT["cap_inicial"]-BOT["capital"])/BOT["cap_inicial"]*100
                BOT["drawdown"] = max(0,dd)
                if dd >= BOT["max_dd"]:
                    log(f"DRAWDOWN {dd:.2f}% -- Bot parado!","error")
                    BOT["running"] = False; break
            h = datetime.now().hour
            if h != BOT["hora_atual"]: BOT["hora_atual"]=h; BOT["arbs_hora"]=0
            if BOT["arbs_hora"] >= 20: time.sleep(60); continue
            if not BOT["paper"] and time.time()-ult_saldo > 120:
                try:
                    bal = ex.fetch_balance()
                    BOT["saldo_conta"] = float(bal.get("USDT",{}).get("free",BOT["saldo_conta"]))
                except: pass
                ult_saldo = time.time()
            BOT["scans"] += 1
            ops = []
            scan_todos = []
            for tri in TRIANGULOS:
                ts_str = f"{tri[0]}@{tri[1]}@{tri[2]}@{tri[0]}"
                cd = BOT["cooldowns"].get(ts_str,0)
                skip = time.time()-cd < 30
                res = calcular(tri, BOT["capital"])
                if res:
                    if res["pct"] > BOT["melhor"]: BOT["melhor"] = res["pct"]
                    scan_todos.append(res)
                    if not skip and res["ok"]:
                        ops.append(res); BOT["arbs_achadas"]+=1
                    elif res["pct"] > 0 and not res["ok"]:
                        BOT["arbs_rejeit"]+=1
            scan_todos.sort(key=lambda x:x["pct"],reverse=True)
            BOT["scan_data"] = scan_todos[:20]
            if ops:
                ops.sort(key=lambda x:x["pct"]-x["slip"]*2,reverse=True)
                melhor = ops[0]
                check = calcular(melhor["tri"].split("@")[:3], melhor["capital"])
                if check and check["ok"] and abs(check["pct"]-melhor["pct"])<0.3:
                    ok, lucro = executar_arb(melhor)
                    if ok:
                        registar_lucro(lucro)
                        BOT["arbs_exec"]+=1
                        BOT["arbs_hora"]+=1
                        BOT["last_arb"] = melhor
                        BOT["cooldowns"][melhor["tri"]] = time.time()
            time.sleep(2)
        except Exception as e:
            log(f"Erro: {e}","error"); time.sleep(10)
    log("Bot parado","warn")

@app.route("/api/status")
def api_status():
    roi = (BOT["capital"]/BOT["cap_inicial"]-1)*100 if BOT["cap_inicial"]>0 else 0
    g   = BOT["cap_base"]*BOT["gatilho_jc"]/100
    prog= min(100,BOT["lucro_ciclo"]/g*100) if g>0 else 0
    return jsonify({
        "running":BOT["running"],"paper":BOT["paper"],
        "capital":round(BOT["capital"],6),"saldo_conta":round(BOT["saldo_conta"],4),
        "lucro_total":round(BOT["lucro_total"],6),"lucro_ciclo":round(BOT["lucro_ciclo"],6),
        "roi":round(roi,4),"ciclos_jc":BOT["ciclos_jc"],"prog_ciclo":round(prog,2),
        "gatilho_usdt":round(g,6),"falta":round(max(0,g-BOT["lucro_ciclo"]),6),
        "arbs_exec":BOT["arbs_exec"],"arbs_achadas":BOT["arbs_achadas"],"arbs_rejeit":BOT["arbs_rejeit"],
        "melhor":round(BOT["melhor"],4),"drawdown":round(BOT["drawdown"],2),
        "scans":BOT["scans"],"last_arb":BOT["last_arb"],"marcos":BOT["marcos"][:5],
        "max_dd":BOT["max_dd"],"gatilho_jc":BOT["gatilho_jc"],
    })

@app.route("/api/logs")
def api_logs():
    return jsonify(BOT["logs"][:80])

@app.route("/api/scan")
def api_scan():
    return jsonify(BOT["scan_data"][:20])

@app.route("/api/start", methods=["POST"])
def api_start():
    global ex
    if BOT["running"]: return jsonify({"ok":False,"msg":"Ja esta a correr"})
    d = request.json or {}
    with _lock:
        BOT.update({
            "paper":d.get("paper",True),"capital":float(d.get("capital",10)),
            "cap_inicial":float(d.get("capital",10)),"cap_base":float(d.get("capital",10)),
            "saldo_conta":float(d.get("saldo_conta",d.get("capital",10))),
            "gatilho_jc":float(d.get("gatilho_jc",10)),"lucro_min":float(d.get("lucro_min",0.20)),
            "slip_max":float(d.get("slip_max",0.05)),"max_dd":float(d.get("max_dd",10)),
            "api_key":d.get("api_key",BOT["api_key"]),"api_secret":d.get("api_secret",BOT["api_secret"]),
            "lucro_total":0,"lucro_ciclo":0,"ciclos_jc":0,
            "arbs_exec":0,"arbs_achadas":0,"arbs_rejeit":0,
            "scans":0,"melhor":0,"drawdown":0,"arbs_hora":0,
            "cooldowns":{},"logs":[],"scan_data":[],"last_arb":None,"marcos":[],"running":True,
        })
    threading.Thread(target=bot_loop,daemon=True).start()
    return jsonify({"ok":True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    BOT["running"] = False
    return jsonify({"ok":True})

@app.route("/api/config", methods=["POST"])
def api_config():
    d = request.json or {}
    for k,v in d.items():
        if k in BOT: BOT[k]=v
    return jsonify({"ok":True})

@app.route("/")
def index():
    with open("dashboard.html","r",encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
