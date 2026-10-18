#!/usr/bin/env python3
"""
Päivittäinen vienti: kuukausitason kulutus kohteittain -> CSV -> Hostinger (FTPS).

Käyttää shelly_report.py:n laskentaa (oltava samassa kansiossa).
Taloussähkö = kokonaiskulutus - kaikki eritellyt (käsin + Shelly).

Asetukset tiedostossa ~/shelly/upload.conf:

    [ftp]
    host = ftp.oma-domain.fi
    user = u123456789.energia
    password = salasana
    remote_dir = public_html/energia
    tls = yes

Ajo cronilla kerran päivässä, ks. ohje tiedoston lopussa.
"""

import configparser
import csv
import ftplib
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shelly_report as r  # noqa: E402

BASE = Path.home() / "shelly"
OUT = BASE / "export" / "energia_kk.csv"
CONF = BASE / "upload.conf"
REMOTE_NAME = "energia_kk.csv"


def build_rows():
    man_total, items = r.read_manual()
    dh = r.read_datahub()
    shelly, sources, partial = r.shelly_items()

    months = sorted(set(man_total) | set(dh) | set(shelly) |
                    {m for v in items.values() for m in v})
    devices = sorted({d for m in shelly.values() for d in m})

    rows = []  # kuukausi, kohde, kwh, tyyppi, lahde, osittainen
    for m in months:
        total = dh.get(m) if m in dh else man_total.get(m)
        src = "Datahub" if m in dh else "käsin"
        known = 0.0
        for name in r.MANUAL_ITEMS:
            v = items[name].get(m)
            if v is not None:
                rows.append([m, name, v, "eritelty", "käsin", 0])
                known += v
        for d in devices:
            v = shelly.get(m, {}).get(d)
            if v is not None:
                rows.append([m, d, v, "eritelty",
                             sources.get(m, {}).get(d, "Shelly"),
                             1 if (m, d) in partial else 0])
                known += v
        if total is not None:
            rows.append([m, "Kokonaiskulutus", total, "kokonais", src, 0])
            rows.append([m, "Taloussähkö", total - known, "talous", "erotus", 0])
    return rows


def write_csv(rows):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)  # pilkku erottimena, piste desimaalina (koneluettava)
        w.writerow(["kuukausi", "kohde", "kwh", "tyyppi", "lahde", "osittainen"])
        for m, k, v, t, s, p in rows:
            w.writerow([m, k, f"{v:.3f}", t, s, p])


def upload():
    cfg = configparser.ConfigParser()
    if not cfg.read(CONF):
        raise SystemExit(f"Asetustiedosto puuttuu: {CONF}")
    c = cfg["ftp"]
    use_tls = c.get("tls", "yes").lower() in ("yes", "true", "1")
    ftp = ftplib.FTP_TLS(timeout=30) if use_tls else ftplib.FTP(timeout=30)
    ftp.connect(c["host"], int(c.get("port", "21")))
    ftp.login(c["user"], c["password"])
    if use_tls:
        ftp.prot_p()  # salaa myös tiedonsiirron
    remote_dir = c.get("remote_dir", "").strip("/")
    if remote_dir:
        ftp.cwd(remote_dir)
    tmp = REMOTE_NAME + ".tmp"
    with OUT.open("rb") as f:
        ftp.storbinary(f"STOR {tmp}", f)
    try:
        ftp.delete(REMOTE_NAME)
    except ftplib.error_perm:
        pass  # ensimmäinen lähetys
    ftp.rename(tmp, REMOTE_NAME)  # vaihto kerralla, lukija ei näe puolikasta
    ftp.quit()


def main():
    rows = build_rows()
    write_csv(rows)
    if "--no-upload" in sys.argv:
        print(f"Kirjoitettu {len(rows)} riviä: {OUT}")
        return
    upload()
    print(f"{datetime.now():%Y-%m-%d %H:%M} lähetetty {len(rows)} riviä")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# KÄYTTÖÖNOTTO
#
# 1. Kopioi tämä tiedosto kansioon ~/shelly (samaan kuin shelly_report.py)
# 2. Luo ~/shelly/upload.conf (malli yllä) ja suojaa se:
#        chmod 600 ~/shelly/upload.conf
# 3. Testaa ilman lähetystä:  python3 ~/shelly/shelly_export.py --no-upload
#    Testaa lähetys:          python3 ~/shelly/shelly_export.py
# 4. Ajasta kerran päivässä (klo 05.17):
#    (crontab -l; echo "17 5 * * * /usr/bin/python3 /home/hoobs/shelly/shelly_export.py >> /home/hoobs/shelly/export.log 2>&1") | crontab -
# ---------------------------------------------------------------------------
