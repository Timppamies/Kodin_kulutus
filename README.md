# Kodin energiaraportti

Streamlit-sovellus, joka näyttää kodin sähkönkulutuksen kuukausittain ja vuosittain
kohteittain jaettuna sekä Helsinki-Vantaan keskilämpötilan (Ilmatieteen laitos, avoin data).

## Datavirta

```
Shelly-laitteet ──(minuutin välein)──> HOOBS: shelly.db
manual.csv (kokonais, auto, sauna) ──> HOOBS
history.csv (Shelly-kohteet 2024-01 … 2026-10-15) ──> HOOBS
HOOBS: shelly_export.py ──(kerran päivässä, FTPS)──> Hostinger: sivuston juuri /energia_kk.csv
Streamlit Cloud: app.py ──(HTTPS + tunnukset)──> energia_kk.csv
                         └─> lämpötila: oma sääasema (Wunderground), varalla Ilmatieteen laitos
                         └─> lämmitystarveluku S17: data/s17_kk.csv (historia) + Wunderground (uudet kk)
```

## Tiedostot

- `app.py` – Streamlit-sovellus (Streamlit Cloud, main file)
- `hoobs/` – HOOBS-laitteella ajettavat skriptit: keräin (cron minuutin välein), raportti, vienti (cron kerran päivässä)

## Käyttöönotto

### 1. Hostinger
1. hPanel → Files → File Manager: luo kansio `public_html/energia`.
2. hPanel → Advanced → Password Protect Directories: suojaa kansio `energia`
   (käyttäjätunnus + salasana → näitä käyttää Streamlit).
3. hPanel → Files → FTP Accounts: luo FTP-tunnus, jonka kotikansio on `energia`
   (tai käytä päätunnusta ja aseta `remote_dir = public_html/energia`).

### 2. HOOBS
Kopioi `shelly_export.py` kansioon `~/shelly` ja luo `~/shelly/upload.conf`:
```ini
[ftp]
host = ftp.oma-domain.fi
user = u123456789.energia
password = ...
remote_dir =
tls = yes
```
```
chmod 600 ~/shelly/upload.conf
python3 ~/shelly/shelly_export.py
(crontab -l; echo "17 5 * * * /usr/bin/python3 /home/hoobs/shelly/shelly_export.py >> /home/hoobs/shelly/export.log 2>&1") | crontab -
```
Tarkista selaimella, että `https://oma-domain.fi/energia/energia_kk.csv` kysyy salasanan.

### 3. GitHub + Streamlit Cloud
1. Luo GitHub-repo (voi olla yksityinen) ja vie sinne `app.py`, `requirements.txt`, `.gitignore`.
2. share.streamlit.io → New app → valitse repo, main file `app.py`.
3. App settings → Secrets: liitä `.streamlit/secrets.toml.example`-tiedoston sisältö omilla arvoillasi.

## Shelly-historia (history.csv)

Kohteiden kuukausikulutus ajalta ennen keräintä, tiedostossa `~/shelly/history.csv`:

```
kuukausi;kohde;kwh;osittainen
2025-01;maalampo;1234,5;
2025-03;kuivauskaappi;12,0;1
```

- `kohde` kirjoitetaan täsmälleen kuten keräimen `DEVICES`-listassa, jotta historia ja
  uusi data näkyvät samana sarjana.
- `osittainen = 1`, jos kuukausi on vajaa. Kuukausi, jolta dataa ei ole, jätetään pois.
- Kuukausille `HISTORY_UNTIL` (shelly_report.py, nyt `2026-09`) asti käytetään vain historiaa.
  Sen jälkeen historia ja `shelly.db` lasketaan yhteen, joten lokakuulle 2026 viedään
  historiaan vain päivät ennen keräimen käynnistymistä (1.–15.10.).

Shelly Cloudin laitekohtaisen viennin (Consumption, Wh) saa muunnettua ja lisättyä
history.csv:hen skriptillä; saman kohteen vanhat rivit korvataan:

```
python3 hoobs/shelly_history_import.py "Varasto lämmitys" 2026YTD_varasto.csv
python3 hoobs/shelly_history_import.py kuivauskaappi kuivauskaappi.csv --partial 2025-03
```

## Lämpötilakorjaus

`data/s17_kk.csv` sisältää oman sääaseman kuukausittaiset lämmitystarveluvut (S17)
2011-01 alkaen. Luvut on laskettu sääaseman tietokannasta (taulu `wp_ws40_log_daily`,
`metric = 'outTemp'`) kyselyllä, joka summaa kevään (tammi–kesä) alle +10 °C ja syksyn
(heinä–joulu) alle +12 °C päiviltä erotuksen 17 °C − päivän keskilämpötila. Tiedoston
jälkeiset kuukaudet sovellus laskee Wundergroundin päiväkeskiarvoista.

Normaalivuosi on vuosien 2011–2025 keskiarvo (`NORMAL_YEARS` app.py:ssä). Ryhmävertailun
kytkin korjaa lämmitysryhmän normaalivuoteen mallilla
`kWh = a · vuorokaudet + b · S17`, joka sovitetaan kaikista täysistä kuukausista.

## Datan muoto (energia_kk.csv)

| sarake | kuvaus |
|---|---|
| kuukausi | `VVVV-KK` |
| kohde | esim. Sähköauto, Sauna, maalampopumppu, Kokonaiskulutus, Taloussähkö |
| kwh | kulutus kuukaudessa |
| tyyppi | `kokonais`, `eritelty` tai `talous` (erotus) |
| lahde | käsin / Shelly / Shelly-historia / Datahub / erotus |
| osittainen | 1 = kohteen mittaus kattaa vain osan kuukaudesta |
