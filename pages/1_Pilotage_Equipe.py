from __future__ import annotations

import re
import unicodedata

import pandas as pd
import streamlit as st

from database import read_table

st.set_page_config(
    page_title="Cockpit Direction — Free Énergie",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

EXCLUDED = {"ANTHONY BOUVIER", "MEHDI DIFALLAH"}

# -----------------------------
# DESIGN
# -----------------------------
st.markdown("""
<style>
.block-container{
    padding-top:1.4rem;
    padding-bottom:2rem;
    max-width:1500px;
}
[data-testid="stSidebar"]{
    min-width:280px;
}
h1{
    font-size:2.1rem !important;
    margin-bottom:.15rem !important;
}
h2{
    font-size:1.35rem !important;
    margin-top:.5rem !important;
}
h3{
    font-size:1.05rem !important;
}
div[data-testid="stMetric"]{
    border:1px solid #e6e8ec;
    border-radius:14px;
    padding:14px 16px;
    background:#ffffff;
    box-shadow:0 1px 4px rgba(0,0,0,.04);
}
div[data-testid="stMetric"] label{
    font-size:.82rem !important;
}
div[data-testid="stMetric"] [data-testid="stMetricValue"]{
    font-size:1.65rem !important;
}
.manager-card{
    border:1px solid #e5e7eb;
    border-radius:16px;
    padding:16px 18px;
    margin-bottom:10px;
    background:#fff;
    box-shadow:0 2px 8px rgba(0,0,0,.035);
}
.manager-red{
    border-left:6px solid #ef4444;
}
.manager-orange{
    border-left:6px solid #f59e0b;
}
.manager-green{
    border-left:6px solid #22c55e;
}
.manager-name{
    font-size:1.05rem;
    font-weight:700;
}
.manager-sub{
    color:#6b7280;
    font-size:.9rem;
    margin-top:2px;
}
.pill{
    display:inline-block;
    border-radius:999px;
    padding:3px 9px;
    font-size:.78rem;
    margin-right:5px;
    background:#f3f4f6;
}
.kpi-caption{
    color:#6b7280;
    font-size:.82rem;
}
.small-muted{
    color:#6b7280;
    font-size:.82rem;
}
hr{
    margin:.7rem 0 1rem 0 !important;
}
</style>
""", unsafe_allow_html=True)


def clean(v):
    return re.sub(r"\s+", " ", "" if v is None else str(v)).strip()


def norm(v):
    value = unicodedata.normalize("NFD", clean(v))
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return re.sub(r"[_\s]+", " ", value).strip().upper()


def col(df, names):
    mapping = {str(c).lower(): c for c in df.columns}
    for n in names:
        if n.lower() in mapping:
            return mapping[n.lower()]
    return None


def bucket_status(v):
    n = norm(v)
    if "PROSPECT A ATTRIBUER" in n:
        return "PROSPECT"
    if "A RELANCER" in n:
        return "RELANCE"
    return "AUTRE"


def fake_person(v):
    v = clean(v)
    n = norm(v)
    return (
        not v
        or n.startswith("NRP")
        or bool(re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", v))
        or bool(re.search(r"\d{1,2}:\d{2}", v))
        or len(v) > 80
    )


def derive_leads(raw):
    if raw.empty:
        return raw
    d = raw.copy()

    cp = col(d, ["intervenant","commercial","collaborateur","user"])
    ci = col(d, ["crm_id","lead_id","id"])
    cs = col(d, ["statut","status"])
    cn = col(d, ["nombre_nrp","nrp","nb_nrp"])
    cc = col(d, ["date_creation","created_at","cree_le"])
    cl = col(d, ["dernier_appel","last_call","last_attempt"])
    cnom = col(d, ["nom","name"])
    cpre = col(d, ["prenom","first_name"])
    csy = col(d, ["synced_at","updated_at"])

    d["_commercial"] = d[cp].fillna("").astype(str).map(clean) if cp else ""
    d["_commercial_norm"] = d["_commercial"].map(norm)
    d["_crm_id"] = d[ci].astype(str) if ci else d.index.astype(str)
    d["_status"] = d[cs].fillna("").astype(str).map(bucket_status) if cs else "AUTRE"
    d["_nrp"] = pd.to_numeric(d[cn], errors="coerce").fillna(0) if cn else 0
    d["_created"] = pd.to_datetime(d[cc], errors="coerce", dayfirst=True) if cc else pd.NaT
    d["_lead_last_call"] = pd.to_datetime(d[cl], errors="coerce", dayfirst=True) if cl else pd.NaT
    d["_synced"] = pd.to_datetime(d[csy], errors="coerce", dayfirst=True) if csy else pd.NaT

    if cnom and cpre:
        d["_prospect"] = (
            d[cnom].fillna("").astype(str).str.strip()
            + " "
            + d[cpre].fillna("").astype(str).str.strip()
        ).str.strip()
    elif cnom:
        d["_prospect"] = d[cnom].fillna("").astype(str)
    else:
        d["_prospect"] = d["_crm_id"]

    return d[
        ~d["_commercial"].map(fake_person)
        & ~d["_commercial_norm"].isin(EXCLUDED)
    ].copy()


def derive_calls(raw, leads):
    if raw.empty:
        return raw
    d = raw.copy()

    cp = col(d, ["commercial","intervenant","collaborateur","user"])
    ci = col(d, ["crm_id","lead_id","prospect_id"])
    cd = col(d, ["event_datetime","datetime","date_heure","appel_at"])

    d["_crm_id"] = d[ci].astype(str) if ci else d.index.astype(str)
    d["_datetime"] = pd.to_datetime(d[cd], errors="coerce", dayfirst=True) if cd else pd.NaT

    owner_map = (
        leads.drop_duplicates("_crm_id", keep="last")
        .set_index("_crm_id")["_commercial"].to_dict()
        if not leads.empty else {}
    )

    raw_people = d[cp].fillna("").astype(str).map(clean) if cp else pd.Series("", index=d.index)
    persons = []

    for idx, person in raw_people.items():
        if fake_person(person) or norm(person) in EXCLUDED:
            person = owner_map.get(str(d.at[idx, "_crm_id"]), "")
        persons.append(person)

    d["_commercial"] = persons
    d["_commercial_norm"] = d["_commercial"].map(norm)

    d = d[
        d["_datetime"].notna()
        & d["_commercial"].str.strip().ne("")
        & ~d["_commercial_norm"].isin(EXCLUDED)
    ].copy()

    if d.empty:
        return d

    mins = d["_datetime"].dt.hour*60 + d["_datetime"].dt.minute
    d["_strategic"] = ((mins >= 720) & (mins < 840)) | (mins >= 1110)

    return d


def derive_calendar(raw):
    if raw.empty:
        return raw
    d = raw.copy()

    cp = col(d, ["commercial","intervenant","collaborateur"])
    ct = col(d, ["title","details","name"])
    cd = col(d, ["event_date","date"])
    csy = col(d, ["synced_at","updated_at"])

    d["_commercial"] = d[cp].fillna("").astype(str).map(clean) if cp else ""
    d["_commercial_norm"] = d["_commercial"].map(norm)
    d["_title"] = d[ct].fillna("").astype(str) if ct else ""
    d["_date"] = pd.to_datetime(d[cd], errors="coerce", dayfirst=True) if cd else pd.NaT
    d["_synced"] = pd.to_datetime(d[csy], errors="coerce", dayfirst=True) if csy else pd.NaT

    return d[
        ~d["_commercial_norm"].isin(EXCLUDED)
        & d["_title"].str.contains(
            r"(?<![A-Za-z0-9])R\s*[12](?![A-Za-z0-9])",
            case=False, regex=True, na=False
        )
    ].copy()


try:
    raw_leads = read_table("leads")
    raw_calls = read_table("call_events")
    raw_calendar = read_table("calendar_events")
except Exception as exc:
    st.error("Connexion à Supabase impossible. Vérifiez DATABASE_URL dans les Secrets Streamlit.")
    st.exception(exc)
    st.stop()

leads = derive_leads(raw_leads)
calls = derive_calls(raw_calls, leads)
calendar = derive_calendar(raw_calendar)

# Dernier appel réel par CRM ID
if not calls.empty:
    last_call = calls.groupby("_crm_id")["_datetime"].max()
    leads = leads.join(last_call.rename("_last_call_events"), on="_crm_id")
else:
    leads["_last_call_events"] = pd.NaT

leads["_last_call"] = leads["_last_call_events"].combine_first(leads["_lead_last_call"])

now = pd.Timestamp.now()

with st.sidebar:
    st.header("Réglages")
    overdue_days = st.slider("Relance en retard après", 1, 10, 3, 1, format="%d jours")
    old_prospect_days = st.slider("Prospect ancien après", 1, 7, 2, 1, format="%d jours")
    st.divider()
    st.caption("Exclus de l'analyse : Anthony BOUVIER, Mehdi DIFALLAH")

prospects = leads[leads["_status"]=="PROSPECT"].copy()
relances = leads[leads["_status"]=="RELANCE"].copy()

if not prospects.empty:
    prospects["_age"] = (now-prospects["_created"]).dt.total_seconds()/86400
    prospects["_old"] = prospects["_created"].notna() & (prospects["_age"] >= old_prospect_days)
else:
    prospects["_old"] = False

if not relances.empty:
    relances["_history"] = relances["_last_call"].notna()
    relances["_days"] = (now-relances["_last_call"]).dt.total_seconds()/86400
    relances["_overdue"] = relances["_history"] & (relances["_days"] >= overdue_days)
    relances["_missing"] = ~relances["_history"]
else:
    relances["_history"] = False
    relances["_overdue"] = False
    relances["_missing"] = False

last7 = now - pd.Timedelta(days=7)
last30 = now - pd.Timedelta(days=30)

rows = []
for person in sorted(leads["_commercial"].dropna().unique()):
    pp = prospects[prospects["_commercial"]==person]
    rr = relances[relances["_commercial"]==person]
    cp = calls[calls["_commercial_norm"]==norm(person)] if not calls.empty else pd.DataFrame()
    kp = calendar[calendar["_commercial_norm"]==norm(person)] if not calendar.empty else pd.DataFrame()

    cp7 = cp[cp["_datetime"]>=last7] if not cp.empty else pd.DataFrame()

    overdue = int(rr["_overdue"].sum()) if not rr.empty else 0
    missing = int(rr["_missing"].sum()) if not rr.empty else 0
    oldp = int(pp["_old"].sum()) if not pp.empty else 0
    calls7 = len(cp7)
    strategic = int(cp7["_strategic"].sum()) if not cp7.empty else 0
    strategic_pct = round(strategic/max(calls7,1)*100,1)
    rdv30 = len(kp[kp["_date"]>=last30]) if not kp.empty else 0

    # Score direction plus lisible, avec pénalités plafonnées
    score = 100.0
    score -= min(30, overdue * 2.5)
    score -= min(12, missing * 2)
    score -= min(18, oldp / max(len(pp),1) * 18 if len(pp) else 0)

    if calls7 >= 10:
        if strategic_pct < 10:
            score -= 15
        elif strategic_pct < 20:
            score -= 8

    if rdv30 == 0 and calls7 >= 20:
        score -= 8

    score = max(0, round(score))

    if score < 55:
        level = "red"
        state = "🔴 Action"
    elif score < 75:
        level = "orange"
        state = "🟠 Vigilance"
    else:
        level = "green"
        state = "🟢 OK"

    actions = []
    if overdue:
        actions.append(f"{overdue} relance(s) en retard")
    if missing:
        actions.append(f"{missing} historique(s) manquant(s)")
    if oldp:
        actions.append(f"{oldp} prospect(s) ancien(s)")
    if calls7 >= 10 and strategic_pct < 20:
        actions.append("créneaux de phoning à renforcer")
    if not actions:
        actions = ["suivi maîtrisé"]

    rows.append({
        "Commercial":person,
        "Niveau":level,
        "État":state,
        "Score":score,
        "À attribuer":len(pp),
        "Prospects anciens":oldp,
        "À relancer":len(rr),
        "Retards":overdue,
        "Historique manquant":missing,
        "Appels 7j":calls7,
        "Phoning stratégique":strategic_pct,
        "RDV 30j":rdv30,
        "RDV 8 sem.":len(kp),
        "Action":" · ".join(actions[:2]),
    })

team = pd.DataFrame(rows)
if not team.empty:
    team = team.sort_values(["Score","Retards"], ascending=[True,False]).reset_index(drop=True)

# -----------------------------
# HEADER COMPACT
# -----------------------------
title_col, sync_col = st.columns([2.5,1])
with title_col:
    st.title("⚡ Cockpit Direction")
    st.caption("Les décisions du jour, sans bruit.")
with sync_col:
    crm_ts = leads["_synced"].dropna().max() if not leads.empty else None
    cal_ts = calendar["_synced"].dropna().max() if not calendar.empty else None
    st.markdown(
        f"<div class='small-muted'>CRM : <b>{pd.Timestamp(crm_ts).strftime('%d/%m %H:%M') if crm_ts is not None and not pd.isna(crm_ts) else '—'}</b>"
        f"<br>Calendrier : <b>{pd.Timestamp(cal_ts).strftime('%d/%m %H:%M') if cal_ts is not None and not pd.isna(cal_ts) else '—'}</b></div>",
        unsafe_allow_html=True
    )

# KPI
k1,k2,k3,k4,k5 = st.columns(5)
k1.metric("À attribuer", len(prospects))
k2.metric("À relancer", len(relances))
k3.metric("Retards réels", int(relances["_overdue"].sum()) if not relances.empty else 0)
k4.metric("Historiques manquants", int(relances["_missing"].sum()) if not relances.empty else 0)
k5.metric("RDV R1/R2", len(calendar))

st.markdown("<hr>", unsafe_allow_html=True)

# -----------------------------
# 1. AGENDA DU MANAGER
# -----------------------------
st.subheader("🚦 À traiter aujourd'hui")

attention = team[team["Niveau"]!="green"].head(5) if not team.empty else pd.DataFrame()

if attention.empty:
    st.success("Tout est sous contrôle aujourd'hui.")
else:
    for _,r in attention.iterrows():
        css = "manager-red" if r["Niveau"]=="red" else "manager-orange"
        st.markdown(
            f"""
            <div class="manager-card {css}">
              <div class="manager-name">{r['Commercial']} <span class="pill">{r['État']}</span></div>
              <div class="manager-sub">{r['Action']}</div>
              <div style="margin-top:10px;">
                <span class="pill">Score {int(r['Score'])}/100</span>
                <span class="pill">Retards {int(r['Retards'])}</span>
                <span class="pill">À attribuer {int(r['À attribuer'])}</span>
                <span class="pill">RDV 30j {int(r['RDV 30j'])}</span>
              </div>
            </div>
            """,
            unsafe_allow_html=True
        )

# -----------------------------
# 2. ÉQUIPE EN CARTES
# -----------------------------
st.subheader("👥 Équipe")

cols = st.columns(4)
for i,(_,r) in enumerate(team.iterrows()):
    css = {"red":"manager-red","orange":"manager-orange","green":"manager-green"}[r["Niveau"]]
    with cols[i%4]:
        st.markdown(
            f"""
            <div class="manager-card {css}">
              <div class="manager-name">{r['Commercial']}</div>
              <div class="manager-sub">{r['État']} · Score {int(r['Score'])}/100</div>
              <div style="margin-top:9px">
                <span class="pill">Retards {int(r['Retards'])}</span>
                <span class="pill">RDV {int(r['RDV 30j'])}</span>
              </div>
              <div style="margin-top:8px" class="manager-sub">
                {int(r['Appels 7j'])} appels / 7j · {r['Phoning stratégique']:.0f}% horaires stratégiques
              </div>
            </div>
            """,
            unsafe_allow_html=True
        )

st.markdown("<hr>", unsafe_allow_html=True)

# -----------------------------
# 3. ANALYSE INDIVIDUELLE
# -----------------------------
st.subheader("🔎 Focus commercial")

left, right = st.columns([1,2.2])

with left:
    person = st.selectbox(
        "Choisir",
        team["Commercial"].tolist(),
        label_visibility="collapsed"
    )
    r = team[team["Commercial"]==person].iloc[0]

    st.markdown(f"### {r['Commercial']}")
    st.markdown(f"**{r['État']} — {int(r['Score'])}/100**")
    st.write(r["Action"])

with right:
    a,b,c,d = st.columns(4)
    a.metric("À attribuer", int(r["À attribuer"]))
    b.metric("À relancer", int(r["À relancer"]))
    c.metric("Retards", int(r["Retards"]))
    d.metric("RDV 30j", int(r["RDV 30j"]))

    e,f,g,h = st.columns(4)
    e.metric("Appels 7j", int(r["Appels 7j"]))
    f.metric("Phoning stratégique", f"{r['Phoning stratégique']:.0f}%")
    g.metric("Prospects anciens", int(r["Prospects anciens"]))
    h.metric("Hist. manquants", int(r["Historique manquant"]))

# Liste actionnable des relances
person_rr = relances[
    (relances["_commercial"]==person)
    & relances["_overdue"]
].copy()

if not person_rr.empty:
    st.markdown("#### Relances prioritaires")
    view = person_rr[
        ["_prospect","_nrp","_last_call","_days"]
    ].copy()
    view["_days"] = view["_days"].round(0).astype("Int64")
    view = view.sort_values("_days", ascending=False)
    view.columns = ["Prospect","NRP","Dernier appel","Jours sans appel"]

    st.dataframe(
        view.head(25),
        use_container_width=True,
        hide_index=True,
        height=min(36*len(view.head(25))+40, 420),
    )
else:
    st.success("Aucune relance réellement en retard pour ce commercial.")

# -----------------------------
# 4. TABLEAU COMPLET EN OPTION
# -----------------------------
with st.expander("Voir le tableau complet de l'équipe"):
    st.dataframe(
        team[
            [
                "État","Commercial","Score","À attribuer","Prospects anciens",
                "À relancer","Retards","Historique manquant","Appels 7j",
                "Phoning stratégique","RDV 30j","Action"
            ]
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Score": st.column_config.ProgressColumn(
                "Score", min_value=0, max_value=100, format="%d"
            ),
            "Phoning stratégique": st.column_config.ProgressColumn(
                "Phoning stratégique", min_value=0, max_value=100, format="%.0f%%"
            ),
        }
    )

with st.expander("Qualité des données"):
    st.write(f"Leads analysés : **{len(leads)}**")
    st.write(f"Événements d'appel exploitables : **{len(calls)}**")
    st.write(f"RDV R1/R2 analysés : **{len(calendar)}**")
    st.write(
        f"Relances sans historique exploitable : "
        f"**{int(relances['_missing'].sum()) if not relances.empty else 0}**"
    )
