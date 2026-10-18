#!/usr/bin/env python3
"""
Shelly Cloudin laitekohtainen kulutusvienti -> history.csv (kuukausittain).

Vientitiedoston muoto (Shelly Cloud, "Consumption"):
     Consumption
     Time, Wh
      01/01/2025 00:00 , 174957.08
      01/02/2025 00:00 , 172571.42
Päivämäärä PP/KK/VVVV. Kuukausi-, päivä- ja tuntitason viennit käyvät:
rivit summataan kuukausiksi ja Wh muunnetaan kWh:ksi.

Käyttö:
    python3 shelly_history_import.py KOHDE VIENTI.csv [VIENTI2.csv ...]
                                     [--partial 2025-03,2025-04] [--out history.csv]

- KOHDE = sama nimi kuin keräimen DEVICES-listassa, esim. "Varasto lämmitys"
- --partial merkitsee kuukaudet vajaiksi (mittaamaton osa jää taloussähköön)
- saman kohteen aiemmat rivit history.csv:ssä korvataan, muiden kohteiden säilyvät
- oletustiedosto on ~/shelly/history.csv
"""

import argparse
import re
from collections import defaultdict
from pathlib import Path

ROW = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*[,;]\s*([-\d.,]+)\s*$")
HEADER = "kuukausi;kohde;kwh;osittainen"


def read_export(path):
    """{kk: kWh} Shelly-viennistä."""
    out = defaultdict(float)
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        m = ROW.match(line)
        if not m:
            continue  # otsikot, tyhjät rivit
        day, month, year, val = m.groups()
        out[f"{year}-{int(month):02d}"] += float(val.replace(",", ".")) / 1000
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("kohde")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--partial", default="", help="vajaat kuukaudet pilkulla, esim. 2025-03,2025-04")
    ap.add_argument("--out", default=str(Path.home() / "shelly" / "history.csv"))
    a = ap.parse_args()

    months = defaultdict(float)
    for f in a.files:
        for m, v in read_export(f).items():
            months[m] += v
    if not months:
        raise SystemExit("Vientitiedostosta ei löytynyt kulutusrivejä")
    partial = {p.strip() for p in a.partial.split(",") if p.strip()}

    out = Path(a.out)
    keep = []
    if out.exists():
        for line in out.read_text(encoding="utf-8-sig").splitlines():
            parts = [p.strip() for p in line.split(";")]
            if len(parts) >= 3 and parts[0][:4].isdigit() and parts[1] != a.kohde:
                keep.append(line)
    new = [f"{m};{a.kohde};{f'{v:.3f}'.replace('.', ',')};{'1' if m in partial else ''}"
           for m, v in sorted(months.items())]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join([HEADER] + sorted(keep + new)) + "\n", encoding="utf-8")

    total = sum(months.values())
    print(f"{a.kohde}: {len(months)} kk ({min(months)} … {max(months)}), "
          f"yhteensä {total:.1f} kWh -> {out}")


if __name__ == "__main__":
    main()
