#!/usr/bin/env python3
"""
Kuukausiraportti: kokonaiskulutus, eritellyt kulutukset ja taloussähkö.

Lähteet (kaikki kansiossa ~/shelly):
  shelly.db        Shelly-keräimen tietokanta (shelly_collector.py)
  manual.csv       käsin syötetyt kuukausilukemat, rivi per kuukausi:
                       kuukausi;kokonais_kwh;ev_kwh;sauna_kwh
                       2026-10;1450;312,5;85
                   kokonais_kwh voi jättää tyhjäksi, jos Datahub-CSV on olemassa:
                       2026-10;;312,5;85
  datahub/*.csv    (valinnainen) Oma Datahubista ladatut kulutustiedostot
  history.csv      (valinnainen) Shelly-kohteiden historia ajalta ennen keräintä,
                   rivi per kuukausi per kohde:
                       kuukausi;kohde;kwh;osittainen
                       2025-01;maalampo;1234,5;
                       2025-03;kuivauskaappi;12,0;1
                   - kohde = täsmälleen sama nimi kuin keräimen DEVICES-listassa
                   - osittainen = 1, jos kohteen kuukausi on vajaa (puuttuva osa
                     päätyy taloussähköön); muuten tyhjä tai 0
                   - puuttuva kuukausi jätetään kokonaan pois (ei 0-riviä)
                   - käytetään kuukausille HISTORY_UNTIL asti, sen jälkeen shelly.db

Taloussähkö = kokonaiskulutus - sähköauto - sauna - kaikki Shelly-mittaukset.
Shelly-laitteet eivät saa mitata samaa kuormaa keskenään eivätkä autoa tai
saunaa, muuten sama kulutus vähennetään kahdesti.
Tulos tulostetaan ruudulle ja tallennetaan tiedostoon raportti.csv.

Käyttö:  python3 ~/shelly/shelly_report.py
"""

import csv
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path.home() / "shelly"
DB_PATH = BASE / "shelly.db"
MANUAL = BASE / "manual.csv"
DATAHUB_DIR = BASE / "datahub"
HISTORY = BASE / "history.csv"
OUT = BASE / "raportti.csv"
TZ = ZoneInfo("Europe/Helsinki")

# Viimeinen kuukausi, jonka Shelly-kohteet otetaan history.csv:stä.
# Myöhemmät kuukaudet tulevat keräimen tietokannasta (shelly.db).
HISTORY_UNTIL = "2026-09"


def num(s):
    s = (s or "").strip().replace(" ", "").replace(",", ".")
    return float(s) if s else None


def month_of(dt):
    return dt.astimezone(TZ).strftime("%Y-%m")


# --- Käsin syötetyt ---------------------------------------------------------

MANUAL_ITEMS = ["Sähköauto", "Sauna"]  # manual.csv sarakkeet 3, 4, ...


def read_manual():
    """Palauttaa (kokonaiskulutus per kk, {kohde: {kk: kWh}}).
    Saman kuukauden myöhempi rivi korvaa aiemman."""
    total = {}
    items = {name: {} for name in MANUAL_ITEMS}
    if not MANUAL.exists():
        return total, items
    for line in MANUAL.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2 or not parts[0][:4].isdigit():
            continue  # otsikko tai tyhjä rivi
        m = parts[0]
        t = num(parts[1])
        if t is not None:
            total[m] = t
        for name, val in zip(MANUAL_ITEMS, parts[2:]):
            v = num(val)
            if v is not None:
                items[name][m] = v
    return total, items


# --- Datahub-CSV ------------------------------------------------------------

TIME_KEYS = ("start", "alku", "time", "aika", "date", "pvm")
QTY_KEYS = ("quantity", "määrä", "maara", "kwh", "kulutus", "energy", "value")


def parse_time(s):
    s = s.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError:
            pass
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def read_datahub():
    totals = defaultdict(float)
    if not DATAHUB_DIR.exists():
        return totals
    seen = set()  # sama jakso useassa tiedostossa lasketaan kerran
    for f in sorted(DATAHUB_DIR.glob("*.csv")):
        text = f.read_text(encoding="utf-8-sig", errors="replace")
        delim = ";" if text.count(";") >= text.count(",") else ","
        rows = list(csv.reader(text.splitlines(), delimiter=delim))
        ti = qi = None
        for i, r in enumerate(rows):
            low = [c.strip().lower() for c in r]
            t = next((j for j, c in enumerate(low) if any(k in c for k in TIME_KEYS)), None)
            q = next((j for j, c in enumerate(low) if any(k in c for k in QTY_KEYS)), None)
            if t is not None and q is not None and t != q:
                ti, qi, start = t, q, i + 1
                break
        if ti is None:
            print(f"Ohitetaan {f.name}: sarakkeita ei tunnistettu")
            continue
        for r in rows[start:]:
            try:
                dt, v = parse_time(r[ti]), num(r[qi])
            except (ValueError, IndexError):
                continue
            if v is None or dt in seen:
                continue
            seen.add(dt)
            totals[month_of(dt)] += v
    return totals


# --- Shelly-historia --------------------------------------------------------

def read_history():
    """Palauttaa ({kk: {kohde: kWh}}, {(kk, kohde) osittaisina}).
    Rivit HISTORY_UNTIL-kuukauden jälkeen ohitetaan (ne tulevat shelly.db:stä).
    Saman kuukauden ja kohteen myöhempi rivi korvaa aiemman."""
    hist = defaultdict(dict)
    partial = set()
    if not HISTORY.exists():
        return hist, partial
    skipped = 0
    for line in HISTORY.read_text(encoding="utf-8-sig").splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 3 or not parts[0][:4].isdigit():
            continue  # otsikko tai tyhjä rivi
        m, k, v = parts[0], parts[1], num(parts[2])
        if v is None or not k:
            continue
        if m > HISTORY_UNTIL:
            skipped += 1
            continue
        hist[m][k] = v
        if len(parts) > 3 and parts[3] in ("1", "x", "kyllä"):
            partial.add((m, k))
        else:
            partial.discard((m, k))
    if skipped:
        print(f"history.csv: ohitettu {skipped} riviä {HISTORY_UNTIL} jälkeen "
              f"(ne luetaan shelly.db:stä)")
    return hist, partial


def shelly_items():
    """Yhdistää historian ja keräimen datan.
    Palauttaa ({kk: {kohde: kWh}}, {kk: {kohde: lähde}}, {(kk, kohde) osittaisina})."""
    hist, hist_partial = read_history()
    db, first_ts = read_shelly()
    first_month = {d: month_of(datetime.fromtimestamp(t, timezone.utc))
                   for d, t in first_ts.items()}
    values = defaultdict(dict)
    source = defaultdict(dict)
    partial = set()
    for m, items in hist.items():
        for k, v in items.items():
            values[m][k] = v
            source[m][k] = "Shelly-historia"
    partial |= hist_partial
    for m, items in db.items():
        if m <= HISTORY_UNTIL:
            continue  # historia on näille kuukausille ensisijainen
        for d, v in items.items():
            values[m][d] = v
            source[m][d] = "Shelly"
            if first_month.get(d) == m:
                partial.add((m, d))
    return values, source, partial


# --- Shelly-keräin ----------------------------------------------------------

def read_shelly():
    """kWh per kuukausi per laite energialaskurien muutoksista."""
    result = defaultdict(lambda: defaultdict(float))
    first_ts = {}
    if not DB_PATH.exists():
        return result, first_ts
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT device, channel, ts, energy_wh FROM readings "
        "WHERE energy_wh IS NOT NULL ORDER BY device, channel, ts")
    channels = defaultdict(list)
    for dev, ch, ts, e in rows:
        channels[(dev, ch)].append((ts, e))
        first_ts[dev] = min(first_ts.get(dev, ts), ts)
    con.close()

    # Pro 3EM: käytetään vain kokonaiskanavaa, ettei vaiheita lasketa kahdesti
    has_total = {d for (d, c) in channels if c.endswith(".total")}

    for (dev, ch), series in channels.items():
        if dev in has_total and not ch.endswith(".total"):
            continue
        for (_, prev), (ts, cur) in zip(series, series[1:]):
            delta = cur - prev if cur >= prev else cur  # laskurin nollautuminen
            m = month_of(datetime.fromtimestamp(ts, timezone.utc))
            result[m][dev] += delta / 1000
    return result, first_ts


# --- Raportti ---------------------------------------------------------------

def fmt(v):
    return "–" if v is None else f"{v:,.1f}".replace(",", " ").replace(".", ",")


def main():
    man_total, items = read_manual()
    dh = read_datahub()
    shelly, sources, partial = shelly_items()

    months = sorted(set(man_total) | set(dh) | set(shelly) |
                    {m for v in items.values() for m in v})
    devices = sorted({d for m in shelly.values() for d in m})

    out_rows = []
    for m in months:
        total = dh.get(m) if m in dh else man_total.get(m)
        src = "Datahub" if m in dh else ("käsin" if m in man_total else "")

        # Kaikki tiedossa olevat eritellyt kulutukset
        parts = []  # (nimi, kWh, huomautus)
        for name in MANUAL_ITEMS:
            v = items[name].get(m)
            parts.append((name, v, "käsin" if v is not None else "puuttuu"))
        for d in devices:
            v = shelly.get(m, {}).get(d)
            note = sources.get(m, {}).get(d, "Shelly")
            if (m, d) in partial:
                note += ", osittainen kk"
            parts.append((d, v, note if v is not None else "ei dataa"))

        known = sum(v for _, v, _ in parts if v is not None)
        house = total - known if total is not None else None

        print(f"\n=== {m} ===")
        print(f"  Kokonaiskulutus ({src or 'puuttuu'}): {fmt(total)} kWh")
        print("  Eritellyt:")
        for name, v, note in parts:
            print(f"    {name:<22} {fmt(v):>10} kWh  ({note})")
        print(f"  {'Eritellyt yhteensä':<24} {fmt(known):>10} kWh")
        print(f"  {'Taloussähkö (erotus)':<24} {fmt(house):>10} kWh")
        if house is not None and house < 0:
            print("  ! Erotus negatiivinen: tarkista lukemat tai päällekkäiset mittaukset")

        out_rows.append([m, src, total] + [v for _, v, _ in parts] + [known, house])

    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["kuukausi", "kokonais_lahde", "kokonais_kwh"] +
                   [f"{n.lower()}_kwh" for n in MANUAL_ITEMS] +
                   [f"{d}_kwh" for d in devices] +
                   ["eritellyt_kwh", "talous_kwh"])
        for r in out_rows:
            w.writerow([("" if v is None else
                         (f"{v:.2f}".replace(".", ",") if isinstance(v, float) else v))
                        for v in r])
    print(f"\nTallennettu: {OUT}")


if __name__ == "__main__":
    main()
