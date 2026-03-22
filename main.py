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

PROXY_HOSTS = [
    "64.137.96.74:6641", "31.59.20.176:6754", "23.95.150.145:6114",
    "198.23.239.134:6540", "45.38.107.97:6014", "107.172.163.27:6543",
    "198.105.121.200:6462", "216.10.27.159:6837", "142.111.67.146:5611",
    "191.96.254.138:6185"
]
PROXY_USER = "mirintel"
PROXY_PASS = "xz2xcafofyvd"

SCAN_INTERVAL      = 15     
FAIL_TRIGGER       = 2      
SESSION_MAX        = 6      
SESSION_DURATION   = 1800   # 30 minuti
SESSION_WIN_LIMIT  = 6      
SESSION_LOSS_LIMIT = 3      
STATE_FILE         = "session_state.json"

# ─── STATO GLOBALE ──────────────────────────────────────────────────────────────
state = {
    "running": True, "last_update": None, "spin_history": [], "prev_spins_since": None,
    "inner_phase": 0, "mode": "observing", "cycles_failed": 0, "session_cycles": 0,
    "session_wins": 0, "session_losses": 0, "session_consec_losses": 0,
    "session_max_consec_losses": 0, "session_start_time": None, "last_source": None,
    "last_result": None
}

# ─── PERSISTENZA ───────────────────────────────────────────────────────────────
def save_state():
    data = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in state.items() if k != 'running'}
    try:
        with open(STATE_FILE, "w") as f: json.dump(data, f)
    except Exception: pass

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
                state.update(data)
                if state["session_start_time"] and isinstance(state["session_start_time"], str):
                    state["session_start_time"] = datetime.fromisoformat(state["session_start_time"])
        except Exception: pass

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception: pass

# ─── GESTIONE SESSIONE ──────────────────────────────────────────────────────────
def _send_session_report(reason: str):
    send_telegram(
        f"🏁 <b>SESSIONE TERMINATA</b> ({reason})\n"
        f"Vincite Totali: <b>{state['session_wins']}</b>\n"
        f"Sconfitte Totali: <b>{state['session_losses']}</b>\n"
        f"Max Sconfitte Consecutive: <b>{state['session_max_consec_losses']}</b>\n"
        f"🕒 {datetime.now().strftime('%H:%M:%S')}"
    )

def _reset_session():
    state.update({"session_cycles": 0, "session_wins": 0, "session_losses": 0, "session_consec_losses": 0, "session_max_consec_losses": 0, "session_start_time": datetime.now(), "inner_phase": 0})
    save_state()
    send_telegram(f"🔄 <b>NUOVA SESSIONE AVVIATA</b>\nSegnalo i prossimi {SESSION_MAX} cicli.\n🕒 {datetime.now().strftime('%H:%M:%S')}")

# ─── LOGICA CORE ───────────────────────────────────────────────────────────────
def _handle_cycle_win(puntata: int):
    if state["mode"] == "session":
        state["session_wins"] += 1
        state["session_consec_losses"] = 0
        save_state()
        send_telegram(f"🎯 <b>VINTO!</b> Puntata {puntata} – Profitto incassato.\n🕒 {datetime.now().strftime('%H:%M:%S')}")
        if state["session_wins"] >= SESSION_WIN_LIMIT:
            _send_session_report(f"{SESSION_WIN_LIMIT} vincite raggiunte")
            _reset_session()
        else:
            state["inner_phase"] = 0
            save_state()
    else:
        state.update({"cycles_failed": 0, "inner_phase": 0})
        save_state()

def _handle_cycle_fail(last_result: str | None):
    state["inner_phase"] = 0
    res_text = f"È uscito: <b>{last_result}</b>" if last_result else "risultato non disponibile"
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
        save_state()
        send_telegram(f"❌ <b>PERDITA</b> ({res_text})\nCiclo {state['session_cycles']}/{SESSION_MAX} terminato.\n🕒 {datetime.now().strftime('%H:%M:%S')}")
        if state["session_losses"] >= SESSION_LOSS_LIMIT:
            _send_session_report(f"{SESSION_LOSS_LIMIT} sconfitte raggiunte")
            _reset_session()

def process_spin(spins_since: int, last_result: str | None):
    prev = state["prev_spins_since"]
    state["prev_spins_since"] = spins_since
    if last_result: state["last_result"] = last_result
    if prev is None or spins_since == prev: return
    
    appeared = spins_since < prev
    phase = state["inner_phase"]

    if phase == 0 and appeared:
        state["inner_phase"] = 1
        state["last_result"] = None
        if state["mode"] == "session":
            state["session_cycles"] += 1
            save_state()
            send_telegram(f"🎯 <b>Ciclo {state['session_cycles']}/{SESSION_MAX}</b> – Il 2 è uscito!\nPuntata 1 in gioco...\n🕒 {datetime.now().strftime('%H:%M:%S')}")
        else:
            save_state()

    elif phase == 1:
        if appeared: _handle_cycle_win(1)
        else:
            state["inner_phase"] = 2
            res = last_result or state.get("last_result")
            if state["mode"] == "session":
                msg = f"🔄 <b>Puntata 1 mancata</b> – Ciclo {state['session_cycles']}/{SESSION_MAX}\n"
                if res: msg += f"È uscito: <b>{res}</b>\n"
                msg += f"Puntata 2 in gioco...\n🕒 {datetime.now().strftime('%H:%M:%S')}"
                send_telegram(msg)
            save_state()

    elif phase == 2:
        if appeared: _handle_cycle_win(2)
        else: _handle_cycle_fail(last_result or state.get("last_result"))

# ─── LOOP PRINCIPALE & FLASK ───────────────────────────────────────────────────
def bot_loop():
    load_state()
    send_telegram(f"🤖 <b>Bot Tracker AVVIATO</b>\n🕒 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    while state["running"]:
        if state["mode"] == "session" and state["session_start_time"]:
            if (datetime.now() - state["session_start_time"]).total_seconds() >= SESSION_DURATION:
                _send_session_report("30 minuti scaduti")
                _reset_session()
        # Qui andrebbe la tua funzione di scraping esistente
        time.sleep(SCAN_INTERVAL)

app = Flask(__name__)
@app.route("/")
def index(): return jsonify({"status": "running", "mode": state["mode"]})

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    
    # AGGIORNAMENTO PORTA 10000 PER RENDER
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
