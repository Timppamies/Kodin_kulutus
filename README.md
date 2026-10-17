# Kodin energiaraportti

Streamlit-sovellus, joka näyttää kodin sähkönkulutuksen kuukausittain ja vuosittain
kohteittain jaettuna sekä Helsinki-Vantaan keskilämpötilan (Ilmatieteen laitos, avoin data).

## Datavirta

```
Shelly-laitteet ──(minuutin välein)──> HOOBS: shelly.db
manual.csv (kokonais, auto, sauna) ──> HOOBS
HOOBS: shelly_export.py ──(kerran päivässä, FTPS)──> Hostinger: sivuston juuri /energia_kk.csv
Streamlit Cloud: app.py ──(HTTPS + tunnukset)──> energia_kk.csv
                         └─> lämpötila: oma sääasema (Wunderground), varalla Ilmatieteen laitos
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

## Datan muoto (energia_kk.csv)

| sarake | kuvaus |
|---|---|
| kuukausi | `VVVV-KK` |
| kohde | esim. Sähköauto, Sauna, maalampopumppu, Kokonaiskulutus, Taloussähkö |
| kwh | kulutus kuukaudessa |
| tyyppi | `kokonais`, `eritelty` tai `talous` (erotus) |
| lahde | käsin / Shelly / Datahub / erotus |
| osittainen | 1 = Shelly-mittaus alkoi kesken kuukauden |
