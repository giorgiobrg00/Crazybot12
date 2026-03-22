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

SCAN_INTERVAL = 15    # secondi tra una scansione e l'altra
FAIL_TRIGGER  = 2     # cicli base falliti consecutivi per attivare sessione
SESSION_MAX   = 6     # cicli massimi per sessione
MAX_ERRORS    = 3     # errori consecutivi prima di inviare avviso

STATE_FILE = "session_state.json"

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_8 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.8 Mobile/15E148 Safari/604.1",
]

# ─── STATO GLOBALE ──────────────────────────────────────────────────────────────

state = {
    "running":            True,
    "last_update":        None,
    "spin_history":       [],

    "prev_spins_since":   None,

    # Fasi interne: 0=attesa 2 | 1=2 uscito, aspetto puntata 1 | 2=puntata 1 persa, aspetto puntata 2
    "inner_phase":        0,

    # Modalità: "observing" (silenziosa) | "session" (attiva)
    "mode":               "observing",

    "cycles_failed":      0,   # cicli base falliti consecutivi in osservazione
    "session_cycles":     0,   # cicli completati nella sessione corrente
    "session_losses":     0,   # perdite nella sessione corrente
    "session_start_time": None,

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
        "mode":               state["mode"],
        "inner_phase":        state["inner_phase"],
        "cycles_failed":      state["cycles_failed"],
        "prev_spins_since":   state["prev_spins_since"],
        "session_cycles":     state["session_cycles"],
        "session_losses":     state["session_losses"],
        "session_start_time": state["session_start_time"].isoformat() if state["session_start_time"] else None,
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
        logger.info(f"Stato caricato: mode={state['mode']}")
    except Exception as e:
        logger.warning(f"load_state errore: {e}")

# ─── PROXY ─────────────────────────────────────────────────────────────────────

def get_proxy():
    host = random.choice(PROXY_HOSTS)
    url  = f"http://{PROXY_USER}:{PROXY_PASS}@{host}"
    return {"http": url, "https": url}

def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer":    "https://www.google.com/",
        "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    }

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            logger.info(f"Telegram inviato: {text[:80]}")
        else:
            logger.warning(f"Telegram error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.error(f"Telegram eccezione: {e}")

# ─── FETCH HTML ────────────────────────────────────────────────────────────────

def fetch_html(url: str):
    try:
        r = requests.get(
            url,
            headers=get_headers(),
            proxies=get_proxy(),
            verify=False,
            timeout=20,
        )
        if r.status_code == 200:
            return r.text
        logger.warning(f"HTTP {r.status_code} da {url}")
    except Exception as e:
        logger.warning(f"Errore fetch {url}: {e}")
    return None

# ─── ESTRATTORI ─────────────────────────────────────────────────────────────────

def extract_tracksino(html: str):
    soup = BeautifulSoup(html, "lxml")
    spins_since_2 = None
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

        if val == 0:
            name = re.sub(r'(?i)crazy\s*time\s*', '', alt)
            name = re.sub(r'(?i)\s*segment\s*', '', name).strip()
            last_result = name if name else alt

    return spins_since_2, last_result


def extract_casinonews_machineSlot(html: str, source_url: str):
    soup = BeautifulSoup(html, "lxml")

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            for i, cell in enumerate(cells):
                txt = cell.get_text(strip=True)
                if re.fullmatch(r'2|Number\s*2|Num\.?\s*2', txt, re.IGNORECASE):
                    for j in range(i + 1, min(i + 4, len(cells))):
                        candidate = cells[j].get_text(strip=True)
                        m = re.search(r'(\d+)', candidate)
                        if m:
                            return int(m.group(1))

    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue
        patterns = [
            r'"2"\s*:\s*(\d+)',
            r"'2'\s*:\s*(\d+)",
            r'"label"\s*:\s*"2"\s*,\s*"(?:count|value|frequency|total|spins_since|spinsSince)"\s*:\s*(\d+)',
            r'"(?:count|value|frequency|total|spins_since|spinsSince)"\s*:\s*(\d+)\s*,\s*"label"\s*:\s*"2"',
            r'"segment"\s*:\s*"2"\s*,\s*"(?:count|occurrences|hits|spins_since)"\s*:\s*(\d+)',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return int(m.group(1))

    label_re = re.compile(r'^(2|Number\s*2|Num\.?\s*2)$', re.IGNORECASE)
    for tag in soup.find_all(["span", "div", "p", "li", "td"]):
        txt = tag.get_text(strip=True)
        if label_re.match(txt):
            parent = tag.parent
            if parent:
                sibs = list(parent.children)
                try:
                    idx = sibs.index(tag)
                except ValueError:
                    continue
                for sib in sibs[idx + 1:]:
                    sib_txt = getattr(sib, "get_text", lambda strip=False: str(sib))(strip=True)
                    m = re.search(r'(\d+)', sib_txt)
                    if m:
                        return int(m.group(1))

    full_text = soup.get_text(" ", strip=True)
    bt_patterns = [
        r'(?:Number|Num\.?)?\s*2\s*[:\-–]\s*(\d+)',
        r'(\d+)\s*(?:volte|times|x)\s*(?:il\s*|the\s*)?2\b',
        r'\b2\b[^\d]{0,20}(\d{1,4})\b',
    ]
    for pat in bt_patterns:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            candidate = int(m.group(1))
            if 0 < candidate < 1000:
                return candidate

    return None


def extract_casinoscores(html: str):
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue
        patterns = [
            r'"2"\s*:\s*\{[^}]*"(?:count|spins_since|spinsSince|frequency)"\s*:\s*(\d+)',
            r'"label"\s*:\s*"2"[^}]*"(?:count|spins_since|frequency)"\s*:\s*(\d+)',
            r'"segment"\s*:\s*"2"[^}]*"(?:count|spins_since)"\s*:\s*(\d+)',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return int(m.group(1))

        json_blocks = re.findall(r'\{[^{}]{10,500}\}', text)
        for block in json_blocks:
            if '"2"' in block or "'2'" in block:
                for sub_pat in [r'"(?:count|spins_since|frequency)"\s*:\s*(\d+)']:
                    m = re.search(sub_pat, block)
                    if m:
                        return int(m.group(1))

    return None

# ─── ORCHESTRAZIONE SORGENTI ────────────────────────────────────────────────────

def scrape_all_sources():
    html = fetch_html("https://www.tracksino.com/crazytime")
    if html:
        try:
            val, last_result = extract_tracksino(html)
        except Exception as e:
            logger.warning(f"[tracksino] Errore estrazione: {e}")
            val, last_result = None, None
        if val is not None:
            logger.info(f"[tracksino] Valore '2': {val}")
            if last_result:
                logger.info(f"[tracksino] Ultimo risultato: {last_result}")
            state["last_source"] = "tracksino"
            return val, last_result
        else:
            logger.warning("[tracksino] Nessun dato '2' trovato")
    else:
        logger.warning("[tracksino] Impossibile scaricare la pagina")

    fallbacks = [
        ("https://casinoscores.com/crazy-time/",                extract_casinoscores,          "casinoscores"),
        ("https://www.casinonews.it/crazy-time-tracker/",       extract_casinonews_machineSlot, "casinonews"),
        ("https://www.machineslotonline.it/crazy-time-tracker/", extract_casinonews_machineSlot, "machineslotonline"),
    ]
    for url, extractor, name in fallbacks:
        html = fetch_html(url)
        if not html:
            logger.warning(f"[{name}] Impossibile scaricare {url}")
            continue
        try:
            val = extractor(html, url) if name in ("casinonews", "machineslotonline") else extractor(html)
        except Exception as e:
            logger.warning(f"[{name}] Errore estrazione: {e}")
            val = None
        if val is not None:
            logger.info(f"[{name}] Valore '2': {val}")
            state["last_source"] = name
            return val, None
        else:
            logger.warning(f"[{name}] Nessun dato '2' trovato")

    return None, None

# ─── GESTIONE MODALITÀ ──────────────────────────────────────────────────────────

def _return_to_observing(reason: str):
    """Torna in osservazione e resetta tutto."""
    state["mode"]               = "observing"
    state["inner_phase"]        = 0
    state["cycles_failed"]      = 0
    state["session_cycles"]     = 0
    state["session_losses"]     = 0
    state["session_start_time"] = None
    save_state()
    logger.info(f"Tornato in osservazione: {reason}")
    send_telegram(
        f"<b>Sessione terminata</b> ({reason})\n"
        f"Torno in osservazione. Attendo 2 cicli falliti consecutivi.\n"
        f"<b>{datetime.now().strftime('%H:%M:%S')}</b>"
    )


def _enter_session():
    """Attiva la sessione dopo 2 cicli base falliti consecutivi."""
    state["mode"]               = "session"
    state["inner_phase"]        = 0
    state["session_cycles"]     = 0
    state["session_losses"]     = 0
    state["session_start_time"] = datetime.now()
    state["cycles_failed"]      = 0
    save_state()
    send_telegram(
        f"⚠️NUOVA SESSIONE AVVIATA\n"
        f"Segnalo i prossimi {SESSION_MAX} cicli di uscita del 2\n"
        f"<b>{datetime.now().strftime('%H:%M:%S')}</b>"
    )
    logger.info("Sessione attivata")

# ─── LOGICA CICLO ───────────────────────────────────────────────────────────────

def _handle_cycle_win(puntata: int):
    """Vincita: interrompe la sessione e torna immediatamente in osservazione."""
    if state["mode"] == "session":
        send_telegram(
            f"✅️VINTO! Puntata {puntata} – Profitto incassato.\n"
            f"<b>{datetime.now().strftime('%H:%M:%S')}</b>"
        )
        _return_to_observing("vincita")
    else:
        state["cycles_failed"] = 0
        state["inner_phase"]   = 0
        save_state()
        logger.info(f"Vincita in osservazione al colpo {puntata} → cicli_falliti azzerati")


def _handle_cycle_fail(last_result):
    """Ciclo fallito: il 2 non è uscito né in puntata 1 né in puntata 2."""
    state["inner_phase"] = 0
    result_str = str(last_result) if last_result else "sconosciuto"

    if state["mode"] == "observing":
        state["cycles_failed"] += 1
        logger.info(f"Ciclo Base FALLITO | consecutivi: {state['cycles_failed']}/{FAIL_TRIGGER}")
        send_telegram(
            f"<b>Ciclo Base fallito</b> (2-X-X)\n"
            f"Consecutivi: <b>{state['cycles_failed']}/{FAIL_TRIGGER}</b>\n"
            f"<b>{datetime.now().strftime('%H:%M:%S')}</b>"
        )
        if state["cycles_failed"] >= FAIL_TRIGGER:
            _enter_session()
        else:
            save_state()

    else:  # mode == "session"
        state["session_losses"] += 1
        save_state()
        logger.info(
            f"Ciclo {state['session_cycles']}/{SESSION_MAX} PERDITA "
            f"| perdite sessione: {state['session_losses']}"
        )
        send_telegram(
            f"PERDITA (E' uscito: {result_str})\n"
            f"Ciclo {state['session_cycles']}/{SESSION_MAX} terminato.\n"
            f"<b>{datetime.now().strftime('%H:%M:%S')}</b>"
        )


def process_spin(spins_since: int, last_result):
    """
    Macchina a stati principale.

    Phase 0: attesa del 2
    Phase 1: 2 uscito, aspetto puntata 1 (giro successivo)
    Phase 2: puntata 1 persa, aspetto puntata 2 (secondo giro successivo)
    """
    prev = state["prev_spins_since"]
    state["prev_spins_since"] = spins_since

    if last_result:
        state["last_result"] = last_result

    if prev is None:
        logger.info(f"Primo valore: spins_since={spins_since} | avvio osservazione")
        save_state()
        return

    if spins_since == prev:
        logger.debug(f"spins_since invariato ({spins_since}) → skip")
        return

    appeared = spins_since < prev

    logger.info(
        f"spins_since: {prev}→{spins_since} | uscito={appeared} | "
        f"mode={state['mode']} | phase={state['inner_phase']} | "
        f"cicli_falliti={state['cycles_failed']} | sess_cicli={state['session_cycles']}"
    )

    phase = state["inner_phase"]

    # ── Phase 0: attesa del 2 ────────────────────────────────────────────────
    if phase == 0:
        if appeared:
            if state["mode"] == "session" and state["session_cycles"] >= SESSION_MAX:
                _return_to_observing(f"{SESSION_MAX} cicli completati senza vincita")
                return

            state["inner_phase"] = 1
            state["last_result"] = None

            if state["mode"] == "session":
                state["session_cycles"] += 1
                save_state()
                send_telegram(
                    f"<b>Ciclo {state['session_cycles']}/{SESSION_MAX}</b> – Il 2 e' uscito!\n"
                    f"Puntata 1 in gioco...\n"
                    f"<b>{datetime.now().strftime('%H:%M:%S')}</b>"
                )
            else:
                save_state()
                logger.info("2 uscito in osservazione → phase=1")

    # ── Phase 1: puntata 1 (giro subito dopo il 2) ───────────────────────────
    elif phase == 1:
        if appeared:
            _handle_cycle_win(puntata=1)
        else:
            state["inner_phase"] = 2
            used_result = last_result or state.get("last_result")
            if state["mode"] == "session":
                save_state()
                msg = f"<b>Puntata 1 mancata</b> – Ciclo {state['session_cycles']}/{SESSION_MAX}\n"
                if used_result:
                    msg += f"E' uscito: <b>{used_result}</b>\n"
                msg += (
                    f"Puntata 2 in gioco...\n"
                    f"<b>{datetime.now().strftime('%H:%M:%S')}</b>"
                )
                send_telegram(msg)
            else:
                save_state()
                logger.info("Puntata 1 persa in osservazione → phase=2")

    # ── Phase 2: puntata 2 (secondo giro dopo il 2) ──────────────────────────
    elif phase == 2:
        if appeared:
            _handle_cycle_win(puntata=2)
        else:
            used_result = last_result or state.get("last_result")
            _handle_cycle_fail(used_result)

# ─── LOOP PRINCIPALE ────────────────────────────────────────────────────────────

def bot_loop():
    load_state()
    logger.info("Bot Crazy Time avviato!")

    send_telegram(
        f"<b>Bot Crazy Time Tracker AVVIATO</b>\n"
        f"Scansione ogni {SCAN_INTERVAL}s | Trigger: {FAIL_TRIGGER} cicli falliti\n"
        f"Sessione: max {SESSION_MAX} cicli\n"
        f"<b>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</b>"
    )

    while state["running"]:
        state["total_cycles"] += 1
        state["last_update"]  = datetime.now().isoformat()

        try:
            value, last_result = scrape_all_sources()
            if value is not None:
                state["consecutive_errors"] = 0
                state["sos_sent"]           = False
                state["last_spins_since"]   = value
                state["spin_history"].append({
                    "ts":          state["last_update"],
                    "spins_since": value,
                    "source":      state["last_source"],
                    "last_result": last_result,
                })
                state["spin_history"] = state["spin_history"][-200:]
                process_spin(value, last_result)
            else:
                state["consecutive_errors"] += 1
                logger.error(
                    f"Nessun dato valido. Errori consecutivi: {state['consecutive_errors']}/{MAX_ERRORS}"
                )
                if state["consecutive_errors"] >= MAX_ERRORS and not state["sos_sent"]:
                    send_telegram(
                        f"<b>ERRORE TRACCIAMENTO</b>\n"
                        f"Nessun dato valido da {MAX_ERRORS} tentativi consecutivi.\n"
                        f"Controlla le sorgenti e la connessione proxy.\n"
                        f"<b>{datetime.now().strftime('%H:%M:%S')}</b>"
                    )
                    state["sos_sent"] = True

        except Exception as e:
            state["consecutive_errors"] += 1
            logger.exception(f"Errore imprevisto nel loop: {e}")

        time.sleep(SCAN_INTERVAL)

# ─── FLASK WEB SERVER ────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def index():
    elapsed = None
    if state["session_start_time"]:
        elapsed = int((datetime.now() - state["session_start_time"]).total_seconds())
    return jsonify({
        "status":            "running",
        "bot":               "Crazy Time Tracker v5",
        "total_cycles":      state["total_cycles"],
        "last_update":       state["last_update"],
        "mode":              state["mode"],
        "inner_phase":       state["inner_phase"],
        "cycles_failed":     state["cycles_failed"],
        "fail_trigger":      FAIL_TRIGGER,
        "session_cycles":    state["session_cycles"],
        "session_max":       SESSION_MAX,
        "session_losses":    state["session_losses"],
        "session_elapsed_s": elapsed,
        "consecutive_errors": state["consecutive_errors"],
        "last_source":       state["last_source"],
        "last_spins_since":  state["last_spins_since"],
        "last_result":       state["last_result"],
        "spin_history_len":  len(state["spin_history"]),
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

# ─── ENTRY POINT ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    logger.info(f"Flask web server in ascolto su porta {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)