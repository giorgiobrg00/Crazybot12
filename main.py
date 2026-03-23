import os
import re
import json
import time
import random
import threading
import warnings
import logging
from datetime import datetime
from typing import Optional, Tuple

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

PROXY_USER = "mirintel"
PROXY_PASS = "xz2xcafofyvd"
PROXY_HOSTS = [
    "31.59.20.176:6754",
    "23.95.150.145:6114",
    "198.23.239.134:6540",
    "45.38.107.97:6014",
    "107.172.163.27:6543",
    "198.105.121.200:6462",
    "64.137.96.74:6641",
    "216.10.27.159:6837",
    "142.111.67.146:5611",
    "191.96.254.138:6185",
]

SCAN_INTERVAL  = 15
FAIL_TRIGGER   = 2
SESSION_MAX    = 6
MAX_ERRORS     = 3
FETCH_RETRIES  = 3

STATE_FILE = "session_state.json"

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_8 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.8 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

# ─── STATO GLOBALE ─────────────────────────────────────────────────────────────

state = {
    "running":              True,
    "last_update":          None,
    "spin_history":         [],
    "prev_spins_since_2":   None,
    "prev_spins_since_1":   None,
    "inner_phase":          0,
    "mode":                 "observing",
    "cycles_failed":        0,
    "session_cycles":       0,
    "session_losses":       0,
    "session_start_time":   None,
    "consecutive_errors":   0,
    "sos_sent":             False,
    "total_cycles":         0,
    "last_source":          None,
    "last_spins_since":     None,
    "last_result":          None,
}

# ─── PERSISTENZA STATO ─────────────────────────────────────────────────────────

def save_state():
    data = {
        "mode":                state["mode"],
        "inner_phase":         state["inner_phase"],
        "cycles_failed":       state["cycles_failed"],
        "prev_spins_since_2":  state["prev_spins_since_2"],
        "prev_spins_since_1":  state["prev_spins_since_1"],
        "session_cycles":      state["session_cycles"],
        "session_losses":      state["session_losses"],
        "session_start_time":  state["session_start_time"].isoformat() if state["session_start_time"] else None,
    }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning("save_state errore: %s", e)


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
        logger.info("Stato caricato: mode=%s", state["mode"])
    except Exception as e:
        logger.warning("load_state errore: %s", e)

# ─── PROXY ─────────────────────────────────────────────────────────────────────

def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer":    "https://www.google.com/",
        "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(text):
    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            logger.info("Telegram inviato: %s", text[:80])
        else:
            logger.warning("Telegram error %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.error("Telegram eccezione: %s", e)

# ─── FETCH HTML con retry ──────────────────────────────────────────────────────

def fetch_html(url):
    # type: (str) -> Optional[str]
    used_proxies = set()

    for attempt in range(1, FETCH_RETRIES + 1):
        available = [h for h in PROXY_HOSTS if h not in used_proxies]
        if not available:
            available = PROXY_HOSTS
        host = random.choice(available)
        used_proxies.add(host)
        proxy_url = "http://{}:{}@{}".format(PROXY_USER, PROXY_PASS, host)
        proxies   = {"http": proxy_url, "https": proxy_url}

        try:
            r = requests.get(url, headers=get_headers(), proxies=proxies,
                             verify=False, timeout=20)
            if r.status_code == 200:
                logger.info("[fetch] OK via proxy %s (tentativo %d)", host, attempt)
                return r.text
            logger.warning("[fetch] HTTP %s proxy %s tentativo %d", r.status_code, host, attempt)
        except Exception as e:
            logger.warning("[fetch] Errore proxy %s tentativo %d: %s", host, attempt, e)

        time.sleep(1)

    # Ultimo tentativo senza proxy
    try:
        logger.info("[fetch] Tentativo diretto senza proxy per %s", url)
        r = requests.get(url, headers=get_headers(), verify=False, timeout=25)
        if r.status_code == 200:
            logger.info("[fetch] OK connessione diretta")
            return r.text
        logger.warning("[fetch] HTTP %s connessione diretta", r.status_code)
    except Exception as e:
        logger.warning("[fetch] Errore connessione diretta: %s", e)

    return None

# ─── ESTRATTORI ────────────────────────────────────────────────────────────────

def extract_tracksino(html):
    # type: (str) -> Tuple[Optional[int], Optional[int], Optional[str]]
    soup = BeautifulSoup(html, "lxml")
    spins_since_2 = None
    spins_since_1 = None
    last_result   = None

    for seg in soup.find_all(class_=re.compile(r"game-stats-seg")):
        img = seg.find("img", alt=re.compile(r"Crazy Time", re.IGNORECASE))
        if not img:
            continue
        alt  = img.get("alt", "")
        text = seg.get_text(" ", strip=True)
        m = re.search(r'\)\s*(\d+)\s+spins?\s+since', text)
        if not m:
            m = re.search(r'[\d.]+%\s*\([^)]+\)\s*(\d+)', text)
        if not m:
            continue
        val = int(m.group(1))
        if re.search(r'Crazy Time 2 Segment', alt, re.IGNORECASE):
            spins_since_2 = val
        if re.search(r'Crazy Time 1 Segment', alt, re.IGNORECASE):
            spins_since_1 = val
        if val == 0:
            name = re.sub(r'(?i)crazy\s*time\s*', '', alt)
            name = re.sub(r'(?i)\s*segment\s*', '', name).strip()
            last_result = name if name else alt

    return spins_since_2, spins_since_1, last_result


def extract_casinoscores(html):
    # type: (str) -> Tuple[Optional[int], Optional[int]]
    soup = BeautifulSoup(html, "lxml")
    results = {}

    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue
        for seg in ("1", "2"):
            if seg in results:
                continue
            patterns = [
                r'"' + seg + r'"\s*:\s*\{[^}]*"(?:count|spins_since|spinsSince|frequency)"\s*:\s*(\d+)',
                r'"label"\s*:\s*"' + seg + r'"[^}]*"(?:count|spins_since|frequency)"\s*:\s*(\d+)',
                r'"segment"\s*:\s*"' + seg + r'"[^}]*"(?:count|spins_since)"\s*:\s*(\d+)',
            ]
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    results[seg] = int(m.group(1))
                    break

        if len(results) < 2:
            json_blocks = re.findall(r'\{[^{}]{10,500}\}', text)
            for block in json_blocks:
                for seg in ("1", "2"):
                    if seg in results:
                        continue
                    if ('"' + seg + '"') in block or ("'" + seg + "'") in block:
                        m = re.search(r'"(?:count|spins_since|frequency)"\s*:\s*(\d+)', block)
                        if m:
                            results[seg] = int(m.group(1))

        if "1" in results and "2" in results:
            break

    return results.get("2"), results.get("1")

# ─── ORCHESTRAZIONE SORGENTI ───────────────────────────────────────────────────

def scrape_all_sources():
    # Sorgente primaria: tracksino
    html = fetch_html("https://www.tracksino.com/crazytime")
    if html:
        try:
            val2, val1, last_result = extract_tracksino(html)
        except Exception as e:
            logger.warning("[tracksino] Errore estrazione: %s", e)
            val2, val1, last_result = None, None, None
        if val2 is not None:
            logger.info("[tracksino] spins_since_2=%s | spins_since_1=%s", val2, val1)
            if last_result:
                logger.info("[tracksino] Ultimo risultato: %s", last_result)
            state["last_source"] = "tracksino"
            return val2, val1, last_result
        else:
            logger.warning("[tracksino] Nessun dato '2' trovato nell'HTML")
    else:
        logger.warning("[tracksino] Impossibile scaricare la pagina")

    # Sorgente di riserva: casinoscores
    html = fetch_html("https://casinoscores.com/crazy-time/")
    if html:
        try:
            val2, val1 = extract_casinoscores(html)
        except Exception as e:
            logger.warning("[casinoscores] Errore estrazione: %s", e)
            val2, val1 = None, None
        if val2 is not None:
            logger.info("[casinoscores] spins_since_2=%s | spins_since_1=%s", val2, val1)
            state["last_source"] = "casinoscores"
            return val2, val1, None
        else:
            logger.warning("[casinoscores] Nessun dato '2' trovato")
    else:
        logger.warning("[casinoscores] Impossibile scaricare la pagina")

    return None, None, None

# ─── GESTIONE MODALITÀ ─────────────────────────────────────────────────────────

def _return_to_observing(reason):
    state["mode"]               = "observing"
    state["inner_phase"]        = 0
    state["cycles_failed"]      = 0
    state["session_cycles"]     = 0
    state["session_losses"]     = 0
    state["session_start_time"] = None
    save_state()
    logger.info("Tornato in osservazione: %s", reason)
    send_telegram(
        "<b>Sessione terminata</b> ({})\n"
        "Torno in osservazione. Attendo 2 cicli falliti consecutivi.\n"
        "<b>{}</b>".format(reason, datetime.now().strftime("%H:%M:%S"))
    )


def _enter_session():
    state["mode"]               = "session"
    state["inner_phase"]        = 0
    state["session_cycles"]     = 0
    state["session_losses"]     = 0
    state["session_start_time"] = datetime.now()
    state["cycles_failed"]      = 0
    save_state()
    send_telegram(
        "⚠️NUOVA SESSIONE AVVIATA\n"
        "Segnalo i prossimi {} cicli di uscita del 2\n"
        "<b>{}</b>".format(SESSION_MAX, datetime.now().strftime("%H:%M:%S"))
    )
    logger.info("Sessione attivata")

# ─── LOGICA CICLO ──────────────────────────────────────────────────────────────

def _handle_cycle_win(puntata):
    if state["mode"] == "session":
        send_telegram(
            "✅️VINTO! Puntata {} – Profitto incassato.\n"
            "<b>{}</b>".format(puntata, datetime.now().strftime("%H:%M:%S"))
        )
        _return_to_observing("vincita")
    else:
        state["cycles_failed"] = 0
        state["inner_phase"]   = 0
        save_state()
        logger.info("Vincita in osservazione al colpo %d → cicli_falliti azzerati", puntata)


def _handle_cycle_fail(last_result):
    state["inner_phase"] = 0
    result_str = str(last_result) if last_result else "sconosciuto"

    if state["mode"] == "observing":
        state["cycles_failed"] += 1
        logger.info("Ciclo Base FALLITO | consecutivi: %d/%d",
                    state["cycles_failed"], FAIL_TRIGGER)
        send_telegram(
            "<b>Ciclo Base fallito</b> (2-X-X)\n"
            "Consecutivi: <b>{}/{}</b>\n"
            "<b>{}</b>".format(state["cycles_failed"], FAIL_TRIGGER,
                               datetime.now().strftime("%H:%M:%S"))
        )
        if state["cycles_failed"] >= FAIL_TRIGGER:
            _enter_session()
        else:
            save_state()

    else:
        state["session_losses"] += 1
        save_state()
        logger.info("Ciclo %d/%d PERDITA | perdite sessione: %d",
                    state["session_cycles"], SESSION_MAX, state["session_losses"])
        send_telegram(
            "PERDITA (E' uscito: {})\n"
            "Ciclo {}/{} terminato.\n"
            "<b>{}</b>".format(result_str, state["session_cycles"], SESSION_MAX,
                               datetime.now().strftime("%H:%M:%S"))
        )


def process_spin(spins_since_2, spins_since_1, last_result):
    prev_2 = state["prev_spins_since_2"]
    prev_1 = state["prev_spins_since_1"]

    state["prev_spins_since_2"] = spins_since_2
    state["prev_spins_since_1"] = spins_since_1

    if last_result:
        state["last_result"] = last_result

    if prev_2 is None:
        logger.info("Prima lettura: spins_since_2=%s spins_since_1=%s", spins_since_2, spins_since_1)
        save_state()
        return

    # ── Logica differenziale ──────────────────────────────────────────────────
    if spins_since_1 is not None and prev_1 is not None:
        changed_2 = spins_since_2 != prev_2
        changed_1 = spins_since_1 != prev_1

        if not changed_2 and not changed_1:
            logger.debug("Differenziale: nessun cambio (2=%s, 1=%s) → skip",
                         spins_since_2, spins_since_1)
            return

        appeared_2 = (spins_since_2 == 0)
        logger.info("Differenziale: 2: %s→%s | 1: %s→%s | appeared_2=%s | mode=%s | phase=%s",
                    prev_2, spins_since_2, prev_1, spins_since_1,
                    appeared_2, state["mode"], state["inner_phase"])

    else:
        # Fallback logica semplice
        if spins_since_2 == prev_2:
            logger.debug("spins_since_2 invariato (%s) → skip", spins_since_2)
            return
        appeared_2 = spins_since_2 < prev_2
        logger.info("Semplice: 2: %s→%s | appeared_2=%s | mode=%s | phase=%s",
                    prev_2, spins_since_2, appeared_2, state["mode"], state["inner_phase"])

    # ── Macchina a stati ──────────────────────────────────────────────────────
    phase = state["inner_phase"]

    if phase == 0:
        if appeared_2:
            if state["mode"] == "session" and state["session_cycles"] >= SESSION_MAX:
                _return_to_observing("{} cicli completati senza vincita".format(SESSION_MAX))
                return
            state["inner_phase"] = 1
            state["last_result"] = None
            if state["mode"] == "session":
                state["session_cycles"] += 1
                save_state()
                send_telegram(
                    "<b>Ciclo {}/{}</b> – Il 2 e' uscito!\n"
                    "Puntata 1 in gioco...\n"
                    "<b>{}</b>".format(state["session_cycles"], SESSION_MAX,
                                       datetime.now().strftime("%H:%M:%S"))
                )
            else:
                save_state()
                logger.info("2 uscito in osservazione → phase=1")

    elif phase == 1:
        if appeared_2:
            _handle_cycle_win(puntata=1)
        else:
            state["inner_phase"] = 2
            used_result = last_result or state.get("last_result")
            if state["mode"] == "session":
                save_state()
                msg = "<b>Puntata 1 mancata</b> – Ciclo {}/{}\n".format(
                    state["session_cycles"], SESSION_MAX)
                if used_result:
                    msg += "E' uscito: <b>{}</b>\n".format(used_result)
                msg += "Puntata 2 in gioco...\n<b>{}</b>".format(
                    datetime.now().strftime("%H:%M:%S"))
                send_telegram(msg)
            else:
                save_state()
                logger.info("Puntata 1 persa in osservazione → phase=2")

    elif phase == 2:
        if appeared_2:
            _handle_cycle_win(puntata=2)
        else:
            used_result = last_result or state.get("last_result")
            _handle_cycle_fail(used_result)

# ─── LOOP PRINCIPALE ───────────────────────────────────────────────────────────

def bot_loop():
    load_state()
    logger.info("Bot Crazy Time avviato!")

    send_telegram(
        "<b>Bot Crazy Time Tracker AVVIATO</b>\n"
        "Scansione ogni {}s | Trigger: {} cicli falliti\n"
        "Sessione: max {} cicli\n"
        "<b>{}</b>".format(SCAN_INTERVAL, FAIL_TRIGGER, SESSION_MAX,
                           datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    )

    while state["running"]:
        state["total_cycles"] += 1
        state["last_update"]  = datetime.now().isoformat()

        try:
            value2, value1, last_result = scrape_all_sources()
            if value2 is not None:
                state["consecutive_errors"] = 0
                state["sos_sent"]           = False
                state["last_spins_since"]   = value2
                state["spin_history"].append({
                    "ts":            state["last_update"],
                    "spins_since_2": value2,
                    "spins_since_1": value1,
                    "source":        state["last_source"],
                    "last_result":   last_result,
                })
                state["spin_history"] = state["spin_history"][-200:]
                process_spin(value2, value1, last_result)
            else:
                state["consecutive_errors"] += 1
                logger.error("Nessun dato valido. Errori consecutivi: %d/%d",
                             state["consecutive_errors"], MAX_ERRORS)
                if state["consecutive_errors"] >= MAX_ERRORS and not state["sos_sent"]:
                    send_telegram(
                        "<b>ERRORE TRACCIAMENTO</b>\n"
                        "Nessun dato valido da {} tentativi consecutivi.\n"
                        "Controlla le sorgenti e la connessione proxy.\n"
                        "<b>{}</b>".format(MAX_ERRORS, datetime.now().strftime("%H:%M:%S"))
                    )
                    state["sos_sent"] = True

        except Exception as e:
            state["consecutive_errors"] += 1
            logger.exception("Errore imprevisto nel loop: %s", e)

        time.sleep(SCAN_INTERVAL)

# ─── FLASK WEB SERVER ──────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def index():
    elapsed = None
    if state["session_start_time"]:
        elapsed = int((datetime.now() - state["session_start_time"]).total_seconds())
    return jsonify({
        "status":             "running",
        "bot":                "Crazy Time Tracker v5",
        "total_cycles":       state["total_cycles"],
        "last_update":        state["last_update"],
        "mode":               state["mode"],
        "inner_phase":        state["inner_phase"],
        "cycles_failed":      state["cycles_failed"],
        "fail_trigger":       FAIL_TRIGGER,
        "session_cycles":     state["session_cycles"],
        "session_max":        SESSION_MAX,
        "session_losses":     state["session_losses"],
        "session_elapsed_s":  elapsed,
        "consecutive_errors": state["consecutive_errors"],
        "last_source":        state["last_source"],
        "last_spins_since_2": state["last_spins_since"],
        "last_spins_since_1": state["prev_spins_since_1"],
        "last_result":        state["last_result"],
        "spin_history_len":   len(state["spin_history"]),
    })

@app.route("/history")
def history():
    return jsonify({"spin_history": state["spin_history"][-20:]})

@app.route("/ping")
@app.route("/api/ping")
def ping():
    return jsonify({"pong": True, "ts": datetime.now().isoformat()})

@app.route("/health")
@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})

# ─── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    logger.info("Flask web server in ascolto su porta %d", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)