"""
Bot che cerca stanze a Milano sui portali e ti avvisa su Telegram dei nuovi annunci.

Come funziona, in breve:
1. legge da config.json gli URL di ricerca (quelli che copi dal browser con i filtri già impostati)
2. scarica ogni pagina e ne estrae i link agli annunci
3. confronta con seen.json (gli annunci già visti)
4. ti manda su Telegram solo quelli nuovi, poi aggiorna seen.json
"""
import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

CONFIG = json.loads(Path("config.json").read_text(encoding="utf-8"))
STATE_FILE = Path("seen.json")
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Ci presentiamo come un normale browser
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

# Come riconoscere il link di un singolo annuncio su ogni sito
PATTERNS = {
    "immobiliare.it": re.compile(r"/annunci/(\d+)/?$"),
    "idealista.it": re.compile(r"/immobile/(\d+)/?$"),
    "subito.it": re.compile(r"-(\d+)\.htm$"),
}
PRICE = re.compile(r"€\s?\d[\d.]*|\d[\d.]*\s?€")
MAX_MSG = 15      # massimo messaggi per controllo, per non intasarti la chat
FAIL_ALERT = 3    # dopo quanti errori di fila su una fonte ti avviso


def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {}
    state.setdefault("seen", {})
    state.setdefault("fail", {})
    state.setdefault("init", [])
    return state


def save_state(state):
    # tiene in memoria solo gli ultimi 5000 annunci
    state["seen"] = dict(sorted(state["seen"].items(), key=lambda kv: kv[1])[-5000:])
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def send(text):
    if not TOKEN or not CHAT_ID:
        print("[Telegram non configurato]\n" + text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=20,
    )
    if not r.ok:
        print("Errore Telegram:", r.text)
    time.sleep(1)


def site_of(url):
    host = urlsplit(url).netloc.lower()
    for site in PATTERNS:
        if host.endswith(site):
            return site
    return None


def find_price(a):
    """Cerca un prezzo nel riquadro dell'annuncio che contiene il link."""
    node = a
    for _ in range(5):
        node = node.parent
        if node is None:
            break
        text = node.get_text(" ", strip=True)
        if len(text) > 1500:
            break
        m = PRICE.search(text)
        if m:
            return m.group(0)
    return ""


def scrape(search_url):
    site = site_of(search_url)
    if not site:
        raise ValueError("sito non supportato")
    r = requests.get(search_url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"il sito ha risposto con errore HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        link = urljoin(search_url, a["href"]).split("?")[0].split("#")[0]
        if site_of(link) != site:
            continue
        m = PATTERNS[site].search(urlsplit(link).path)
        if not m:
            continue
        key = f"{site}:{m.group(1)}"
        title = a.get_text(" ", strip=True) or a.get("title") or a.get("aria-label") or ""
        if key in found and len(found[key]["title"]) >= len(title):
            continue
        found[key] = {"link": link, "title": title[:150], "price": find_price(a)}
    if not found:
        raise RuntimeError("nessun annuncio trovato (pagina bloccata o cambiata)")
    return found


def main():
    state = load_state()
    excl = [w.lower() for w in CONFIG.get("escludi_parole", [])]
    new = []

    for s in CONFIG["ricerche"]:
        url = s.get("url", "")
        if not url.startswith("http"):
            continue  # ricerca non ancora compilata
        name = s.get("nome", url)
        try:
            results = scrape(url)
            state["fail"].pop(url, None)
            print(f"{name}: {len(results)} annunci trovati")
        except Exception as e:
            n = state["fail"].get(url, 0) + 1
            state["fail"][url] = n
            print(f"{name}: ERRORE - {e} ({n} di fila)")
            if n == FAIL_ALERT:
                send(f"⚠️ <b>{html.escape(name)}</b> non funziona da {n} controlli di fila: "
                     f"{html.escape(str(e))}")
            continue

        # Prima volta che vediamo questa ricerca: memorizza tutto senza avvisare
        if url not in state["init"]:
            for key in results:
                state["seen"].setdefault(key, int(time.time()))
            state["init"].append(url)
            send(f"✅ <b>{html.escape(name)}</b> attiva: memorizzati {len(results)} annunci già "
                 f"online, da ora ti avviso solo dei nuovi.")
            continue

        for key, ad in results.items():
            if key in state["seen"]:
                continue
            state["seen"][key] = int(time.time())
            if any(w in ad["title"].lower() for w in excl):
                continue
            new.append((name, ad))

    for name, ad in new[:MAX_MSG]:
        lines = [f"🏠 <b>{html.escape(ad['title'] or 'Nuovo annuncio')}</b>"]
        if ad["price"]:
            lines.append(f"💶 {html.escape(ad['price'])}")
        lines.append(f"📍 {html.escape(name)}")
        lines.append(ad["link"])
        send("\n".join(lines))
    if len(new) > MAX_MSG:
        send(f"…e altri {len(new) - MAX_MSG} nuovi annunci: dai un'occhiata ai siti.")

    print(f"Nuovi annunci in questo giro: {len(new)}")
    save_state(state)


if __name__ == "__main__":
    main()
