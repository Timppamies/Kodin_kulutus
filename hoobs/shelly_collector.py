#!/usr/bin/env python3
"""
Shelly-energiadatan keräin lähiverkosta (HOOBS / Raspberry Pi).

- Tunnistaa laitteen sukupolven automaattisesti (Gen1 / Gen2+).
- Tukee mm. Shelly 3EM (Gen1), Pro 3EM, Plus Plug S, Pro EM-50, Plus PM Mini.
- Tallentaa lukemat SQLite-tietokantaan (pitkä muoto: yksi rivi / kanava / ajokerta).
- Vain Pythonin vakiokirjasto, ei asennettavia paketteja.

Ajo cronilla kerran minuutissa, ks. ohje tiedoston lopussa.
"""

import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# ASETUKSET – muokkaa nämä
# ---------------------------------------------------------------------------

DEVICES = [
    # nimi: vapaa tunniste, ip: laitteen osoite
    {"name": "3em_paakeskus", "ip": "192.168.1.50"},
    {"name": "plug_kahvinkeitin", "ip": "192.168.1.51"},
    # {"name": "garage_em50", "ip": "192.168.1.52"},
]

# Jos laitteissa on salasana, täytä tähän (Gen1: käyttäjä vapaa, Gen2: "admin")
USERNAME = None
PASSWORD = None

DB_PATH = Path.home() / "shelly" / "shelly.db"
TIMEOUT = 5  # sekuntia / pyyntö

# ---------------------------------------------------------------------------


def make_opener():
    if not PASSWORD:
        return urllib.request.build_opener()
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    for d in DEVICES:
        mgr.add_password(None, f"http://{d['ip']}/", USERNAME or "admin", PASSWORD)
    return urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(mgr),  # Gen2+
        urllib.request.HTTPBasicAuthHandler(mgr),   # Gen1
    )


OPENER = make_opener()


def get_json(url):
    with OPENER.open(url, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def detect_gen(ip):
    info = get_json(f"http://{ip}/shelly")
    return info.get("gen", 1)


def row(channel, power=None, energy=None, voltage=None, current=None):
    return {"channel": channel, "power_w": power, "energy_wh": energy,
            "voltage": voltage, "current": current}


def read_gen1(ip):
    s = get_json(f"http://{ip}/status")
    rows = []
    # 3EM / EM: emeters, total on Wh
    for i, m in enumerate(s.get("emeters", [])):
        rows.append(row(f"emeter{i}", m.get("power"), m.get("total"),
                        m.get("voltage"), m.get("current")))
    # Plug S, 1PM, 2.5 jne: meters, total on wattiminuutteja -> Wh
    for i, m in enumerate(s.get("meters", [])):
        total = m.get("total")
        rows.append(row(f"meter{i}", m.get("power"),
                        total / 60 if total is not None else None))
    return rows


def read_gen2(ip):
    s = get_json(f"http://{ip}/rpc/Shelly.GetStatus")
    rows = []
    for key, c in s.items():
        if not isinstance(c, dict):
            continue
        comp = key.split(":")[0]

        if comp in ("switch", "pm1"):  # Plus Plug S, 1PM, PM Mini
            rows.append(row(key, c.get("apower"),
                            (c.get("aenergy") or {}).get("total"),
                            c.get("voltage"), c.get("current")))

        elif comp == "em1":  # Pro EM-50 mittauskanava
            rows.append(row(key, c.get("act_power"), None,
                            c.get("voltage"), c.get("current")))
        elif comp == "em1data":
            rows.append(row(key, None, c.get("total_act_energy")))

        elif comp == "em":  # Pro 3EM
            for ph in ("a", "b", "c"):
                rows.append(row(f"{key}.{ph}", c.get(f"{ph}_act_power"), None,
                                c.get(f"{ph}_voltage"), c.get(f"{ph}_current")))
            rows.append(row(f"{key}.total", c.get("total_act_power")))
        elif comp == "emdata":
            for ph in ("a", "b", "c"):
                rows.append(row(f"{key}.{ph}", None,
                                c.get(f"{ph}_total_act_energy")))
            rows.append(row(f"{key}.total", None, c.get("total_act")))
    return rows


def init_db(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            ts        INTEGER NOT NULL,
            device    TEXT    NOT NULL,
            channel   TEXT    NOT NULL,
            power_w   REAL,
            energy_wh REAL,
            voltage   REAL,
            current   REAL
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_dev_ts ON readings(device, ts)")


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    init_db(con)
    ts = int(time.time())
    errors = 0

    for d in DEVICES:
        try:
            gen = detect_gen(d["ip"])
            rows = read_gen1(d["ip"]) if gen == 1 else read_gen2(d["ip"])
            con.executemany(
                "INSERT INTO readings VALUES (?,?,?,?,?,?,?)",
                [(ts, d["name"], r["channel"], r["power_w"], r["energy_wh"],
                  r["voltage"], r["current"]) for r in rows])
        except Exception as e:
            errors += 1
            print(f"{d['name']} ({d['ip']}): {e}", file=sys.stderr)

    con.commit()
    con.close()
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# KÄYTTÖÖNOTTO
#
# 1. Kopioi tiedosto HOOBS-laitteelle, esim. ~/shelly/shelly_collector.py
# 2. Muokkaa DEVICES-lista (ja salasana tarvittaessa)
# 3. Testaa:   python3 ~/shelly/shelly_collector.py
#              sqlite3 ~/shelly/shelly.db "SELECT * FROM readings LIMIT 20;"
# 4. Ajasta:   crontab -e  ja lisää rivi:
#    * * * * * /usr/bin/python3 /home/<käyttäjä>/shelly/shelly_collector.py 2>> /home/<käyttäjä>/shelly/errors.log
#
# Kulutus aikavälillä = energy_wh-laskurin erotus (loppu - alku).
# ---------------------------------------------------------------------------
