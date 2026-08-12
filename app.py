from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from database import load_leads, upsert
from utils import clean_dataframe, read_uploaded_file

BASE_DIR = Path(__file__).resolve().parent

st.set_page_config(
    page_title="Free Énergie — Lead Tracker V2",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
.block-container {padding-top: 1rem; padding-bottom: 3rem;}
div[data-testid="stMetric"] {
  background:white; border:1px solid #ececec; border-radius:18px;
  padding:16px; box-shadow:0 8px 28px rgba(0,0,0,.05);
}
.stTabs [data-baseweb="tab-list"] {gap: 1.1rem;}
</style>
""",
    unsafe_allow_html=True,
)

st.title("⚡ Free Énergie — Lead Tracker V2")
st.caption("Synchronisation CRM, priorités, alertes et analyse des NRP.")

with st.sidebar:
    st.header("Données cloud")
    st.success("Connecté à Supabase")
    st.caption("Utilisez l’import CSV/Excel ci-dessous pour actualiser Supabase. L’automatisation CRM sera raccordée séparément.")

    st.divider()
    st.header("Actualisation")
    auto_refresh = st.toggle("Rafraîchir le dashboard", value=False)
    refresh_minutes = st.slider("Toutes les X minutes", 1, 60, 10)
    if auto_refresh:
        st_autorefresh(interval=refresh_minutes * 60_000, key="autorefresh")

    st.divider()
    st.header("Import")
    uploaded = st.file_uploader("CSV ou Excel", type=["csv", "xlsx", "xls"])
    if uploaded:
        try:
            imported = read_uploaded_file(uploaded)
            st.success(f"{len(imported)} ligne(s) reconnue(s)")
            if st.button("Importer", use_container_width=True):
                upsert(imported)
                st.rerun()
        except Exception as exc:
            st.error(str(exc))

df = clean_dataframe(load_leads())
if df.empty:
    st.warning("Aucune donnée. Lance une synchronisation CRM.")
    st.stop()

for col in ("date_creation", "date_statut", "dernier_appel"):
    if col not in df:
        df[col] = pd.NaT
    df[col] = pd.to_datetime(df[col], errors="coerce", dayfirst=True)

for col in ("nombre_nrp",):
    if col not in df:
        df[col] = 0
    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

now = pd.Timestamp.now()
df["heures_depuis_appel"] = (now - df["dernier_appel"]).dt.total_seconds() / 3600
df["score_priorite"] = (
    df["nombre_nrp"] * 12
    + df["heures_depuis_appel"].fillna(0).clip(0, 168) / 4
    + df["dernier_appel"].isna().astype(int) * 60
)

if "statut" not in df:
    df["statut"] = ""
df.loc[df["statut"].eq("A RELANCER"), "score_priorite"] += 35
df.loc[df["statut"].eq("PROSPECT A ATTRIBUER"), "score_priorite"] += 20

df["niveau_priorite"] = pd.cut(
    df["score_priorite"],
    [-1, 35, 70, 120, float("inf")],
    labels=["Faible", "Moyenne", "Haute", "Critique"],
)

with st.expander("Filtres", expanded=True):
    c1, c2, c3, c4 = st.columns(4)
    valid_dates = df["date_creation"].dropna()
    today = pd.Timestamp.today().date()
    min_date = valid_dates.min().date() if not valid_dates.empty else today
    max_date = valid_dates.max().date() if not valid_dates.empty else today
    date_from = c1.date_input("Du", min_date)
    date_to = c2.date_input("Au", max_date)

    sources = sorted(df.get("source", pd.Series(dtype=str)).dropna().astype(str).unique())
    owners = sorted(df.get("intervenant", pd.Series(dtype=str)).dropna().astype(str).unique())
    source_filter = c3.multiselect("Sources", sources)
    owner_filter = c4.multiselect("Intervenants", owners)

    c5, c6, c7, c8 = st.columns(4)
    statuses = sorted(df["statut"].dropna().astype(str).unique())
    status_filter = c5.multiselect("Statuts", statuses, default=[])
    nrp_min = c6.number_input("NRP minimum", min_value=0, value=0)
    priority_filter = c7.multiselect(
        "Priorités", ["Faible", "Moyenne", "Haute", "Critique"],
        default=["Faible", "Moyenne", "Haute", "Critique"],
    )
    urgent_hours = c8.number_input("Urgent après (h)", min_value=1, value=24)

filtered = df.copy()
if filtered["date_creation"].notna().any():
    filtered = filtered[
        filtered["date_creation"].dt.date.between(date_from, date_to, inclusive="both")
    ]
if source_filter:
    filtered = filtered[filtered["source"].isin(source_filter)]
if owner_filter:
    filtered = filtered[filtered["intervenant"].isin(owner_filter)]
if status_filter:
    filtered = filtered[filtered["statut"].isin(status_filter)]
filtered = filtered[filtered["nombre_nrp"] >= nrp_min]
filtered = filtered[filtered["niveau_priorite"].astype(str).isin(priority_filter)]

urgent_mask = (
    filtered["dernier_appel"].isna()
    | filtered["heures_depuis_appel"].ge(urgent_hours)
    | filtered["niveau_priorite"].astype(str).isin(["Haute", "Critique"])
)

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Leads", len(filtered))
m2.metric("NRP cumulés", int(filtered["nombre_nrp"].sum()))
m3.metric("NRP moyen", f"{filtered['nombre_nrp'].mean() if len(filtered) else 0:.2f}")
m4.metric("Relances urgentes", int(urgent_mask.sum()))
last_sync = pd.to_datetime(df.get("synced_at"), errors="coerce").max() if "synced_at" in df else pd.NaT
m5.metric("Dernière synchro", last_sync.strftime("%d/%m %H:%M") if pd.notna(last_sync) else "—")

priority = filtered.sort_values(
    ["score_priorite", "nombre_nrp", "date_creation"],
    ascending=[False, False, True],
)

tab_priority, tab_overview, tab_time, tab_team, tab_alerts, tab_export = st.tabs(
    ["Priorités", "Vue générale", "Temps", "Équipe", "Alertes", "Export"]
)

with tab_priority:
    st.subheader("Qui appeler en premier")
    if priority.empty:
        st.info("Aucun prospect ne correspond aux filtres.")
    else:
        top = priority.iloc[0]
        display_name = " ".join(
            str(top.get(x) or "") for x in ("prenom", "nom")
        ).strip() or "Prospect sans nom"
        st.info(
            f"**{display_name}** — {int(top['nombre_nrp'])} NRP — "
            f"priorité {top['niveau_priorite']}"
        )
        phone = str(top.get("telephone") or "").strip()
        if phone:
            st.link_button("📞 Appeler le prochain", f"tel:{phone}", use_container_width=True)

    cols = [
        "niveau_priorite", "score_priorite", "date_creation", "nom", "prenom",
        "ville", "statut", "source", "intervenant", "nombre_nrp",
        "dernier_appel", "heures_depuis_appel", "telephone",
    ]
    cols = [c for c in cols if c in priority]
    st.dataframe(
        priority[cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "score_priorite": st.column_config.ProgressColumn(
                "Score urgence",
                min_value=0,
                max_value=max(150.0, float(priority["score_priorite"].max()) if len(priority) else 150.0),
            ),
            "heures_depuis_appel": st.column_config.NumberColumn("Heures depuis appel", format="%.1f"),
        },
    )

with tab_overview:
    c1, c2 = st.columns(2)
    if "source" in filtered:
        by_source = filtered.groupby("source", dropna=False).size().reset_index(name="leads")
        c1.plotly_chart(px.bar(by_source, x="source", y="leads", title="Leads par source"), use_container_width=True)
    by_status = filtered.groupby("statut", dropna=False).size().reset_index(name="leads")
    c2.plotly_chart(px.pie(by_status, names="statut", values="leads", title="Statuts"), use_container_width=True)

    dated = filtered.dropna(subset=["date_creation"]).copy()
    if not dated.empty:
        dated["jour"] = dated["date_creation"].dt.floor("D")
        timeline = dated.groupby("jour", as_index=False).agg(
            leads=("crm_id", "count"),
            nrp=("nombre_nrp", "sum"),
        )
        st.plotly_chart(
            px.line(timeline, x="jour", y=["leads", "nrp"], markers=True, title="Évolution quotidienne"),
            use_container_width=True,
        )

with tab_time:
    calls = filtered.dropna(subset=["dernier_appel"]).copy()
    if calls.empty:
        st.info("Aucune date de dernière tentative disponible.")
    else:
        c1, c2 = st.columns(2)
        calls["heure"] = calls["dernier_appel"].dt.hour
        hourly = calls.groupby("heure", as_index=False).size().rename(columns={"size": "appels"})
        c1.plotly_chart(px.bar(hourly, x="heure", y="appels", title="Tentatives par heure"), use_container_width=True)
        c2.plotly_chart(px.histogram(calls, x="heures_depuis_appel", nbins=20, title="Temps depuis le dernier appel"), use_container_width=True)

with tab_team:
    if "intervenant" in filtered:
        team = filtered.groupby("intervenant", dropna=False).agg(
            leads=("crm_id", "count"),
            nrp=("nombre_nrp", "sum"),
            score_moyen=("score_priorite", "mean"),
        ).reset_index()
        st.dataframe(team, use_container_width=True, hide_index=True)
        st.plotly_chart(px.bar(team, x="intervenant", y=["leads", "nrp"], barmode="group"), use_container_width=True)

with tab_alerts:
    alerts = filtered[urgent_mask].sort_values("score_priorite", ascending=False)
    st.subheader(f"{len(alerts)} alerte(s) active(s)")
    alert_cols = [
        "niveau_priorite", "nom", "prenom", "statut", "source", "intervenant",
        "nombre_nrp", "dernier_appel", "heures_depuis_appel", "telephone",
    ]
    st.dataframe(alerts[[c for c in alert_cols if c in alerts]], use_container_width=True, hide_index=True)
    st.caption("Les notifications macOS sont produites par notifier.py en arrière-plan.")

with tab_export:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        filtered.to_excel(writer, index=False, sheet_name="Leads")
        priority.to_excel(writer, index=False, sheet_name="Priorites")
        filtered[urgent_mask].to_excel(writer, index=False, sheet_name="Alertes")
    st.download_button(
        "📥 Télécharger le rapport Excel",
        buffer.getvalue(),
        file_name=f"freeenergie_v2_{datetime.now():%Y%m%d_%H%M}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
