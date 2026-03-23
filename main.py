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
    "prev_spins_since_2":   None,   # contatore differenziale segmento 2
    "prev_spins_since_1":   None,   # contatore differenziale segmento 1
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
        "Cache-Control": "no-cache",
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

# ─── FETCH HTML con retry ──────────────────────────────────────────────────────

def fetch_html(url: str) -> str | None:
    used_proxies = set()

    for attempt in range(1, FETCH_RETRIES + 1):
        available = [h for h in PROXY_HOSTS if h not in used_proxies]
        if not available:
            available = PROXY_HOSTS
        host = random.choice(available)
        used_proxies.add(host)
        proxy_url = f"http://{PROXY_USER}:{PROXY_PASS}@{host}"
        proxies   = {"http": proxy_url, "https": proxy_url}

        try:
            r = requests.get(
                url,
                headers=get_headers(),
                proxies=proxies,
                verify=False,
                timeout=20,
            )
            if r.status_code == 200:
                logger.info(f"[fetch] OK via proxy {host} (tentativo {attempt})")
                return r.text
            logger.warning(f"[fetch] HTTP {r.status_code} proxy {host} tentativo {attempt}")
        except Exception as e:
            logger.warning(f"[fetch] Errore proxy {host} tentativo {attempt}: {e}")

        time.sleep(1)

    try:
        logger.info(f"[fetch] Tentativo diretto senza proxy per {url}")
        r = requests.get(url, headers=get_headers(), verify=False, timeout=25)
        if r.status_code == 200:
            logger.info(f"[fetch] OK connessione diretta")
            return r.text
        logger.warning(f"[fetch] HTTP {r.status_code} connessione diretta")
    except Exception as e:
        logger.warning(f"[fetch] Errore connessione diretta: {e}")

    return None

# ─── ESTRATTORI ────────────────────────────────────────────────────────────────

def extract_tracksino(html: str):
    """Restituisce (spins_since_2, spins_since_1, last_result)."""
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


def extract_casinoscores(html: str):
    """Restituisce (spins_since_2, spins_since_1)."""
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
                rf'"{seg}"\s*:\s*\{{[^}}]*"(?:count|spins_since|spinsSince|frequency)"\s*:\s*(\d+)',
                rf'"label"\s*:\s*"{seg}"[^}}]*"(?:count|spins_since|frequency)"\s*:\s*(\d+)',
                rf'"segment"\s*:\s*"{seg}"[^}}]*"(?:count|spins_since)"\s*:\s*(\d+)',
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
                    if f'"{seg}"' in block or f"'{seg}'" in block:
                        m = re.search(r'"(?:count|spins_since|frequency)"\s*:\s*(\d+)', block)
                        if m:
                            results[seg] = int(m.group(1))

        if "1" in results and "2" in results:
            break

    return results.get("2"), results.get("1")

# ─── ORCHESTRAZIONE SORGENTI ───────────────────────────────────────────────────

def scrape_all_sources():
    """Restituisce (spins_since_2, spins_since_1, last_result)."""

    # Sorgente primaria: tracksino
    html = fetch_html("https://www.tracksino.com/crazytime")
    if html:
        try:
            val2, val1, last_result = extract_tracksino(html)
        except Exception as e:
            logger.warning(f"[tracksino] Errore estrazione: {e}")
            val2, val1, last_result = None, None, None
        if val2 is not None:
            logger.info(f"[tracksino] spins_since_2={val2} | spins_since_1={val1}")
            if last_result:
                logger.info(f"[tracksino] Ultimo risultato: {last_result}")
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
            logger.warning(f"[casinoscores] Errore estrazione: {e}")
            val2, val1 = None, None
        if val2 is not None:
            logger.info(f"[casinoscores] spins_since_2={val2} | spins_since_1={val1}")
            state["last_source"] = "casinoscores"
            return val2, val1, None
        else:
            logger.warning("[casinoscores] Nessun dato '2' trovato")
    else:
        logger.warning("[casinoscores] Impossibile scaricare la pagina")

    return None, None, None

# ─── GESTIONE MODALITÀ ─────────────────────────────────────────────────────────

def _return_to_observing(reason: str):
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

# ─── LOGICA CICLO ──────────────────────────────────────────────────────────────

def _handle_cycle_win(puntata: int):
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

    else:
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


def process_spin(spins_since_2: int, spins_since_1, last_result):
    """
    Logica Differenziale Matematica.

    Monitora due segmenti (2 e 1) simultaneamente.
    In ogni giro reale, matematicamente almeno uno dei due contatori cambia.

    Regole di rilevazione:
      - appeared_2 = True  se spins_since_2 == 0  (il 2 è uscito, anche se era già 0 al giro prima)
      - appeared_1 = True  se spins_since_1 == 0  (l'1 è uscito)
      - entrambi aumentano → uscito un altro numero
      - nessun cambiamento → nessun giro nuovo, skip

    Se spins_since_1 non è disponibile, fallback alla logica semplice (solo val_2).
    """
    prev_2 = state["prev_spins_since_2"]
    prev_1 = state["prev_spins_since_1"]

    state["prev_spins_since_2"] = spins_since_2
    state["prev_spins_since_1"] = spins_since_1

    if last_result:
        state["last_result"] = last_result

    # Prima lettura assoluta
    if prev_2 is None:
        logger.info(f"Prima lettura: spins_since_2={spins_since_2} spins_since_1={spins_since_1}")
        save_state()
        return

    # ── Logica differenziale (disponibile solo se abbiamo entrambi i valori) ──
    if spins_since_1 is not None and prev_1 is not None:

        changed_2 = spins_since_2 != prev_2
        changed_1 = spins_since_1 != prev_1

        # Nessun giro rilevato
        if not changed_2 and not changed_1:
            logger.debug(f"Differenziale: nessun cambio (2={spins_since_2}, 1={spins_since_1}) → skip")
            return

        # Il 2 è uscito se spins_since_2 == 0 (indipendentemente dal valore precedente)
        appeared_2 = (spins_since_2 == 0)
        # (conferma di giro garantita dal fatto che almeno uno dei due è cambiato)

        logger.info(
            f"Differenziale: 2: {prev_2}→{spins_since_2} | 1: {prev_1}→{spins_since_1} | "
            f"appeared_2={appeared_2} | mode={state['mode']} | phase={state['inner_phase']}"
        )

    else:
        # ── Fallback: logica semplice se spins_since_1 non disponibile ──
        if spins_since_2 == prev_2:
            logger.debug(f"spins_since_2 invariato ({spins_since_2}) → skip")
            return
        appeared_2 = spins_since_2 < prev_2
        logger.info(
            f"Semplice: 2: {prev_2}→{spins_since_2} | appeared_2={appeared_2} | "
            f"mode={state['mode']} | phase={state['inner_phase']}"
        )

    # ── Macchina a stati (identica alla versione precedente) ──────────────────
    phase = state["inner_phase"]

    if phase == 0:
        if appeared_2:
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

    elif phase == 1:
        if appeared_2:
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
        f"<b>Bot Crazy Time Tracker AVVIATO</b>\n"
        f"Scansione ogni {SCAN_INTERVAL}s | Trigger: {FAIL_TRIGGER} cicli falliti\n"
        f"Sessione: max {SESSION_MAX} cicli\n"
        f"<b>{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</b>"
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

# ─── FLASK WEB SERVER ──────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def index():
    elapsed = None
    if state["session_start_time"]:
        elapsed = int((datetime.now() - state["session_start_time"]).total_seconds())
    return jsonify({
        "status":              "running",
        "bot":                 "Crazy Time Tracker v5",
        "total_cycles":        state["total_cycles"],
        "last_update":         state["last_update"],
        "mode":                state["mode"],
        "inner_phase":         state["inner_phase"],
        "cycles_failed":       state["cycles_failed"],
        "fail_trigger":        FAIL_TRIGGER,
        "session_cycles":      state["session_cycles"],
        "session_max":         SESSION_MAX,
        "session_losses":      state["session_losses"],
        "session_elapsed_s":   elapsed,
        "consecutive_errors":  state["consecutive_errors"],
        "last_source":         state["last_source"],
        "last_spins_since_2":  state["last_spins_since"],
        "last_spins_since_1":  state["prev_spins_since_1"],
        "last_result":         state["last_result"],
        "spin_history_len":    len(state["spin_history"]),
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
    logger.info(f"Flask web server in ascolto su porta {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)