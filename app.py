"""
Tuulensuunkaari 12 sähkönkulutus – kuukausi- ja vuosikooste.

Data: energia_kk.csv (HOOBS lähettää kerran päivässä Hostingeriin)
Lämpötila: oma sääasema (Weather Underground PWS) jos avain on asetettu,
           muuten / puuttuville kuukausille Ilmatieteen laitos, Helsinki-Vantaa (fmisid 100968)
Lämmitystarveluku: data/s17_kk.csv (oman aseman historia), uudemmat kuukaudet säähausta

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
from pathlib import Path

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
def fmi_month_days(year: int, month: int):
    """Helsinki-Vantaan päivien keskilämpötilat kuukaudelta (kesken olevalta tähänastiset)."""
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
    return vals or None


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def wu_month_days(year: int, month: int, station: str, key: str):
    """Oman sääaseman päivien keskilämpötilat kuukaudelta (yksi kutsu / kk)."""
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
    return vals or None


def month_days(year: int, month: int):
    """(päivien keskilämpötilat, lähde): oma asema ensin, varalla Helsinki-Vantaa."""
    key, station = st.secrets.get("WU_API_KEY"), st.secrets.get("WU_STATION")
    vals = wu_month_days(year, month, station, key) if key and station else None
    if vals:
        return vals, f"oma sääasema {station}"
    vals = fmi_month_days(year, month)
    return (vals, "Helsinki-Vantaa") if vals else (None, None)


def temps_for(periods):
    """Palauttaa ({kk: °C}, lähteen kuvaus). Oma asema ensin, puutteet Helsinki-Vantaalta."""
    temps, used = {}, set()
    for p in periods:
        vals, src = month_days(p.year, p.month)
        temps[p] = round(sum(vals) / len(vals), 1) if vals else None
        if src:
            used.add(src)
    return temps, " + ".join(sorted(used)) or "ei saatavilla"


# --- Lämmitystarveluku (S17) ---------------------------------------------------

S17_FILE = Path(__file__).parent / "data" / "s17_kk.csv"
NORMAL_YEARS = range(2011, 2026)   # normaalivuosi: oman aseman 15 täyttä vuotta


def s17_of(month: int, temps):
    """Ilmatieteen laitoksen S17: kevät (tammi–kesä) < +10 °C, syksy < +12 °C."""
    limit = 10 if month <= 6 else 12
    return sum(17 - t for t in temps if t < limit)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def load_s17_static():
    s = pd.read_csv(S17_FILE)
    s["kuukausi"] = pd.PeriodIndex(s["kuukausi"], freq="M")
    return s.set_index("kuukausi")


def s17_table(periods):
    """DataFrame (index = kuukausi): s17, paivia, paivia_kk, normaali, normaali_jakso, lahde.
    Historia repon tiedostosta, sen jälkeiset kuukaudet säähausta päivistä laskien.
    normaali_jakso = normaalikuukausi suhteutettuna mitattujen päivien määrään
    (kesken oleva kuukausi verrataan vain sen tähänastisiin päiviin)."""
    st_ = load_s17_static()
    hist = st_[st_.index.year.isin(NORMAL_YEARS)]
    normal = hist.groupby(hist.index.month)["s17"].mean()
    rows = {}
    for p in sorted(set(periods)):
        if p in st_.index:
            r = st_.loc[p]
            rows[p] = dict(s17=r["s17"], paivia=r["paivia"], paivia_kk=r["paivia_kk"],
                           lahde="oma sääasema (historia)")
        else:
            vals, src = month_days(p.year, p.month)
            if not vals:
                continue
            rows[p] = dict(s17=round(s17_of(p.month, vals), 1), paivia=len(vals),
                           paivia_kk=p.days_in_month, lahde=src)
    t = pd.DataFrame.from_dict(rows, orient="index")
    if t.empty:
        return t
    t["normaali"] = [normal.get(p.month) for p in t.index]
    t["normaali_jakso"] = t["normaali"] * t["paivia"] / t["paivia_kk"]
    return t


def heating_model(monthly_kwh, s17t):
    """Sovitus: kWh = a · vuorokaudet + b · S17 täysistä kuukausista.
    Palauttaa dict(a, b, r2, n) tai None, jos dataa on alle 12 kuukautta."""
    import numpy as np
    d = pd.concat([monthly_kwh.rename("kwh"), s17t], axis=1, join="inner")
    d = d[d["paivia"] == d["paivia_kk"]].dropna(subset=["kwh", "s17"])
    if len(d) < 12:
        return None
    X = np.c_[d["paivia_kk"], d["s17"]]
    y = d["kwh"].to_numpy()
    (a, b), *_ = np.linalg.lstsq(X, y, rcond=None)
    pred = X @ [a, b]
    r2 = 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    return dict(a=a, b=b, r2=r2, n=len(d), data=d.assign(pred=pred))


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
    st.plotly_chart(fig, width="stretch")

    partial = d[d["osittainen"] == 1]
    if not partial.empty:
        st.caption("Mittaus vajaa osalta kuukautta: " +
                   ", ".join(f"{r.kohde} ({KK[r.kk - 1]})" for r in partial.itertuples()) +
                   ". Mittaamaton osa sisältyy taloussähköön.")

    with st.expander("Taulukko"):
        tab = parts.pivot_table(index="kohde", columns="kk", values="kwh", aggfunc="sum")
        tab = tab.reindex([k for k in order if k in tab.index])
        tab.loc["Kokonaiskulutus"] = total
        tab.loc["Lämpötila °C"] = pd.Series(tvals, index=months, dtype="float")
        tab.columns = [KK[m - 1] for m in tab.columns]
        st.dataframe(tab.round(1), width="stretch")


def yearly_view(df, order, cmap):
    parts = df[df["tyyppi"].isin(["eritelty", "talous"])]
    yearly = parts.pivot_table(index="vuosi", columns="kohde", values="kwh", aggfunc="sum").fillna(0)
    totals = df[df["tyyppi"] == "kokonais"].groupby("vuosi")["kwh"].sum()
    nmonths = df[df["tyyppi"] == "kokonais"].groupby("vuosi")["kk"].nunique()
    years = list(yearly.index)

    # Tunnusluvut viimeisimmältä vuodelta
    y = years[-1]
    cols = st.columns(4)
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
    try:
        s = s17_table(list(per))
        idx = s["s17"].sum() / s["normaali_jakso"].sum() * 100 if not s.empty else None
    except Exception:
        idx = None
    cols[3].metric(f"Lämmitystarve {y}", f"{idx:.0f} % normaalista" if idx else "–",
                   help="Lämmitystarveluku S17 suhteessa oman sääaseman normaalivuoteen "
                        f"({NORMAL_YEARS.start}–{NORMAL_YEARS.stop - 1}) samalta jaksolta. "
                        "Yli 100 % = normaalia kylmempi.")

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
    st.plotly_chart(fig, width="stretch")

    # Osuudet
    share = yearly.T.reindex([k for k in order if k in yearly.columns])
    pct = share.div(share.sum(), axis=1) * 100
    out = pd.DataFrame({f"{v} kWh": share[v].round(0) for v in years} |
                       {f"{v} %": pct[v].round(1) for v in years})
    st.subheader("Kohteiden osuudet")
    st.dataframe(out, width="stretch")

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
    st.plotly_chart(fig2, width="stretch")

    st.info("🚗 Kesäkuusta 2025 alkaen taloudessa on kaksi sähköautoa. "
            "Vuoden 2026 alusta autot on ladattu kotona, koska veroetu muuttui.")
    st.caption("Shelly-erittely alkaa tammikuusta 2024 (15.10.2026 asti Shellyn historiasta, "
               "sen jälkeen suoraan mittauksista). Aiemmat vuodet eivät ole erittelyltään vertailukelpoisia: "
               "niillä mittaamaton kulutus sisältyy taloussähköön.")


# --- Ryhmävertailu -----------------------------------------------------------

# Kohde -> ryhmä. Listaamattomat eritellyt kohteet menevät taloussähköön.
GROUPS = {
    "Lämmitys": ["Varasto lämmitys", "Maalämpö", "IVK",
                 "AT lämmitys", "AT kuivaus", "AT ILP"],
    "Sähköauto": ["Sähköauto"],
    "Sauna": ["Sauna"],
    "Taloussähkö": ["Taloussähkö", "Kuivauskaappi"],
}
GROUP_COLORS = {"Lämmitys": "#eb6834", "Sähköauto": "#2a78d6",
                "Sauna": "#eda100", "Taloussähkö": TALOUS_COLOR}
# Vuodet yhdellä sävyllä vaaleasta tummaan: uusin vuosi tummin
YEAR_RAMP = ["#c9dcf4", "#94bbeb", "#5e98e0", "#2a78d6", "#1d5aa6", "#123d72"]
PREV_YEAR_FROM = 2024  # Shelly-erittely alkaa tästä vuodesta


def group_of(kohde):
    for g, items in GROUPS.items():
        if kohde in items:
            return g
    return "Taloussähkö"


def group_view(df):
    parts = df[df["tyyppi"].isin(["eritelty", "talous"])].copy()
    parts["ryhma"] = parts["kohde"].map(group_of)
    all_years = sorted(parts["vuosi"].unique())
    last_year = all_years[-1]
    last_month = int(parts.loc[parts["vuosi"] == last_year, "kk"].max())
    # Vertailuun vain täydet kuukaudet: kuluva kuukausi jätetään pois.
    # Tammikuussa ei ole vielä yhtään täyttä kuukautta, jolloin verrataan koko vuosia.
    today = date.today()
    running = (last_year, last_month) == (today.year, today.month)
    last_full = last_month - 1 if running else last_month
    if last_full == 0:
        last_full = 12

    # Lämpötilamalli kaikista kuukausista, joilla lämmitys on mitattu
    heat = parts[parts["ryhma"] == "Lämmitys"].groupby("kuukausi")["kwh"].sum()
    heat = heat[heat > 0]
    try:
        s17t = s17_table(list(heat.index))
        model = heating_model(heat, s17t) if not s17t.empty else None
    except Exception as e:
        s17t, model = pd.DataFrame(), None
        st.caption(f"Lämmitystarvelukuja ei saatu: {e}")

    c1, c2, c3 = st.columns([3, 2, 2])
    years = c1.multiselect("Vuodet", all_years,
                           default=[y for y in all_years if y >= PREV_YEAR_FROM] or all_years)
    same = c2.toggle(f"Vain tammi–{KK[last_full - 1]} joka vuodelta", value=True,
                     help="Vertaa samoja täysiä kuukausia joka vuodelta (YTD). "
                          "Kuluva kuukausi ei ole mukana.")
    corrected = c3.toggle("Lämpötilakorjattu lämmitys", value=False, disabled=model is None,
                          help="Lämmitys muunnetaan normaalivuoden säähän (oman sääaseman "
                               f"{NORMAL_YEARS.start}–{NORMAL_YEARS.stop - 1} keskiarvo). "
                               "Muut ryhmät pysyvät toteutuneina.")
    if not years:
        st.info("Valitse vähintään yksi vuosi.")
        return
    years = sorted(years)
    d = parts[parts["vuosi"].isin(years)]
    if same:
        d = d[d["kk"] <= last_full]
        if running:
            d = d[~((d["vuosi"] == last_year) & (d["kk"] == last_month))]

    tab = (d.pivot_table(index="ryhma", columns="vuosi", values="kwh", aggfunc="sum")
           .reindex(index=list(GROUPS), columns=years).fillna(0))
    period = f"tammi–{KK[last_full - 1]}" if same else "koko vuosi"

    # Sää vertailujaksolla ja lämpötilakorjaus: kWh − b · (S17 − normaali)
    months = pd.Index(sorted(d["kuukausi"].unique()))
    sj = s17t.reindex(months).dropna(subset=["s17"]) if not s17t.empty else pd.DataFrame()
    if not sj.empty:
        sj["vuosi"] = [p.year for p in sj.index]
        wy = sj.groupby("vuosi")[["s17", "normaali_jakso"]].sum()
        cols = st.columns(len(years))
        for col, y in zip(cols, years):
            if y in wy.index and wy.loc[y, "normaali_jakso"]:
                v = wy.loc[y, "s17"] / wy.loc[y, "normaali_jakso"] * 100
                col.metric(f"Lämmitystarve {y}", f"{v:.0f} %",
                           help=f"S17 {wy.loc[y, 's17']:.0f} / normaali "
                                f"{wy.loc[y, 'normaali_jakso']:.0f}, {period}. "
                                "Yli 100 % = normaalia kylmempi.")
    if corrected and model is not None and not sj.empty:
        hm = sj.index.intersection(heat.index)
        adj = (-model["b"] * (sj.loc[hm, "s17"] - sj.loc[hm, "normaali_jakso"]))
        for y, v in adj.groupby([p.year for p in hm]).sum().items():
            if y in tab.columns:
                tab.loc["Lämmitys", y] += v
    pct = tab.div(tab.sum(), axis=1) * 100
    if corrected:
        period += ", lämmitys lämpötilakorjattu"
    ramp = YEAR_RAMP[-len(years):] if len(years) <= len(YEAR_RAMP) else YEAR_RAMP
    ycol = {y: ramp[min(i, len(ramp) - 1)] for i, y in enumerate(years)}

    # 1) kWh: ryhmät vierekkäin, vuodet rinnakkaisina pylväinä
    fig = go.Figure()
    for y in years:
        fig.add_bar(x=list(GROUPS), y=tab[y], name=str(y),
                    marker=dict(color=ycol[y], line=dict(width=2, color="rgba(255,255,255,0.9)")),
                    text=[fmt(v) for v in tab[y]], textposition="outside",
                    textfont=dict(size=11),
                    hovertemplate=f"<b>%{{x}}</b> {y}<br>%{{y:,.0f}} kWh<extra></extra>")
    fig.update_layout(title=f"Kulutus ryhmittäin, {period} (kWh)", barmode="group",
                      bargap=0.3, bargroupgap=0.06, height=440,
                      legend=dict(orientation="h", y=-0.12), margin=dict(l=10, r=10, t=50, b=10))
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.15)")
    st.plotly_chart(fig, width="stretch")

    # 2) %: jokainen vuosi 100 %:n palkkina, ryhmien värit kiinteät
    fig2 = go.Figure()
    for g in GROUPS:
        fig2.add_bar(y=[str(y) for y in years], x=pct.loc[g], name=g, orientation="h",
                     marker=dict(color=GROUP_COLORS[g], line=dict(width=2, color="rgba(255,255,255,0.9)")),
                     text=[f"{v:.0f} %" if v >= 5 else "" for v in pct.loc[g]],
                     textposition="inside", insidetextanchor="middle",
                     customdata=tab.loc[g],
                     hovertemplate=f"<b>{g}</b> %{{y}}<br>%{{x:.1f}} % · %{{customdata:,.0f}} kWh<extra></extra>")
    fig2.update_layout(title=f"Osuudet kokonaiskulutuksesta, {period} (%)", barmode="stack",
                       bargap=0.35, height=120 + 60 * len(years),
                       legend=dict(orientation="h", y=-0.25, traceorder="normal"),
                       margin=dict(l=10, r=10, t=50, b=10))
    fig2.update_xaxes(range=[0, 100], ticksuffix=" %", gridcolor="rgba(128,128,128,0.15)")
    fig2.update_yaxes(type="category", autorange="reversed")
    st.plotly_chart(fig2, width="stretch")

    # 3) Taulukko: kWh, % ja muutos edelliseen valittuun vuoteen
    def change(a, b):
        return f"{(a / b - 1) * 100:+.0f} %" if b else "–"

    full = pd.concat([tab, tab.sum().to_frame("Yhteensä").T])
    out = pd.DataFrame(index=full.index)
    for i, y in enumerate(years):
        out[f"{y} kWh"] = full[y].round(0)
        out[f"{y} %"] = (full[y] / tab[y].sum() * 100).round(1) if tab[y].sum() else 0.0
        if i:
            p = years[i - 1]
            out[f"muutos {p}→{y}"] = [change(a, b) for a, b in zip(full[y], full[p])]
    st.dataframe(out, width="stretch")

    other = sorted(set(parts["kohde"]) - {k for v in GROUPS.values() for k in v})
    st.caption("Lämmitys = " + " + ".join(GROUPS["Lämmitys"]) +
               ". Taloussähkö sisältää kuivauskaapin" +
               (" sekä " + ", ".join(other) if other else "") + ". "
               f"Ennen vuotta {PREV_YEAR_FROM} lämmityksen osuus sisältyy taloussähköön, "
               "koska kohteita ei vielä mitattu erikseen.")

    if model is not None:
        with st.expander("Lämpötilamalli"):
            md = model["data"]
            st.markdown(
                f"Lämmitys ≈ **{fmt(model['a'], 1)} kWh/vrk** (säästä riippumaton osa: "
                f"käyttövesi, IVK, kuivaus) + **{fmt(model['b'], 2)} kWh** jokaista "
                f"lämmitystarveluvun astepäivää kohden. Selitysaste R² = "
                f"**{fmt(model['r2'], 2)}**, {model['n']} täyttä kuukautta.")
            normal_year = load_s17_static()
            normal_year = normal_year[normal_year.index.year.isin(NORMAL_YEARS)]
            st.caption(
                "Lämmitystarveluku S17 (Ilmatieteen laitoksen menetelmä): päivät, joiden "
                "keskilämpötila on keväällä alle +10 °C ja syksyllä alle +12 °C, summana "
                f"17 °C − päivän keskilämpötila. Normaalivuosi = oman sääaseman "
                f"{NORMAL_YEARS.start}–{NORMAL_YEARS.stop - 1} keskiarvo, "
                f"{fmt(normal_year.groupby(normal_year.index.month)['s17'].mean().sum())} "
                "astepäivää. Korjattu = toteutunut − kerroin × (S17 − normaali). Kertoimet "
                "lasketaan uudelleen aina, kun dataa kertyy.")
            x = [0, float(md["s17"].max())]
            figm = go.Figure()
            figm.add_scatter(x=md["s17"], y=md["kwh"], mode="markers", name="Kuukausi",
                             marker=dict(size=9, color=GROUP_COLORS["Lämmitys"],
                                         line=dict(width=2, color="rgba(255,255,255,0.9)")),
                             text=[str(p) for p in md.index],
                             hovertemplate="%{text}<br>S17 %{x:.0f} · %{y:,.0f} kWh<extra></extra>")
            figm.add_scatter(x=x, y=[model["a"] * 30.4 + model["b"] * v for v in x],
                             mode="lines", name="Malli (30 vrk kuukausi)",
                             line=dict(color="#5f5e5a", width=2, dash="dash"), hoverinfo="skip")
            figm.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10),
                               legend=dict(orientation="h", y=-0.2),
                               xaxis_title="Lämmitystarveluku S17 (kk)", yaxis_title="kWh / kk")
            figm.update_xaxes(gridcolor="rgba(128,128,128,0.15)")
            figm.update_yaxes(gridcolor="rgba(128,128,128,0.15)", rangemode="tozero")
            st.plotly_chart(figm, width="stretch")


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
    if c2.button("🔄 Päivitä data", width="stretch"):
        load_energy.clear()
        st.rerun()

    t1, t2, t3 = st.tabs(["Kuukausiraportti", "Vuosikooste", "Ryhmävertailu"])
    with t1:
        monthly_view(df, order, cmap)
    with t2:
        yearly_view(df, order, cmap)
    with t3:
        group_view(df)
