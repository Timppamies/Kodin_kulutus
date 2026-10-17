"""
Tuulensuunkaari 12 sähkönkulutus – kuukausi- ja vuosikooste.

Data: energia_kk.csv (HOOBS lähettää kerran päivässä Hostingeriin)
Lämpötila: oma sääasema (Weather Underground PWS) jos avain on asetettu,
           muuten / puuttuville kuukausille Ilmatieteen laitos, Helsinki-Vantaa (fmisid 100968)

Secrets (.streamlit/secrets.toml tai Streamlit Cloudin Secrets-asetus):
    APP_PASSWORD  = "..."
    DATA_URL      = "https://oma-domain.fi/energia_kk.csv"
    WU_API_KEY    = "..."          # valinnainen
    WU_STATION    = "IETELSUO12"   # valinnainen
    DATA_USER / DATA_PASSWORD      # valinnaiset, jos CSV on salasanasuojattu
"""

import hmac
import math
import time
import io
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

st.set_page_config(page_title="Tuulensuunkaari 12 sähkönkulutus", page_icon="⚡", layout="wide")

FMISID = 100968  # Helsinki-Vantaan lentoasema
FMI_URL = "https://opendata.fmi.fi/wfs"
KK = ["tammi", "helmi", "maalis", "huhti", "touko", "kesä",
      "heinä", "elo", "syys", "loka", "marras", "joulu"]

# Kiinteät värit kohteittain (väri seuraa kohdetta, ei järjestystä)
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
          "#e87ba4", "#008300", "#6250d6", "#e34948"]
TALOUS_COLOR = "#a9a79f"   # jäännöserä neutraalina
TEMP_COLOR = "#e34948"     # punainen
WU_URL = "https://api.weather.com/v2/pws/history/daily"


# --- Kirjautuminen -----------------------------------------------------------

def check_password():
    if st.session_state.get("auth_ok"):
        return True
    st.title("⚡ Tuulensuunkaari 12 sähkönkulutus")
    pw = st.text_input("Salasana", type="password")
    if pw:
        if hmac.compare_digest(pw, st.secrets["APP_PASSWORD"]):
            st.session_state.auth_ok = True
            st.rerun()
        st.error("Väärä salasana")
    return False


# --- Data --------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner="Haetaan kulutusdataa…")
def load_energy():
    user, pw = st.secrets.get("DATA_USER"), st.secrets.get("DATA_PASSWORD")
    # Aikaleima osoitteeseen ja no-cache-otsake ohittavat palvelimen/CDN:n välimuistin
    resp = requests.get(st.secrets["DATA_URL"],
                        params={"t": int(time.time())},
                        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                        auth=(user, pw) if user and pw else None,  # tunnukset valinnaisia
                        timeout=20)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.content.decode("utf-8")))
    df["kuukausi"] = pd.PeriodIndex(df["kuukausi"], freq="M")
    df["vuosi"] = df["kuukausi"].dt.year
    df["kk"] = df["kuukausi"].dt.month
    return df


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def fmi_month_mean(year: int, month: int):
    """Kuukauden keskilämpötila päivien keskiarvoista (kesken olevalle kk:lle tähänastinen)."""
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    end = min(end, date.today())
    if end <= start:
        return None
    params = {
        "service": "WFS", "version": "2.0.0", "request": "getFeature",
        "storedquery_id": "fmi::observations::weather::daily::simple",
        "fmisid": FMISID, "parameters": "tday",
        "starttime": f"{start}T00:00:00Z",
        "endtime": f"{end}T00:00:00Z",
    }
    try:
        r = requests.get(FMI_URL, params=params, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception:
        return None
    vals = []
    for el in root.iter():
        if el.tag.endswith("BsWfsElement"):
            t = v = None
            for c in el:
                if c.tag.endswith("Time"):
                    t = c.text
                elif c.tag.endswith("ParameterValue"):
                    v = c.text
            if t and v and t[:7] == f"{year}-{month:02d}" and v.lower() != "nan":
                vals.append(float(v))
    return round(sum(vals) / len(vals), 1) if vals else None


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def wu_month_mean(year: int, month: int, station: str, key: str):
    """Oman sääaseman kuukausikeskiarvo päivien keskilämpötiloista (yksi kutsu / kk)."""
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    end = min(end, date.today())
    if end < start:
        return None
    params = {"stationId": station, "format": "json", "units": "m",
              "numericPrecision": "decimal", "apiKey": key,
              "startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d")}
    try:
        r = requests.get(WU_URL, params=params, timeout=20)
        if r.status_code == 204:  # ei dataa jaksolta
            return None
        r.raise_for_status()
        obs = r.json().get("observations", [])
    except Exception:
        return None
    vals = [o["metric"]["tempAvg"] for o in obs
            if (o.get("metric") or {}).get("tempAvg") is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def temps_for(periods):
    """Palauttaa ({kk: °C}, lähteen kuvaus). Oma asema ensin, puutteet Helsinki-Vantaalta."""
    key, station = st.secrets.get("WU_API_KEY"), st.secrets.get("WU_STATION")
    temps, used = {}, set()
    for p in periods:
        v = wu_month_mean(p.year, p.month, station, key) if key and station else None
        if v is not None:
            used.add(f"oma sääasema {station}")
        else:
            v = fmi_month_mean(p.year, p.month)
            if v is not None:
                used.add("Helsinki-Vantaa")
        temps[p] = v
    return temps, " + ".join(sorted(used)) or "ei saatavilla"


def color_map(df):
    """Käsin syötetyt ensin, sitten Shelly-laitteet aakkosjärjestyksessä."""
    er = df[df["tyyppi"] == "eritelty"]
    manual = sorted(er.loc[er["lahde"] == "käsin", "kohde"].unique())
    shelly = sorted(set(er["kohde"]) - set(manual))
    order = manual + shelly
    cmap = {k: SERIES[i % len(SERIES)] for i, k in enumerate(order)}
    cmap["Taloussähkö"] = TALOUS_COLOR
    return order + ["Taloussähkö"], cmap


def fmt(v, dec=0):
    return f"{v:,.{dec}f}".replace(",", " ").replace(".", ",")


# --- Näkymät -----------------------------------------------------------------

def monthly_view(df, order, cmap):
    years = sorted(df["vuosi"].unique(), reverse=True)
    year = st.selectbox("Vuosi", years, index=0)
    d = df[df["vuosi"] == year]
    parts = d[d["tyyppi"].isin(["eritelty", "talous"])]
    total = d[d["tyyppi"] == "kokonais"].set_index("kk")["kwh"]
    months = sorted(d["kk"].unique())
    labels = [KK[m - 1] for m in months]

    with st.spinner("Haetaan lämpötiloja…"):
        temps, tsrc = temps_for([pd.Period(f"{year}-{m:02d}", "M") for m in months])
    tvals = [temps[pd.Period(f"{year}-{m:02d}", "M")] for m in months]

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    for k in order:
        s = parts[parts["kohde"] == k].set_index("kk")["kwh"]
        if s.empty:
            continue
        fig.add_bar(x=labels, y=[s.get(m) for m in months], name=k,
                    marker=dict(color=cmap[k], line=dict(width=1, color="rgba(255,255,255,0.9)")),
                    hovertemplate=f"<b>{k}</b><br>%{{x}}: %{{y:,.0f}} kWh<extra></extra>",
                    secondary_y=False)
    # Kokonaissumma pylvään yläpuolelle
    fig.add_scatter(x=labels, y=[total.get(m) for m in months], mode="text",
                    text=[fmt(total.get(m)) if m in total else "" for m in months],
                    textposition="top center", showlegend=False, hoverinfo="skip",
                    textfont=dict(size=11), secondary_y=False)
    # Lämpötila oikealle akselille, katkoviivalla erotukseksi pylväistä
    fig.add_scatter(x=labels, y=tvals, mode="lines+markers", name="Keskilämpötila (°C)",
                    line=dict(color=TEMP_COLOR, width=2, dash="dash"),
                    marker=dict(size=8, color=TEMP_COLOR),
                    hovertemplate=f"%{{x}}: %{{y:.1f}} °C<extra>{tsrc}</extra>",
                    secondary_y=True)
    fig.update_layout(barmode="stack", bargap=0.3, height=560,
                      title=f"Kulutus kohteittain ja keskilämpötila ({tsrc})",
                      legend=dict(orientation="h", y=-0.12),
                      margin=dict(l=10, r=10, t=50, b=10), hovermode="closest")
    fig.update_yaxes(title_text="kWh", gridcolor="rgba(128,128,128,0.15)", secondary_y=False)
    # Lämpötila-akseli: kokonaisluvut 5 asteen välein, nolla aina näkyvissä
    tv = [t for t in tvals if t is not None] or [0]
    lo = min(0, math.floor(min(tv) / 5) * 5)
    hi = max(0, math.ceil(max(tv) / 5) * 5)
    if hi - lo < 10:
        hi = lo + 10
    fig.update_yaxes(title_text="°C", showgrid=False, range=[lo - 1, hi + 1],
                     tickmode="linear", tick0=0, dtick=5, tickformat="d",
                     zeroline=True, zerolinecolor="rgba(227,73,72,0.5)", zerolinewidth=1,
                     color=TEMP_COLOR, secondary_y=True)
    st.plotly_chart(fig, use_container_width=True)

    partial = d[d["osittainen"] == 1]
    if not partial.empty:
        st.caption("Shelly-mittaus alkanut kesken kuukauden: " +
                   ", ".join(f"{r.kohde} ({KK[r.kk - 1]})" for r in partial.itertuples()) +
                   ". Mittaamaton osa sisältyy taloussähköön.")

    with st.expander("Taulukko"):
        tab = parts.pivot_table(index="kohde", columns="kk", values="kwh", aggfunc="sum")
        tab = tab.reindex([k for k in order if k in tab.index])
        tab.loc["Kokonaiskulutus"] = total
        tab.loc["Lämpötila °C"] = pd.Series(tvals, index=months, dtype="float")
        tab.columns = [KK[m - 1] for m in tab.columns]
        st.dataframe(tab.round(1), use_container_width=True)


def yearly_view(df, order, cmap):
    parts = df[df["tyyppi"].isin(["eritelty", "talous"])]
    yearly = parts.pivot_table(index="vuosi", columns="kohde", values="kwh", aggfunc="sum").fillna(0)
    totals = df[df["tyyppi"] == "kokonais"].groupby("vuosi")["kwh"].sum()
    nmonths = df[df["tyyppi"] == "kokonais"].groupby("vuosi")["kk"].nunique()
    years = list(yearly.index)

    # Tunnusluvut viimeisimmältä vuodelta
    y = years[-1]
    cols = st.columns(3)
    cols[0].metric(f"Kokonaiskulutus {y}", f"{fmt(totals.get(y, 0))} kWh",
                   help=f"{nmonths.get(y, 0)} kuukautta")
    if len(years) > 1 and y - 1 in totals:
        prev = df[(df["vuosi"] == y - 1) & (df["tyyppi"] == "kokonais") &
                  (df["kk"] <= df[df["vuosi"] == y]["kk"].max())]["kwh"].sum()
        cols[1].metric("Muutos edelliseen vuoteen (sama jakso)",
                       f"{(totals[y] / prev - 1) * 100:+.1f} %" if prev else "–")
    with st.spinner("Haetaan lämpötiloja…"):
        per = df.loc[df["vuosi"] == y, "kuukausi"].unique()
        tdict, tsrc = temps_for(list(per))
        t = [v for v in tdict.values() if v is not None]
    cols[2].metric(f"Keskilämpötila {y}", f"{sum(t) / len(t):.1f} °C".replace(".", ",") if t else "–",
                   help=f"{tsrc}, kuukausien keskiarvo")

    fig = go.Figure()
    for k in order:
        if k in yearly.columns:
            fig.add_bar(x=[str(v) for v in years], y=yearly[k], name=k,
                        marker=dict(color=cmap[k], line=dict(width=1, color="rgba(255,255,255,0.9)")),
                        hovertemplate=f"<b>{k}</b><br>%{{x}}: %{{y:,.0f}} kWh<extra></extra>")
    fig.add_scatter(x=[str(v) for v in years], y=[totals.get(v) for v in years], mode="text",
                    text=[fmt(totals.get(v, 0)) for v in years], textposition="top center",
                    showlegend=False, hoverinfo="skip")
    fig.update_layout(barmode="stack", bargap=0.45, height=460, title="Vuosikulutus kohteittain (kWh)",
                      legend=dict(orientation="h", y=-0.12), margin=dict(l=10, r=10, t=50, b=10))
    fig.update_xaxes(type="category")  # vuodet kokonaislukuina, ei 2024,5
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.15)")
    st.plotly_chart(fig, use_container_width=True)

    # Osuudet
    share = yearly.T.reindex([k for k in order if k in yearly.columns])
    pct = share.div(share.sum(), axis=1) * 100
    out = pd.DataFrame({f"{v} kWh": share[v].round(0) for v in years} |
                       {f"{v} %": pct[v].round(1) for v in years})
    st.subheader("Kohteiden osuudet")
    st.dataframe(out, use_container_width=True)

    # Kuukausittainen kokonaiskulutus vuosittain rinnakkain
    tot = df[df["tyyppi"] == "kokonais"]
    fig2 = go.Figure()
    for i, v in enumerate(years):
        s = tot[tot["vuosi"] == v].set_index("kk")["kwh"]
        fig2.add_bar(x=[KK[m - 1] for m in s.index], y=s.values, name=str(v),
                     marker=dict(color=SERIES[i % len(SERIES)],
                                 line=dict(width=1, color="rgba(255,255,255,0.9)")),
                     hovertemplate=f"{v} %{{x}}: %{{y:,.0f}} kWh<extra></extra>")
    fig2.update_layout(title="Kokonaiskulutus kuukausittain, vuosivertailu (kWh)", height=400,
                       barmode="group", bargap=0.25, bargroupgap=0.05,
                       margin=dict(l=10, r=10, t=50, b=10), hovermode="x unified",
                       legend=dict(orientation="h", y=-0.15))
    fig2.update_xaxes(categoryorder="array", categoryarray=KK)
    fig2.update_yaxes(gridcolor="rgba(128,128,128,0.15)")
    st.plotly_chart(fig2, use_container_width=True)

    st.info("🚗 Kesäkuusta 2025 alkaen taloudessa on kaksi sähköautoa. "
            "Vuoden 2026 alusta autot on ladattu kotona, koska veroetu muuttui.")
    st.caption("Huom. Shelly-erittely alkaa lokakuusta 2026. Sitä ennen esim. maalämpöpumpun "
               "kulutus sisältyy taloussähköön, joten vuosien osuudet eivät ole suoraan vertailukelpoisia.")


# --- Pääohjelma --------------------------------------------------------------

if check_password():
    st.title("⚡ Tuulensuunkaari 12 sähkönkulutus")
    try:
        df = load_energy()
    except Exception as e:
        st.error(f"Datan haku epäonnistui: {e}")
        st.stop()
    order, cmap = color_map(df)
    last = df["kuukausi"].max()
    c1, c2 = st.columns([5, 1])
    c1.caption(f"Data {df['kuukausi'].min()} – {last}. Päivittyy kerran vuorokaudessa.")
    if c2.button("🔄 Päivitä data", use_container_width=True):
        load_energy.clear()
        st.rerun()

    t1, t2 = st.tabs(["Kuukausiraportti", "Vuosikooste"])
    with t1:
        monthly_view(df, order, cmap)
    with t2:
        yearly_view(df, order, cmap)
