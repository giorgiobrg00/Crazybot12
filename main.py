import os
import re
import json
import time
import random
import threading
import warnings
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─── CONFIGURAZIONE ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = "8606699249:AAF0Dp3Kfvo0fDmGjSko6zargyoGSLrsblQ"
TELEGRAM_CHAT  = "@segnalicrazy"

PROXY_USER = "mirintel"
PROXY_PASS = "xz2xcafofyvd"
PROXY_HOSTS = ["64.137.96.74:6641", "31.59.20.176:6754", "23.95.150.145:6114", "45.38.107.97:6014"]

SCAN_INTERVAL      = 15     
FAIL_TRIGGER       = 2      
SESSION_MAX        = 6      
SESSION_DURATION   = 1800   
SESSION_WIN_LIMIT  = 6      
SESSION_LOSS_LIMIT = 3      
STATE_FILE         = "session_state.json"

# ─── STATO GLOBALE ──────────────────────────────────────────────────────────────
state = {
    "running": True, "last_update": None, "prev_spins_since": None,
    "inner_phase": 0, "mode": "observing", "cycles_failed": 0, "session_cycles": 0,
    "session_wins": 0, "session_losses": 0, "session_consec_losses": 0,
    "session_max_consec_losses": 0, "session_start_time": None, "last_source": None,
    "last_result": None
}

# ─── FUNZIONI DI SUPPORTO & SCRAPING ───────────────────────────────────────────
def get_proxy():
    host = random.choice(PROXY_HOSTS)
    url = f"http://{PROXY_USER}:{PROXY_PASS}@{host}"
    return {"http": url, "https": url}

def fetch_html(url):
    try:
        r = requests.get(url, proxies=get_proxy(), timeout=20, verify=False)
        return r.text if r.status_code == 200 else None
    except: return None

def extract_tracksino(html):
    soup = BeautifulSoup(html, "lxml")
    spins_since_2, last_result = None, None
    for seg in soup.find_all(class_=re.compile(r"game-stats-seg")):
        img = seg.find("img", alt=re.compile(r"Crazy Time", re.IGNORECASE))
        if not img: continue
        alt, text = img.get("alt", ""), seg.get_text(" ", strip=True)
        m = re.search(r'\)\s*(\d+)\s+spins?\s+since', text)
        if not m: m = re.search(r'[\d.]+%\s*\([^)]+\)\s*(\d+)', text)
        if m:
            val = int(m.group(1))
            if "2 Segment" in alt: spins_since_2 = val
            if val == 0: last_result = re.sub(r'(?i)crazy\s*time\s*|segment', '', alt).strip()
    return spins_since_2, last_result

# ─── GESTIONE TELEGRAM & SESSIONE ──────────────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
    except: pass

def _send_session_report(reason):
    send_telegram(f"🏁 <b>SESSIONE TERMINATA</b> ({reason})\nVincite Totali: <b>{state['session_wins']}</b>\nSconfitte Totali: <b>{state['session_losses']}</b>\nMax Sconfitte Consecutive: <b>{state['session_max_consec_losses']}</b>\n🕒 {datetime.now().strftime('%H:%M:%S')}")

def _reset_session():
    state.update({"session_cycles": 0, "session_wins": 0, "session_losses": 0, "session_consec_losses": 0, "session_max_consec_losses": 0, "session_start_time": datetime.now(), "inner_phase": 0})
    send_telegram(f"🔄 <b>NUOVA SESSIONE AVVIATA</b>\nSegnalo i prossimi {SESSION_MAX} cicli.\n🕒 {datetime.now().strftime('%H:%M:%S')}")

# ─── LOGICA DI GIOCO ───────────────────────────────────────────────────────────
def _handle_cycle_win(puntata):
    if state["mode"] == "session":
        state["session_wins"] += 1
        state["session_consec_losses"] = 0
        send_telegram(f"🎯 <b>VINTO!</b> Puntata {puntata} – Profitto incassato.\n🕒 {datetime.now().strftime('%H:%M:%S')}")
        if state["session_wins"] >= SESSION_WIN_LIMIT: _send_session_report(f"{SESSION_WIN_LIMIT} vincite"); _reset_session()
        else: state["inner_phase"] = 0
    else: state.update({"cycles_failed": 0, "inner_phase": 0})

def _handle_cycle_fail(res):
    state["inner_phase"] = 0
    res_text = f"È uscito: <b>{res}</b>" if res else "risultato non disponibile"
    if state["mode"] == "observing":
        state["cycles_failed"] += 1
        send_telegram(f"⚠️ <b>Ciclo Base fallito</b>\nConsecutivi: {state['cycles_failed']}/{FAIL_TRIGGER}\n🕒 {datetime.now().strftime('%H:%M:%S')}")
        if state["cycles_failed"] >= FAIL_TRIGGER:
            state.update({"mode": "session", "session_start_time": datetime.now(), "cycles_failed": 0})
            send_telegram(f"🚨 <b>ATTIVAZIONE SESSIONE!</b>\n🕒 {datetime.now().strftime('%H:%M:%S')}")
    else:
        state["session_losses"] += 1
        state["session_consec_losses"] += 1
        state["session_max_consec_losses"] = max(state["session_max_consec_losses"], state["session_consec_losses"])
        send_telegram(f"❌ <b>PERDITA</b> ({res_text})\nCiclo {state['session_cycles']}/{SESSION_MAX} terminato.\n🕒 {datetime.now().strftime('%H:%M:%S')}")
        if state["session_losses"] >= SESSION_LOSS_LIMIT: _send_session_report(f"{SESSION_LOSS_LIMIT} sconfitte"); _reset_session()

def process_spin(spins_since, last_result):
    prev = state["prev_spins_since"]
    state["prev_spins_since"] = spins_since
    if last_result: state["last_result"] = last_result
    if prev is None or spins_since == prev: return
    appeared, phase = spins_since < prev, state["inner_phase"]

    if phase == 0 and appeared:
        state["inner_phase"] = 1
        if state["mode"] == "session":
            state["session_cycles"] += 1
            send_telegram(f"🎯 <b>Ciclo {state['session_cycles']}/{SESSION_MAX}</b> – Il 2 è uscito!\nPuntata 1 in gioco...\n🕒 {datetime.now().strftime('%H:%M:%S')}")
    elif phase == 1:
        if appeared: _handle_cycle_win(1)
        else:
            state["inner_phase"] = 2
            if state["mode"] == "session":
                res = last_result or state.get("last_result")
                send_telegram(f"🔄 <b>Puntata 1 mancata</b> – Ciclo {state['session_cycles']}/{SESSION_MAX}\nÈ uscito: <b>{res}</b>\nPuntata 2 in gioco...\n🕒 {datetime.now().strftime('%H:%M:%S')}")
    elif phase == 2:
        if appeared: _handle_cycle_win(2)
        else: _handle_cycle_fail(last_result or state.get("last_result"))

# ─── LOOP & FLASK ──────────────────────────────────────────────────────────────
def bot_loop():
    while state["running"]:
        if state["mode"] == "session" and state["session_start_time"]:
            if (datetime.now() - state["session_start_time"]).total_seconds() >= SESSION_DURATION:
                _send_session_report("30 minuti scaduti"); _reset_session()
        html = fetch_html("https://www.tracksino.com/crazytime")
        if html:
            val, res = extract_tracksino(html)
            if val is not None: process_spin(val, res)
        time.sleep(SCAN_INTERVAL)

app = Flask(__name__)
@app.route("/")
@app.route("/ping")
def health(): return "OK", 200

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
