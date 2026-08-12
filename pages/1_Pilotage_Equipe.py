from __future__ import annotations

import html
import re
import sqlite3
import unicodedata
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


st.set_page_config(
    page_title="Pulse Direction — Free Énergie",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

ACCENT = "#ff6b2c"
NAVY = "#10233f"
EXCLUDED = {"ANTHONY BOUVIER", "MEHDI DIFALLAH"}
PROJECT = Path(__file__).resolve().parents[1]

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@600;700;800&display=swap');
:root { --ink:#10233f; --muted:#708096; --orange:#ff6b2c; --cream:#f7f8fa; }
html, body, [class*="css"] { font-family:'DM Sans',sans-serif; }
.stApp { background:linear-gradient(145deg,#f7f8fa 0%,#fff 48%,#fff7f1 100%); }
.block-container { max-width:1540px; padding:1.15rem 2rem 4rem; }
h1,h2,h3 { font-family:'Manrope',sans-serif!important; color:var(--ink)!important; }
[data-testid="stHeader"] { background:transparent; }
[data-testid="stSidebar"] { background:#fff; border-right:1px solid #e7ebf0; }
.hero { position:relative; overflow:hidden; background:linear-gradient(120deg,#10233f,#183a65 68%,#315e8f); border-radius:28px; padding:30px 34px; color:white; box-shadow:0 22px 55px rgba(16,35,63,.18); margin-bottom:18px; }
.hero:after { content:""; position:absolute; width:360px; height:360px; right:-130px; top:-210px; border-radius:50%; background:radial-gradient(circle,rgba(255,107,44,.65),rgba(255,107,44,0)); }
.hero-kicker { text-transform:uppercase; letter-spacing:.16em; color:#ffb38f; font-weight:700; font-size:.74rem; }
.hero-title { font-family:'Manrope'; font-size:2.25rem; font-weight:800; margin:.35rem 0 .25rem; }
.hero-sub { color:#cbd8e7; font-size:.98rem; }
.sync { display:inline-flex; gap:7px; align-items:center; margin-top:15px; padding:7px 12px; border:1px solid rgba(255,255,255,.16); background:rgba(255,255,255,.08); border-radius:999px; font-size:.79rem; }
.sync-dot { width:8px; height:8px; border-radius:50%; background:#55db9a; box-shadow:0 0 0 5px rgba(85,219,154,.12); }
[data-testid="stMetric"] { background:rgba(255,255,255,.92); border:1px solid #e6eaf0; border-radius:20px; padding:17px 18px; box-shadow:0 10px 30px rgba(16,35,63,.06); }
[data-testid="stMetricLabel"] { color:#718096; font-weight:600; }
[data-testid="stMetricValue"] { font-family:'Manrope'; color:#10233f; font-size:1.72rem; }
.section-title { display:flex; align-items:center; gap:10px; margin:26px 0 12px; color:#10233f; font-family:'Manrope'; font-weight:800; font-size:1.15rem; }
.section-title span { width:9px; height:9px; border-radius:50%; background:#ff6b2c; box-shadow:0 0 0 6px rgba(255,107,44,.10); }
.person-head { background:#fff; border:1px solid #e5eaf0; border-radius:24px; padding:22px 25px; box-shadow:0 12px 35px rgba(16,35,63,.07); margin:10px 0 15px; }
.avatar { width:52px; height:52px; display:flex; align-items:center; justify-content:center; border-radius:16px; color:white; font-family:'Manrope'; font-size:1.1rem; font-weight:800; background:linear-gradient(135deg,#ff6b2c,#ff9b68); float:left; margin-right:14px; }
.person-name { font-family:'Manrope'; color:#10233f; font-size:1.35rem; font-weight:800; padding-top:2px; }
.person-state { color:#708096; font-size:.88rem; }
.insight { min-height:122px; background:#fff; border:1px solid #e6eaf0; border-radius:20px; padding:18px; box-shadow:0 8px 28px rgba(16,35,63,.05); }
.insight-label { color:#77869a; font-size:.75rem; text-transform:uppercase; letter-spacing:.08em; font-weight:700; }
.insight-value { color:#10233f; font-family:'Manrope'; font-size:1.12rem; font-weight:800; margin:11px 0 5px; }
.insight-note { color:#8491a3; font-size:.79rem; }
.warning-card { background:linear-gradient(135deg,#fff8f3,#fff); border:1px solid #ffd9c6; border-radius:20px; padding:18px; }
.stButton>button { border-radius:15px; border:1px solid #dfe5ec; background:#fff; color:#183a65; font-weight:700; min-height:48px; transition:.2s ease; }
.stButton>button:hover { border-color:#ff6b2c; color:#ff6b2c; transform:translateY(-1px); box-shadow:0 8px 18px rgba(255,107,44,.12); }
div[data-testid="stPlotlyChart"] { background:#fff; border:1px solid #e6eaf0; border-radius:22px; padding:8px; box-shadow:0 10px 30px rgba(16,35,63,.05); }
[data-testid="stDataFrame"] { border:1px solid #e5eaf0; border-radius:18px; overflow:hidden; }
.quiet { color:#7b8798; font-size:.82rem; }
@media(max-width:800px){ .block-container{padding:1rem}.hero{padding:24px}.hero-title{font-size:1.7rem} }
</style>
""",
    unsafe_allow_html=True,
)


def clean(value) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def norm(value) -> str:
    text = unicodedata.normalize("NFD", clean(value))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[_\s]+", " ", text).strip().upper()


def safe_table(name: str) -> pd.DataFrame:
    try:
        from database_cloud import read_dataframe

        return read_dataframe(f'SELECT * FROM "{name}"')
    except Exception:
        for path in (PROJECT / "data" / "leads.sqlite", PROJECT / "leads.sqlite"):
            if path.exists():
                with sqlite3.connect(path) as connection:
                    exists = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                    ).fetchone()
                    if exists:
                        return pd.read_sql_query(f'SELECT * FROM "{name}"', connection)
        return pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def load_data():
    return (
        safe_table("leads"),
        safe_table("call_events"),
        safe_table("calendar_events"),
        safe_table("calendar_daily_activity"),
    )


def column(df: pd.DataFrame, *names: str):
    mapping = {str(c).lower(): c for c in df.columns}
    return next((mapping[n.lower()] for n in names if n.lower() in mapping), None)


def person_initials(person: str) -> str:
    words = [w for w in clean(person).split() if w]
    return "".join(w[0] for w in words[:2]).upper() or "FE"


def fake_person(value) -> bool:
    """Détecte les faux noms produits par certains blocs d'historique CRM."""
    text = clean(value)
    normalized = norm(text)
    return (
        not text
        or normalized.startswith("NRP")
        or bool(re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", text))
        or bool(re.search(r"\d{1,2}:\d{2}", text))
        or len(text) > 80
    )


raw_leads, raw_calls, raw_events, raw_daily = load_data()
if raw_leads.empty:
    st.error("Les données CRM ne sont pas encore disponibles.")
    st.stop()

leads = raw_leads.copy()
owner_col = column(leads, "intervenant", "commercial", "collaborateur")
id_col = column(leads, "crm_id", "lead_id", "id")
status_col = column(leads, "statut", "status")
created_col = column(leads, "date_creation", "created_at")
last_col = column(leads, "dernier_appel", "last_call")
nrp_col = column(leads, "nombre_nrp", "nrp")
name_col = column(leads, "nom", "name")
first_col = column(leads, "prenom", "first_name")
sync_col = column(leads, "synced_at", "updated_at")

leads["_person"] = leads[owner_col].fillna("").astype(str).map(clean) if owner_col else ""
leads["_person_n"] = leads["_person"].map(norm)
leads["_id"] = leads[id_col].astype(str) if id_col else leads.index.astype(str)
leads["_status"] = leads[status_col].fillna("").astype(str).map(norm) if status_col else ""
leads["_created"] = pd.to_datetime(leads[created_col], errors="coerce", dayfirst=True) if created_col else pd.NaT
leads["_last"] = pd.to_datetime(leads[last_col], errors="coerce", dayfirst=True) if last_col else pd.NaT
leads["_nrp"] = pd.to_numeric(leads[nrp_col], errors="coerce").fillna(0).astype(int) if nrp_col else 0
leads["_prospect"] = leads[name_col].fillna("").astype(str) if name_col else leads["_id"]
if first_col:
    leads["_prospect"] = (leads[first_col].fillna("").astype(str) + " " + leads["_prospect"]).str.strip()
leads = leads[(leads["_person"].ne("")) & (~leads["_person_n"].isin(EXCLUDED))].copy()

calls = raw_calls.copy()
if not calls.empty:
    call_person = column(calls, "commercial", "intervenant", "collaborateur")
    call_id = column(calls, "crm_id", "lead_id")
    call_date = column(calls, "event_datetime", "datetime", "date_heure")
    owner_map = leads.drop_duplicates("_id", keep="last").set_index("_id")["_person"].to_dict()
    calls["_id"] = calls[call_id].astype(str) if call_id else calls.index.astype(str)
    calls["_person"] = calls[call_person].fillna("").astype(str).map(clean) if call_person else ""
    invalid_person = calls["_person"].map(fake_person) | calls["_person"].map(norm).isin(EXCLUDED)
    calls.loc[invalid_person, "_person"] = calls.loc[invalid_person, "_id"].map(owner_map).fillna("")
    calls["_person_n"] = calls["_person"].map(norm)
    calls["_dt"] = pd.to_datetime(calls[call_date], errors="coerce", dayfirst=True) if call_date else pd.NaT
    calls = calls[calls["_dt"].notna() & calls["_person"].ne("") & ~calls["_person_n"].isin(EXCLUDED)].copy()
else:
    calls = pd.DataFrame(columns=["_id", "_person", "_person_n", "_dt"])

events = raw_events.copy()
if not events.empty:
    event_person = column(events, "commercial", "intervenant")
    event_date = column(events, "event_date", "date")
    event_title = column(events, "title", "name")
    event_status = column(events, "status_kind", "appointment_status")
    event_color = column(events, "color_hex", "calendar_color")
    events["_person"] = events[event_person].fillna("").astype(str).map(clean) if event_person else ""
    events["_person_n"] = events["_person"].map(norm)
    events["_date"] = pd.to_datetime(events[event_date], errors="coerce", dayfirst=True).dt.normalize() if event_date else pd.NaT
    events["_title"] = events[event_title].fillna("").astype(str) if event_title else ""
    events["_status"] = events[event_status].fillna("").astype(str) if event_status else ""
    events["_color"] = events[event_color].fillna("").astype(str) if event_color else ""
    events = events[events["_date"].notna() & events["_title"].str.contains(r"(?<![A-Za-z0-9])R\s*[12](?![A-Za-z0-9])", case=False, regex=True, na=False)].copy()
    missing_status = events["_status"].eq("")
    events.loc[missing_status & (events["_date"] >= pd.Timestamp.now().normalize()), "_status"] = "scheduled"
    events.loc[missing_status & (events["_date"] < pd.Timestamp.now().normalize()), "_status"] = "unknown"
else:
    events = pd.DataFrame(columns=["_person", "_person_n", "_date", "_title", "_status", "_color"])

now = pd.Timestamp.now()
period_options = {"7 jours": 7, "14 jours": 14, "30 jours": 30, "8 semaines": 56}
with st.sidebar:
    st.markdown("### Vue de direction")
    period_label = st.radio("Période analysée", list(period_options), index=2)
    st.caption("Les jours ratés excluent les week-ends et la journée en cours.")
    if st.button("Actualiser les données", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

days = period_options[period_label]
start = (now - pd.Timedelta(days=days)).normalize()
period_calls = calls[calls["_dt"] >= start].copy()
period_events = events[events["_date"] >= start].copy()

people = sorted(leads["_person"].dropna().unique(), key=norm)
if not people:
    st.warning("Aucun commercial exploitable.")
    st.stop()

business_days = pd.bdate_range(start=start, end=now.normalize() - pd.Timedelta(days=1))
rows = []
missed_by_person: dict[str, list[pd.Timestamp]] = {}
for person in people:
    pn = norm(person)
    lp = leads[leads["_person_n"] == pn]
    cp = period_calls[period_calls["_person_n"] == pn]
    ep = period_events[period_events["_person_n"] == pn]
    call_days = set(cp["_dt"].dt.normalize())
    rdv_days = set(ep["_date"])
    missed = [d for d in business_days if d not in call_days and d not in rdv_days]
    missed_by_person[person] = missed
    strategic = cp[((cp["_dt"].dt.hour >= 12) & (cp["_dt"].dt.hour < 14)) | ((cp["_dt"].dt.hour * 60 + cp["_dt"].dt.minute) >= 1110)]
    completed_rdv = int(ep["_status"].eq("completed").sum())
    cancelled_rdv = int(ep["_status"].eq("cancelled").sum())
    pending_rdv = int(ep["_status"].eq("pending_debrief").sum())
    past_rdv = ep[ep["_date"] < now.normalize()]
    completion_rate = round(completed_rdv / max(len(past_rdv), 1) * 100)
    relances = lp[lp["_status"].str.contains("A RELANCER", na=False)]
    rows.append({
        "Commercial": person,
        "Appels": len(cp),
        "Leads actifs": len(lp),
        "À relancer": len(relances),
        "NRP": int(lp["_nrp"].sum()),
        "R1/R2": len(ep),
        "Effectués": completed_rdv,
        "Non débriefés": pending_rdv,
        "Annulés": cancelled_rdv,
        "Taux effectué": completion_rate,
        "Jours ratés": len(missed),
        "Appels stratégiques": len(strategic),
        "Dernier NRP": cp["_dt"].max() if not cp.empty else pd.NaT,
        "Score": max(0, min(100, round(100 - len(missed) * 5 - len(relances) * .7 + min(12, len(strategic))))),
    })

team = pd.DataFrame(rows).sort_values(["Jours ratés", "Appels"], ascending=[False, True]).reset_index(drop=True)
if "selected_person" not in st.session_state or st.session_state.selected_person not in people:
    st.session_state.selected_person = team.iloc[0]["Commercial"]

latest_sync = pd.to_datetime(leads[sync_col], errors="coerce").max() if sync_col else pd.NaT
sync_text = latest_sync.strftime("%d/%m/%Y à %H:%M") if pd.notna(latest_sync) else "synchronisation cloud active"
st.markdown(
    f"""
    <div class="hero">
      <div class="hero-kicker">Free Énergie · Intelligence commerciale</div>
      <div class="hero-title">Pulse Direction</div>
      <div class="hero-sub">L'activité réelle de l'équipe. Les signaux faibles deviennent des décisions.</div>
      <div class="sync"><span class="sync-dot"></span>Données actualisées : {sync_text}</div>
    </div>
    """,
    unsafe_allow_html=True,
)

total_missed = int(team["Jours ratés"].sum())
total_strategic = int(team["Appels stratégiques"].sum())
latest_team_call = calls["_dt"].max() if not calls.empty else pd.NaT
future_events = events[events["_date"] >= now.normalize()]
past_events = events[events["_date"] < now.normalize()]
completed_total = int(events["_status"].eq("completed").sum())
pending_total = int(events["_status"].eq("pending_debrief").sum())
cancelled_total = int(events["_status"].eq("cancelled").sum())
completion_total = round(completed_total / max(len(past_events), 1) * 100)
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("R1/R2 à venir", len(future_events))
k2.metric("Taux effectués", f"{completion_total}%")
k3.metric("Non débriefés", pending_total, delta="À traiter" if pending_total else "RAS", delta_color="inverse")
k4.metric("Annulés / non faits", cancelled_total)
k5.metric("Jours sans action", total_missed, delta="À expliquer" if total_missed else "RAS", delta_color="inverse")

st.markdown('<div class="section-title"><span></span>Choisir un commercial</div>', unsafe_allow_html=True)
for offset in range(0, len(team), 5):
    cols = st.columns(5)
    for col_ui, (_, item) in zip(cols, team.iloc[offset:offset + 5].iterrows()):
        with col_ui:
            label = f"{item['Commercial']}\n\n{int(item['Appels'])} appels · {int(item['Jours ratés'])} jour(s) raté(s)"
            if st.button(label, key=f"person_{norm(item['Commercial'])}", use_container_width=True):
                st.session_state.selected_person = item["Commercial"]
                st.rerun()

person = st.session_state.selected_person
pn = norm(person)
row = team[team["Commercial"] == person].iloc[0]
pc = calls[calls["_person_n"] == pn].sort_values("_dt", ascending=False).copy()
p_period_calls = pc[pc["_dt"] >= start]
pl = leads[leads["_person_n"] == pn].copy()
pe = events[(events["_person_n"] == pn) & (events["_date"] >= start)].copy()
midday = pc[(pc["_dt"].dt.hour >= 12) & (pc["_dt"].dt.hour < 14)]
evening = pc[(pc["_dt"].dt.hour * 60 + pc["_dt"].dt.minute) >= 1110]
missed = missed_by_person.get(person, [])
prospect_map = leads.drop_duplicates("_id", keep="last").set_index("_id")["_prospect"].to_dict()

status_word = "À surveiller" if row["Jours ratés"] else "Rythme maîtrisé"
st.markdown(
    f"""
    <div class="person-head">
      <div class="avatar">{person_initials(person)}</div>
      <div class="person-name">{html.escape(person)}</div>
      <div class="person-state">{status_word} · score d'activité {int(row['Score'])}/100 · période {period_label}</div>
      <div style="clear:both"></div>
    </div>
    """,
    unsafe_allow_html=True,
)

last_nrp = pc["_dt"].max() if not pc.empty else pd.NaT
last_nrp_name = prospect_map.get(str(pc.iloc[0]["_id"]), "Lead CRM") if not pc.empty else "Aucun prospect"
last_midday = midday["_dt"].max() if not midday.empty else pd.NaT
last_evening = evening["_dt"].max() if not evening.empty else pd.NaT
i1, i2, i3, i4 = st.columns(4)
cards = [
    (i1, "Dernier lead appelé en NRP", last_nrp.strftime("%d/%m/%Y · %H:%M") if pd.notna(last_nrp) else "Aucun historique", f"{last_nrp_name} · {int(row['Appels'])} appel(s)"),
    (i2, "Jour sans R1/R2 et sans appel", f"{len(missed)} jour(s)", missed[-1].strftime("Dernier : %d/%m/%Y") if missed else "Aucun jour raté"),
    (i3, "Dernier appel entre 12h et 14h", last_midday.strftime("%d/%m/%Y · %H:%M") if pd.notna(last_midday) else "Jamais", f"{len(midday[midday['_dt'] >= start])} sur la période"),
    (i4, "Dernier appel après 18h30", last_evening.strftime("%d/%m/%Y · %H:%M") if pd.notna(last_evening) else "Jamais", f"{len(evening[evening['_dt'] >= start])} sur la période"),
]
for target, label, value, note in cards:
    with target:
        st.markdown(f'<div class="insight"><div class="insight-label">{label}</div><div class="insight-value">{value}</div><div class="insight-note">{note}</div></div>', unsafe_allow_html=True)

st.markdown('<div class="section-title"><span></span>Rendez-vous R1 / R2</div>', unsafe_allow_html=True)
status_labels = {
    "completed": "Effectué",
    "pending_debrief": "Non débriefé",
    "cancelled": "Annulé / non effectué",
    "scheduled": "À venir",
    "unknown": "État à confirmer",
}
status_colors = {
    "completed": "🟢",
    "pending_debrief": "🟡",
    "cancelled": "🔴",
    "scheduled": "🔵",
    "unknown": "⚪",
}
person_future = pe[pe["_date"] >= now.normalize()].sort_values(["_date"])
person_past = pe[pe["_date"] < now.normalize()].sort_values(["_date"], ascending=False)
rdv_a, rdv_b = st.tabs([f"À venir · {len(person_future)}", f"Passés · {len(person_past)}"])

def appointment_view(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    view = frame.copy()
    view["État"] = view["_status"].map(lambda value: f"{status_colors.get(value, '⚪')} {status_labels.get(value, value)}")
    view["Date"] = view["_date"].dt.strftime("%d/%m/%Y")
    start_col = column(view, "start_time")
    view["Heure"] = view[start_col].fillna("—").astype(str) if start_col else "—"
    view["Rendez-vous"] = view["_title"]
    return view[["Date", "Heure", "Rendez-vous", "État"]]

with rdv_a:
    future_view = appointment_view(person_future)
    if future_view.empty:
        st.info("Aucun R1/R2 à venir détecté pour ce commercial.")
    else:
        st.dataframe(future_view.head(30), use_container_width=True, hide_index=True, height=min(360, 38 * len(future_view.head(30)) + 38))
with rdv_b:
    past_view = appointment_view(person_past)
    if past_view.empty:
        st.info("Aucun R1/R2 passé sur cette période.")
    else:
        st.dataframe(past_view.head(30), use_container_width=True, hide_index=True, height=min(360, 38 * len(past_view.head(30)) + 38))

st.markdown('<div class="section-title"><span></span>Rythme et discipline commerciale</div>', unsafe_allow_html=True)
left, right = st.columns([1.55, 1])
with left:
    if p_period_calls.empty:
        st.info("Aucun appel enregistré sur cette période.")
    else:
        daily = p_period_calls.assign(Jour=p_period_calls["_dt"].dt.normalize()).groupby("Jour").size().reset_index(name="Appels")
        fig = px.bar(daily, x="Jour", y="Appels", color_discrete_sequence=[ACCENT])
        fig.update_traces(marker_line_width=0, hovertemplate="%{x|%d/%m}<br><b>%{y} appels</b><extra></extra>")
        fig.update_layout(title="Appels par jour", height=350, margin=dict(l=18, r=18, t=55, b=20), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_family="DM Sans", xaxis_title=None, yaxis_title=None, showlegend=False)
        fig.update_xaxes(showgrid=False)
        fig.update_yaxes(gridcolor="#edf0f4")
        st.plotly_chart(fig, use_container_width=True)
with right:
    hours = p_period_calls.assign(Heure=p_period_calls["_dt"].dt.hour + p_period_calls["_dt"].dt.minute / 60)
    fig = go.Figure()
    fig.add_vrect(x0=12, x1=14, fillcolor="#ff6b2c", opacity=.10, line_width=0)
    fig.add_vrect(x0=18.5, x1=24, fillcolor="#183a65", opacity=.08, line_width=0)
    fig.add_trace(go.Histogram(x=hours["Heure"], xbins=dict(start=8, end=24, size=1), marker_color=NAVY, hovertemplate="%{x:.0f}h : %{y} appels<extra></extra>"))
    fig.update_layout(title="Répartition horaire", height=350, margin=dict(l=18, r=18, t=55, b=20), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_family="DM Sans", xaxis_title=None, yaxis_title=None, bargap=.18, showlegend=False)
    fig.update_xaxes(range=[8, 24], dtick=2, showgrid=False)
    fig.update_yaxes(gridcolor="#edf0f4")
    st.plotly_chart(fig, use_container_width=True)

detail_left, detail_right = st.columns([1, 1.25])
with detail_left:
    st.markdown('<div class="section-title"><span></span>Calendrier d’activité</div>', unsafe_allow_html=True)
    person_call_days = set(p_period_calls["_dt"].dt.normalize())
    person_rdv_days = set(pe["_date"])
    french_days = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
    calendar_cells = []
    for activity_day in business_days[-30:]:
        has_call = activity_day in person_call_days
        has_rdv = activity_day in person_rdv_days
        if has_rdv:
            css_class, icon, label = "#e8f8ef", "●", "R1/R2"
            icon_color = "#20a464"
        elif has_call:
            css_class, icon, label = "#fff4e9", "●", "Appels"
            icon_color = "#f08a33"
        else:
            css_class, icon, label = "#fff0f1", "●", "Sans action"
            icon_color = "#e24d5d"
        calendar_cells.append(
            f'<div style="background:{css_class};border-radius:12px;padding:9px 7px;min-width:76px;text-align:center">'
            f'<div style="font-weight:800;color:#10233f">{french_days[activity_day.weekday()]} {activity_day.day:02d}</div>'
            f'<div style="font-size:.72rem;color:{icon_color};margin-top:3px">{icon} {label}</div></div>'
        )
    st.markdown(
        '<div style="display:flex;gap:7px;flex-wrap:wrap">' + "".join(calendar_cells) + "</div>"
        '<div class="quiet" style="margin-top:10px">🟢 R1/R2 · 🟠 appels sans RDV · 🔴 aucune action</div>',
        unsafe_allow_html=True,
    )
with detail_right:
    st.markdown('<div class="section-title"><span></span>Dernières actions</div>', unsafe_allow_html=True)
    if pc.empty:
        st.info("Aucun appel disponible.")
    else:
        last_actions = pc.head(20)[["_dt", "_id"]].copy()
        last_actions["Prospect"] = last_actions["_id"].map(prospect_map).fillna("Lead CRM")
        last_actions["Date"] = last_actions["_dt"].dt.strftime("%d/%m/%Y %H:%M")
        st.dataframe(last_actions[["Date", "Prospect"]], use_container_width=True, hide_index=True, height=390)

st.markdown('<div class="section-title"><span></span>Vue comparative de l’équipe</div>', unsafe_allow_html=True)
compare = team.copy()
compare["Appels / jour"] = (compare["Appels"] / max(len(business_days), 1)).round(1)
selected_compare = compare[compare["Commercial"] == person].iloc[0]
median_calls = compare["Appels / jour"].median()
median_missed = compare["Jours ratés"].median()
median_rdv = compare["R1/R2"].median()
c1, c2, c3 = st.columns(3)
c1.metric("Appels/jour vs équipe", selected_compare["Appels / jour"], delta=f"{selected_compare['Appels / jour'] - median_calls:+.1f} vs médiane")
c2.metric("Jours sans action vs équipe", int(selected_compare["Jours ratés"]), delta=f"{selected_compare['Jours ratés'] - median_missed:+.0f} vs médiane", delta_color="inverse")
c3.metric("R1/R2 vs équipe", int(selected_compare["R1/R2"]), delta=f"{selected_compare['R1/R2'] - median_rdv:+.0f} vs médiane")

rank_left, rank_right = st.columns(2)
with rank_left:
    calls_rank = compare.sort_values("Appels / jour", ascending=True)
    calls_rank["Couleur"] = calls_rank["Commercial"].map(lambda name: "Sélection" if name == person else "Équipe")
    fig_calls = px.bar(calls_rank, x="Appels / jour", y="Commercial", orientation="h", color="Couleur", color_discrete_map={"Sélection": ACCENT, "Équipe": "#d9e1eb"})
    fig_calls.update_layout(title="Intensité d'appels", height=390, margin=dict(l=15, r=15, t=55, b=20), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", showlegend=False, font_family="DM Sans", yaxis_title=None)
    fig_calls.update_xaxes(gridcolor="#edf0f4")
    st.plotly_chart(fig_calls, use_container_width=True)
with rank_right:
    missed_rank = compare.sort_values("Jours ratés", ascending=False)
    missed_rank["Couleur"] = missed_rank["Commercial"].map(lambda name: "Sélection" if name == person else "Équipe")
    fig_missed = px.bar(missed_rank, x="Jours ratés", y="Commercial", orientation="h", color="Couleur", color_discrete_map={"Sélection": ACCENT, "Équipe": "#d9e1eb"})
    fig_missed.update_layout(title="Jours sans action", height=390, margin=dict(l=15, r=15, t=55, b=20), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", showlegend=False, font_family="DM Sans", yaxis_title=None)
    fig_missed.update_xaxes(gridcolor="#edf0f4")
    st.plotly_chart(fig_missed, use_container_width=True)

with st.expander("Voir tous les chiffres de l'équipe"):
    st.dataframe(compare[["Commercial", "Appels / jour", "R1/R2", "Taux effectué", "Non débriefés", "Annulés", "Jours ratés", "À relancer"]], use_container_width=True, hide_index=True)
st.caption("Pulse Direction · Lecture managériale des données CRM et calendriers synchronisés dans Supabase.")
