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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─── CONFIGURAZIONE ────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = "8606699249:AAF0Dp3Kfvo0fDmGjSko6zargyoGSLrsblQ"
TELEGRAM_CHAT  = "@segnalicrazy"

PROXY_USER = "gnrzyqfs"
PROXY_PASS = "3lbaq4efyfv5"
PROXY_HOSTS = [
    "23.95.150.145:6114",
    "31.59.20.176:6754",
    "45.38.107.97:6014",
    "64.137.96.74:6641",
]

SCAN_INTERVAL      = 15     
FAIL_TRIGGER       = 2      
SESSION_MAX        = 6      
SESSION_DURATION   = 1800   
SESSION_WIN_LIMIT  = 6      
SESSION_LOSS_LIMIT = 3      
MAX_ERRORS         = 3      

STATE_FILE = "session_state.json"

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
]

# ─── STATO GLOBALE ──────────────────────────────────────────────────────────────

state = {
    "running":            True,
    "last_update":        None,
    "spin_history":       [],
    "prev_spins_since":   None,
    "inner_phase":        0,
    "mode":               "observing",
    "cycles_failed":      0,
    "session_cycles":     0,
    "session_wins":              0,
    "session_losses":            0,
    "session_consec_losses":     0,
    "session_max_consec_losses": 0,
    "session_start_time":        None,
    "consecutive_errors": 0,
    "sos_sent":           False,
    "total_cycles":       0,
    "last_source":        None,
    "last_spins_since":   None,
    "last_result":        None,
}

# ─── PERSISTENZA STATO ──────────────────────────────────────────────────────────

def save_state():
    data = {
        "mode":                      state["mode"],
        "inner_phase":               state["inner_phase"],
        "cycles_failed":             state["cycles_failed"],
        "prev_spins_since":          state["prev_spins_since"],
        "session_cycles":            state["session_cycles"],
        "session_wins":              state["session_wins"],
        "session_losses":            state["session_losses"],
        "session_consec_losses":     state["session_consec_losses"],
        "session_max_consec_losses": state["session_max_consec_losses"],
        "session_start_time":        state["session_start_time"].isoformat() if state["session_start_time"] else None,
    }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"save_state errore: {e}")

def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            if k in state:
                state[k] = v
        if state["session_start_time"] and isinstance(state["session_start_time"], str):
            state["session_start_time"] = datetime.fromisoformat(state["session_start_time"])
    except Exception as e:
        logger.warning(f"load_state errore: {e}")

# ─── PROXY & HEADERS ───────────────────────────────────────────────────────────

def get_proxy():
    host = random.choice(PROXY_HOSTS)
    url  = f"http://{PROXY_USER}:{PROXY_PASS}@{host}"
    return {"http": url, "https": url}

def get_headers():
    return {"User-Agent": random.choice(USER_AGENTS)}

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logger.error(f"Telegram eccezione: {e}")

# ─── FETCH & ESTRATTORI ────────────────────────────────────────────────────────

def fetch_html(url: str) -> str | None:
    try:
        r = requests.get(url, headers=get_headers(), proxies=get_proxy(), verify=False, timeout=20)
        return r.text if r.status_code == 200 else None
    except:
        return None

def extract_tracksino(html: str):
    soup = BeautifulSoup(html, "lxml")
    spins_since_2, last_result = None, None
    for seg in soup.find_all(class_=re.compile(r"game-stats-seg")):
        img = seg.find("img", alt=re.compile(r"Crazy Time", re.IGNORECASE))
        if not img: continue
        alt = img.get("alt", "")
        text = seg.get_text(" ", strip=True)
        m = re.search(r'\)\s*(\d+)\s+spins?\s+since', text)
        if not m: m = re.search(r'[\d.]+%\s*\([^)]+\)\s*(\d+)', text)
        if not m: continue
        val = int(m.group(1))
        if "Crazy Time 2 Segment" in alt: spins_since_2 = val
        if val == 0:
            name = re.sub(r'(?i)crazy\s*time\s*|segment', '', alt).strip()
            last_result = name if name else alt
    return spins_since_2, last_result

def scrape_all_sources():
    html = fetch_html("https://www.tracksino.com/crazytime")
    if html:
        val, last_result = extract_tracksino(html)
        if val is not None:
            state["last_source"] = "tracksino"
            return val, last_result
    return None, None

# ─── GESTIONE SESSIONE ──────────────────────────────────────────────────────────

def _send_session_report(reason: str):
    send_telegram(
        f"🏁 <b>SESSIONE TERMINATA</b> ({reason})\n"
        f"Vincite Totali: <b>{state['session_wins']}</b>\n"
        f"Sconfitte Totali: <b>{state['session_losses']}</b>\n"
        f"Max Sconfitte Consecutive: <b>{state['session_max_consec_losses']}</b>\n"
        f"🕒 {datetime.now().strftime('%H:%M:%S')}"
    )

def _start_new_session():
    state.update({"session_cycles": 0, "session_wins": 0, "session_losses": 0, "session_consec_losses": 0, "session_max_consec_losses": 0, "session_start_time": datetime.now(), "inner_phase": 0})
    save_state()
    send_telegram(f"🔄 <b>NUOVA SESSIONE AVVIATA</b>\nSegnalo i prossimi {SESSION_MAX} cicli.\n🕒 {datetime.now().strftime('%H:%M:%S')}")

def _enter_session():
    state.update({"mode": "session", "session_cycles": 0, "session_wins": 0, "session_losses": 0, "session_consec_losses": 0, "session_max_consec_losses": 0, "session_start_time": datetime.now(), "cycles_failed": 0, "inner_phase": 0})
    save_state()
    send_telegram(f"🚨 <b>ATTIVAZIONE SESSIONE!</b>\n{FAIL_TRIGGER} Cicli Base falliti.\n🕒 {datetime.now().strftime('%H:%M:%S')}")

def _check_time_limit():
    if state["mode"] == "session" and state["session_start_time"]:
        if (datetime.now() - state["session_start_time"]).total_seconds() >= SESSION_DURATION:
            _send_session_report("30 minuti scaduti")
            _start_new_session()

# ─── LOGICA CICLO ───────────────────────────────────────────────────────────────

def _handle_cycle_win(puntata: int):
    if state["mode"] == "session":
        state["session_wins"] += 1
        state["session_consec_losses"] = 0
        save_state()
        send_telegram(f"🎯 <b>VINTO!</b> Puntata {puntata} – Profitto incassato.\n🕒 {datetime.now().strftime('%H:%M:%S')}")
        if state["session_wins"] >= SESSION_WIN_LIMIT:
            _send_session_report(f"{SESSION_WIN_LIMIT} vincite raggiunte")
            _start_new_session()
        else:
            state["inner_phase"] = 0
            save_state()
    else:
        state["cycles_failed"] = 0
        state["inner_phase"] = 0
        save_state()

def _handle_cycle_fail(last_result: str | None):
    state["inner_phase"] = 0
    res_text = f"È uscito: <b>{last_result}</b>" if last_result else "risultato non disponibile"
    if state["mode"] == "observing":
        state["cycles_failed"] += 1
        send_telegram(f"⚠️ <b>Ciclo Base fallito</b>\nConsecutivi: {state['cycles_failed']}/{FAIL_TRIGGER}\n🕒 {datetime.now().strftime('%H:%M:%S')}")
        if state["cycles_failed"] >= FAIL_TRIGGER: _enter_session()
    else:
        state["session_losses"] += 1
        state["session_consec_losses"] += 1
        if state["session_consec_losses"] > state["session_max_consec_losses"]:
            state["session_max_consec_losses"] = state["session_consec_losses"]
        save_state()
        send_telegram(f"❌ <b>PERDITA</b> ({res_text})\nCiclo {state['session_cycles']}/{SESSION_MAX} terminato.\n🕒 {datetime.now().strftime('%H:%M:%S')}")
        if state["session_losses"] >= SESSION_LOSS_LIMIT:
            _send_session_report(f"{SESSION_LOSS_LIMIT} sconfitte raggiunte")
            _start_new_session()

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

# ─── LOOP & FLASK ──────────────────────────────────────────────────────────────

def bot_loop():
    load_state()
    send_telegram(f"🤖 <b>Bot Tracker AVVIATO</b>\n🕒 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    while state["running"]:
        _check_time_limit()
        try:
            val, res = scrape_all_sources()
            if val is not None:
                state.update({"consecutive_errors": 0, "sos_sent": False, "last_spins_since": val})
                process_spin(val, res)
            else:
                state["consecutive_errors"] += 1
                if state["consecutive_errors"] >= MAX_ERRORS and not state["sos_sent"]:
                    send_telegram("🚨 <b>ERRORE TRACCIAMENTO</b>\nSorgenti non raggiungibili.")
                    state["sos_sent"] = True
        except: pass
        time.sleep(SCAN_INTERVAL)

app = Flask(__name__)
@app.route("/")
def index(): return jsonify({"status": "running", "mode": state["mode"], "wins": state["session_wins"], "losses": state["session_losses"]})

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
