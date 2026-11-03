#!/usr/bin/env python3
"""
Hakee sähkön kokonaiskulutuksen Caruna Plus -palvelusta (päivittäin) ja tallentaa sen
kansioon ~/shelly/datahub/ kuukausittaisina CSV-tiedostoina. shelly_report.py lukee ne
Datahub-tiedostoina, jolloin kokonaiskulutusta ei tarvitse kirjata manual.csv:hen.

Caruna Plus -rajapinta ei ole virallisesti dokumentoitu (perustuu avoimen lähdekoodin
pycaruna- ja caruna-plus-cli-kirjastoihin). Jos Caruna muuttaa palvelua, haku voi lakata
toimimatta; raportti käyttää silloin edelleen manual.csv:n lukuja.

Asetukset tiedostossa ~/shelly/caruna.conf (chmod 600):

    [caruna]
    username = sähköposti@esimerkki.fi
    password = salasana
    # gsrn = 643...   (valinnainen, jos tilillä on useampi käyttöpaikka)

Riippuvuudet: requests ja beautifulsoup4
    sudo apt install python3-requests python3-bs4

Käyttö:
    python3 caruna_fetch.py                  # edellinen ja kuluva kuukausi
    python3 caruna_fetch.py --from 2024-01   # takaisinhaku annetusta kuukaudesta
    python3 caruna_fetch.py --debug          # kirjautumisen vaiheet näkyviin

Caruna päivittää edellisen päivän kulutuksen yleensä seuraavana päivänä klo 15 mennessä.
"""

import argparse
import configparser
import csv
import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    raise SystemExit("Puuttuu kirjasto. Asenna: sudo apt install python3-requests python3-bs4")

BASE = Path.home() / "shelly"
CONF = BASE / "caruna.conf"
OUT_DIR = BASE / "datahub"

BASE_URL = "https://plus.caruna.fi"
API = f"{BASE_URL}/api"
AUTH_URL = "https://authentication2.caruna.fi"
TIMEOUT = 30
DEBUG = False


def log(*a):
    if DEBUG:
        print("  ·", *a)


def soup_of(r):
    return BeautifulSoup(r.content, "html.parser")


def meta_refresh_url(soup):
    """<meta http-equiv="refresh" content="0;URL=/polku"> -> /polku"""
    meta = soup.find("meta", attrs={"http-equiv": lambda v: v and v.lower() == "refresh"}) \
        or soup.find("meta")
    content = meta["content"]
    i = content.lower().find("url=")
    return content[i + 4:] if i >= 0 else content[6:]


def hidden_vars(soup):
    return {i["name"]: i.get("value", "") for i in soup.find_all("input", type="hidden")
            if i.get("name")}


# --- Kirjautuminen -----------------------------------------------------------

def login(username, password):
    """Palauttaa (token, asiakasnumero)."""
    s = requests.Session()
    s.headers["User-Agent"] = "kodin-kulutus/1.0 (+pycaruna-yhteensopiva)"

    r = s.post(f"{API}/authorization/login",
               json={"redirectAfterLogin": BASE_URL, "language": "fi"}, timeout=TIMEOUT)
    r.raise_for_status()
    r = s.get(r.json()["loginRedirectUrl"], timeout=TIMEOUT)
    log("login redirect", r.status_code)

    r = s.get(urljoin(AUTH_URL, meta_refresh_url(soup_of(r))), timeout=TIMEOUT)
    soup = soup_of(r)
    log("kirjautumissivu", r.status_code, r.url)

    # Lomakkeen action muutetaan samoin kuin sivun JavaScript tekisi
    action = soup.find("form")["action"]
    action = action.lstrip(".")[:11] + "0-userIDPanel-usernameLogin-loginWithUserID"
    data = hidden_vars(soup)
    data["ttqusername"] = username
    data["userPassword"] = password
    submit = soup.find("input", type="submit")
    if submit is not None and submit.get("name"):
        data[submit["name"]] = "1"
    r = s.post(AUTH_URL + "/portal" + action, data=data, timeout=TIMEOUT, headers={
        "Wicket-Ajax": "true",
        "Wicket-Ajax-BaseURL": ".",
        "Wicket-FocusedElementId": "loginWithUserID5",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": AUTH_URL,
        "Referer": f"{AUTH_URL}/portal/",
    })
    log("tunnukset lähetetty", r.status_code)

    # Seuraava osoite tulee joko otsakkeessa tai Wicketin CDATA-vastauksessa
    nxt = r.headers.get("Ajax-Location")
    if not nxt and "CDATA[" in r.text:
        t = r.text[r.text.find("CDATA[") + 6:]
        nxt = t[:t.find("]")]
    if not nxt:
        raise SystemExit("Kirjautuminen epäonnistui: tarkista tunnus ja salasana "
                         "(tai Caruna on muuttanut kirjautumista)")
    r = s.get(AUTH_URL + "/portal" + nxt.lstrip("."), timeout=TIMEOUT)

    r = s.get(urljoin(r.url, meta_refresh_url(soup_of(r))), timeout=TIMEOUT)
    soup = soup_of(r)
    form = soup.find("form")
    r = s.post(urljoin(r.url, form["action"]), data=hidden_vars(soup),
               allow_redirects=False, timeout=TIMEOUT)

    # Seurataan uudelleenohjauksia, kunnes vastaan tulee OpenID-paluuosoite (code=…)
    params = None
    for _ in range(10):
        loc = r.headers.get("Location")
        if not loc:
            break
        url = urljoin(r.url, loc)
        q = parse_qs(urlparse(url).query)
        if "code" in q:
            params = {k: q[k][0] for k in ("code", "state", "session_state") if k in q}
            break
        r = s.get(url, allow_redirects=False, timeout=TIMEOUT)
    if params is None:
        q = parse_qs(urlparse(r.url).query)
        if "code" in q:
            params = {k: v[0] for k, v in q.items()}
    if params is None:
        raise SystemExit("Kirjautuminen epäonnistui: OpenID-paluuosoitetta ei löytynyt")
    log("OpenID-parametrit saatu")

    r = s.post(f"{API}/authorization/token", data=params, timeout=TIMEOUT)
    r.raise_for_status()
    info = r.json()
    return info["token"], info["user"]["ownCustomerNumbers"][0]


# --- Data --------------------------------------------------------------------

def api_get(token, path, params=None):
    r = requests.get(f"{API}{path}", params=params, timeout=TIMEOUT,
                     headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def pick_asset(assets, gsrn=None):
    points = [a for a in assets if a.get("type") == "meteringPoint"] or assets
    if gsrn:
        points = [a for a in points if str(a.get("gsrn")) == str(gsrn)]
        if not points:
            raise SystemExit(f"Käyttöpaikkaa GSRN {gsrn} ei löytynyt")
    if len(points) > 1 and not gsrn:
        print("Useita käyttöpaikkoja, käytetään ensimmäistä. Valitse tarvittaessa "
              "gsrn caruna.conf-tiedostossa:")
        for a in points:
            print("   ", a.get("gsrn"), a.get("contractProductDesc", ""))
    return points[0]["assetId"]


def daily_rows(energy):
    """Caruna-vastaus -> [(aikaleima, kWh)] ilman tyhjiä (tulevia) päiviä."""
    rows = []
    for res in energy.get("results", []):
        for d in res.get("data", []):
            v = d.get("consumption")
            if v is None:
                v = d.get("boughtenergy")
            if v is not None and d.get("timestamp"):
                rows.append((d["timestamp"], float(v)))
    return rows


def months_between(first, last):
    y, m = first
    while (y, m) <= last:
        yield y, m
        y, m = (y + (m == 12), m % 12 + 1)


def write_month(y, m, rows):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"caruna_{y}-{m:02d}.csv"
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["aika", "kwh"])
        for ts, v in rows:
            w.writerow([ts, f"{v:.3f}"])
    tmp.replace(path)
    return path


def main():
    global DEBUG
    ap = argparse.ArgumentParser(description="Caruna Plus -kulutushaku")
    ap.add_argument("--from", dest="start", help="ensimmäinen kuukausi VVVV-KK")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    DEBUG = a.debug

    cfg = configparser.ConfigParser()
    if not cfg.read(CONF):
        raise SystemExit(f"Asetustiedosto puuttuu: {CONF}")
    c = cfg["caruna"]

    today = date.today()
    last = (today.year, today.month)
    if a.start:
        first = (int(a.start[:4]), int(a.start[5:7]))
    else:
        first = (today.year - (today.month == 1), (today.month - 2) % 12 + 1)

    token, customer = login(c["username"], c["password"])
    asset = pick_asset(api_get(token, f"/customers/{customer}/assets"), c.get("gsrn"))

    for y, m in months_between(first, last):
        energy = api_get(token, f"/customers/{customer}/assets/{asset}/energy",
                         {"year": y, "month": m, "day": 1, "timespan": "monthly"})
        rows = daily_rows(energy)
        if not rows:
            print(f"{y}-{m:02d}: ei dataa")
            continue
        write_month(y, m, rows)
        print(f"{y}-{m:02d}: {len(rows)} päivää, {sum(v for _, v in rows):.1f} kWh")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# KÄYTTÖÖNOTTO
#
# 1. sudo apt install python3-requests python3-bs4
# 2. Luo ~/shelly/caruna.conf (malli yllä) ja suojaa se: chmod 600 ~/shelly/caruna.conf
# 3. Testaa:  python3 ~/shelly/caruna_fetch.py --debug
# 4. Ajasta päivittäin klo 16.05 ja aja vienti heti perään:
#    (crontab -l; echo "5 16 * * * /usr/bin/python3 /home/hoobs/shelly/caruna_fetch.py >> /home/hoobs/shelly/caruna.log 2>&1; /usr/bin/python3 /home/hoobs/shelly/shelly_export.py >> /home/hoobs/shelly/export.log 2>&1") | crontab -
# ---------------------------------------------------------------------------
