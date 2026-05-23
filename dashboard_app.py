"""
Devotio Stripe Dashboard — Interfaz web local
Uso: streamlit run dashboard_app.py

Datos se acumulan en data/devotio_stripe_MASTER.csv
El pull siempre cubre desde el último pull hasta ahora (sin gaps).
"""

import io
import json
import os
import sys
import shutil
import tempfile
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import streamlit as st
import pandas as pd

try:
    import gspread
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

# ── Rutas ─────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).parent
DATA_DIR    = PROJECT_DIR / "data"
MASTER_CSV  = DATA_DIR / "devotio_stripe_MASTER.csv"
PULL_LOG    = DATA_DIR / "pull_log.json"
UTC         = timezone.utc


# ── Store helpers ─────────────────────────────────────────────────────────────

def find_onedrive_root():
    cloud = Path.home() / "Library/CloudStorage"
    cloud_matches = list(cloud.glob("OneDrive*")) if cloud.exists() else []
    candidates = [
        Path.home() / "OneDrive - Devotio",
        Path.home() / "OneDrive - Devotio Rewards",
        Path.home() / "Library/CloudStorage/OneDriveCommercial",
        Path.home() / "OneDrive",
        Path(os.environ.get("OneDrive", "")),
        Path(os.environ.get("OneDriveCommercial", "")),
    ] + cloud_matches
    for p in candidates:
        try:
            sp = str(p)
            if sp and sp not in (".", "") and Path(p).exists() and Path(p).is_dir():
                return sp
        except Exception:
            pass
    return None


def read_pull_log() -> dict | None:
    if not PULL_LOG.exists():
        return None
    try:
        with open(PULL_LOG) as f:
            return json.load(f)
    except Exception:
        return None


def write_pull_log(start_dt: datetime, end_dt: datetime, records_added: int, total: int):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PULL_LOG, "w") as f:
        json.dump({
            "last_pull_start_utc":  start_dt.isoformat(),
            "last_pull_end_utc":    end_dt.isoformat(),
            "last_pull_at":         datetime.now(UTC).isoformat(),
            "records_added":        records_added,
            "total_master_records": total,
        }, f, indent=2)


def _clean(val) -> str:
    """Convert a potentially NaN/None cell to a clean string (pandas NaN is truthy)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).strip()
    return "" if s == "nan" else s


def _row_key(row) -> str:
    inv = _clean(row.get("invoice_id"))
    if inv:
        return f"inv_{inv}"
    cid  = _clean(row.get("charge_id"))
    tipo = _clean(row.get("tipo"))
    if cid and tipo != "refund":
        return f"charge_{cid}"
    if tipo == "refund" and cid:
        return f"refund_{cid}_{row.get('fecha_utc','')}_{row.get('monto_usd','')}"
    sid = _clean(row.get("subscription_id"))
    if tipo == "subscription_deleted" and sid:
        return f"sub_del_{sid}_{row.get('fecha_utc','')}"
    return f"raw_{tipo}_{row.get('fecha_utc','')}_{row.get('monto_usd','0')}_{row.get('cliente_email','')}"


def merge_into_master(new_csv_path: str) -> tuple[int, int]:
    """Merge new pull CSV into MASTER. Returns (rows_added, total_rows)."""
    new_df = pd.read_csv(new_csv_path)
    if MASTER_CSV.exists():
        master_df = pd.read_csv(MASTER_CSV)
        before    = len(master_df)
        combined  = pd.concat([master_df, new_df], ignore_index=True)
    else:
        before   = 0
        combined = new_df.copy()

    combined["_key"] = combined.apply(_row_key, axis=1)
    deduped = (combined
               .drop_duplicates(subset="_key", keep="first")
               .drop(columns="_key")
               .sort_values("fecha_utc")
               .reset_index(drop=True))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    deduped.to_csv(MASTER_CSV, index=False)

    added = len(deduped) - before
    return max(added, 0), len(deduped)


def find_creds_file() -> str | None:
    """Auto-detect the service account JSON in the project directory."""
    for p in PROJECT_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text())
            if data.get("type") == "service_account":
                return str(p)
        except Exception:
            pass
    return None


def _get_sheets_id() -> str:
    return st.secrets.get("GOOGLE_SHEET_ID", "1ZAb6lChJT6aBdnsIpJ--HrqtmVUnFzE9K6bgkggfJxE")


def _get_gspread_client():
    """Returns gspread client from local JSON file or Streamlit Cloud secrets."""
    if not GSPREAD_AVAILABLE:
        return None
    # Local dev: JSON file in project folder
    creds_file = find_creds_file()
    if creds_file:
        return gspread.service_account(filename=creds_file)
    # Streamlit Cloud: credentials stored as secret dict [GOOGLE_CREDENTIALS]
    if "GOOGLE_CREDENTIALS" in st.secrets:
        return gspread.service_account_from_dict(dict(st.secrets["GOOGLE_CREDENTIALS"]))
    return None


def restore_from_sheets():
    """On cold start (no local files), restore MASTER.csv and pull_log from Sheets."""
    if MASTER_CSV.exists() and PULL_LOG.exists():
        return
    gc = _get_gspread_client()
    if gc is None:
        return
    try:
        sh = gc.open_by_key(_get_sheets_id())
        # Restore MASTER data
        if not MASTER_CSV.exists():
            ws   = sh.sheet1
            rows = ws.get_all_values()
            if len(rows) > 1:
                df = pd.DataFrame(rows[1:], columns=rows[0])
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                df.to_csv(MASTER_CSV, index=False)
        # Restore pull_log from "config" tab
        if not PULL_LOG.exists():
            try:
                cfg = sh.worksheet("config")
                val = cfg.acell("A1").value
                if val:
                    DATA_DIR.mkdir(parents=True, exist_ok=True)
                    PULL_LOG.write_text(val)
            except Exception:
                pass
    except Exception:
        pass


def sync_to_sheets() -> tuple[bool, str]:
    """Overwrite Google Sheet with current MASTER.csv and persist pull_log."""
    if not MASTER_CSV.exists():
        return False, "MASTER.csv no encontrado"
    gc = _get_gspread_client()
    if gc is None:
        return False, "Sin credenciales de Google Sheets"
    try:
        df = pd.read_csv(MASTER_CSV).fillna("")
        sh = gc.open_by_key(_get_sheets_id())
        # Sync data
        ws = sh.sheet1
        ws.clear()
        ws.update([df.columns.tolist()] + df.values.tolist())
        # Sync pull_log to "config" tab
        if PULL_LOG.exists():
            try:
                try:
                    cfg = sh.worksheet("config")
                except Exception:
                    cfg = sh.add_worksheet("config", rows=2, cols=1)
                cfg.update("A1", [[PULL_LOG.read_text()]])
            except Exception:
                pass
        return True, f"{len(df):,} filas sincronizadas → {sh.title}"
    except Exception as e:
        return False, str(e)


def load_master(start_d: date | None = None, end_d: date | None = None) -> pd.DataFrame:
    if not MASTER_CSV.exists():
        return pd.DataFrame()
    df = pd.read_csv(MASTER_CSV)
    if df.empty or start_d is None or end_d is None:
        return df
    df["_date"] = pd.to_datetime(df["fecha_utc"], errors="coerce").dt.date
    df = df[(df["_date"] >= start_d) & (df["_date"] <= end_d)].copy()
    df.drop(columns="_date", inplace=True, errors="ignore")
    return df.reset_index(drop=True)


# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Devotio · Stripe Reports",
    page_icon="💳",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
[data-testid="stSidebar"] { background: #1A1A2E; }
[data-testid="stSidebar"] * { color: #E0E6F0 !important; }
[data-testid="stSidebar"] .stSelectbox label,
[data-testid="stSidebar"] .stTextInput label { color: #A0B0CC !important; font-size: 12px; }
.metric-card { background: #F7F9FF; border-radius: 10px; padding: 18px 20px;
               border: 1px solid #DDE6F7; text-align: center; }
.metric-value { font-size: 28px; font-weight: 700; color: #0F3A75; }
.metric-label { font-size: 12px; color: #6B7A99; margin-top: 4px; }
.metric-value.green  { color: #1A7A40; }
.metric-value.red    { color: #C0392B; }
.metric-value.orange { color: #D36800; }
h1 { color: #FFFFFF !important; }
h2 { color: #0F3A75 !important; }
h3 { color: #162136 !important; }
.page-header { background: linear-gradient(135deg, #1A1A2E 0%, #0F3A75 100%);
               padding: 24px 32px; border-radius: 12px; margin-bottom: 8px; }
.page-header h1 { color: #FFFFFF !important; margin: 0; font-size: 1.8rem; }
.page-header p  { color: #A0C0E8 !important; margin: 4px 0 0; font-size: 0.85rem; }
.stDownloadButton button { background: #0F3A75 !important; color: white !important;
                           border-radius: 8px !important; font-weight: 600 !important; }
.stButton button { border-radius: 8px !important; font-weight: 600 !important; }
.pull-status { background: #0F3A7518; border-left: 3px solid #0F3A75;
               padding: 10px 14px; border-radius: 0 8px 8px 0; margin-bottom: 8px; }
</style>
""", unsafe_allow_html=True)


# ── On cold start: restore MASTER + pull_log from Google Sheets ──────────────
restore_from_sheets()

# ── API key — from Streamlit secrets (never entered manually) ────────────────
api_key = st.secrets.get("STRIPE_SECRET_KEY", os.environ.get("STRIPE_SECRET_KEY", ""))

# ── Read pull log (once per page load) ───────────────────────────────────────
pull_log = read_pull_log()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 💳 Devotio Stripe")
    if api_key:
        st.success("✓ Conectado a Stripe", icon="🔐")
    else:
        st.error("API Key no configurada")
    st.markdown("---")

    # ── Sección 1: Pull automático ────────────────────────────────────────
    st.markdown("**📡 Actualizar Datos**")

    if pull_log:
        last_end_dt   = datetime.fromisoformat(pull_log["last_pull_end_utc"])
        last_at       = datetime.fromisoformat(pull_log["last_pull_at"])
        # Pull from last_end - 2h overlap to now. Dedup handles any duplicates.
        pull_start_dt = last_end_dt - timedelta(hours=2)
        pull_end_dt   = datetime.now(UTC)

        total_rec = pull_log.get("total_master_records", 0)
        st.markdown(
            f"<div class='pull-status'>"
            f"<b>Última actualización</b><br>"
            f"{last_at.strftime('%d %b %Y · %H:%M')} UTC<br>"
            f"<small>{total_rec:,} registros en historial</small>"
            f"</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            f"Próximo pull: {pull_start_dt.strftime('%d %b %H:%M')} → ahora"
        )
    else:
        st.warning("Primera vez — elige la fecha de inicio del historial")
        initial_start = st.date_input(
            "Registrar desde",
            value=date.today() - timedelta(days=30),
            help="Todos los pagos desde esta fecha se agregarán al historial permanente."
        )
        pull_start_dt = datetime(
            initial_start.year, initial_start.month, initial_start.day, tzinfo=UTC
        )
        pull_end_dt = datetime.now(UTC)

    generate = st.button(
        "🚀 Actualizar Datos",
        use_container_width=True,
        type="primary",
        disabled=not bool(api_key),
    )

    st.markdown("---")

    # ── Sección 2: Filtro visual del dashboard ────────────────────────────
    st.markdown("**📅 Filtrar Dashboard**")
    st.caption("Solo cambia qué ves — no afecta el historial guardado.")

    preset = st.selectbox(
        "Período a mostrar",
        options=[
            "Última semana",
            "Últimas 2 semanas",
            "Último mes",
            "Mes anterior completo",
            "Todo el historial",
            "Rango personalizado",
        ],
        index=2,
    )

    today = date.today()
    if preset == "Última semana":
        last_mon   = today - timedelta(days=today.weekday() + 7)
        view_start = last_mon
        view_end   = last_mon + timedelta(days=6)
    elif preset == "Últimas 2 semanas":
        view_start = today - timedelta(days=14)
        view_end   = today - timedelta(days=1)
    elif preset == "Último mes":
        view_start = today - timedelta(days=30)
        view_end   = today
    elif preset == "Mes anterior completo":
        first_this = today.replace(day=1)
        view_end   = first_this - timedelta(days=1)
        view_start = view_end.replace(day=1)
    elif preset == "Todo el historial":
        view_start = None
        view_end   = None
    else:
        view_start = st.date_input("Fecha inicio", value=today - timedelta(days=7))
        view_end   = st.date_input("Fecha fin",    value=today)

    if view_start and view_end:
        st.caption(f"{view_start.strftime('%d/%m/%Y')} → {view_end.strftime('%d/%m/%Y')}")

    # Month quick-filter (populated from available months in MASTER)
    if MASTER_CSV.exists():
        _all = pd.read_csv(MASTER_CSV, usecols=["fecha_utc"])
        _months = (
            pd.to_datetime(_all["fecha_utc"], errors="coerce")
            .dt.to_period("M")
            .dropna()
            .unique()
        )
        _month_opts = sorted([str(m) for m in _months])
        if len(_month_opts) > 1:
            st.markdown("**🗓 Filtro por mes**")
            selected_months = st.multiselect(
                "Seleccionar mes(es)",
                options=_month_opts,
                default=_month_opts,
                help="Filtra el dashboard por mes específico (independiente del período arriba).",
            )
        else:
            selected_months = _month_opts
    else:
        selected_months = []

    st.markdown("---")

    # ── Sección 3: Descargar (placeholder — se llena después de cargar data) ──
    st.markdown("**⬇ Descargar datos**")
    sidebar_dl_placeholder = st.empty()

    st.markdown("---")
    st.markdown(
        "<small style='color:#6080A0'>Blue Phoenix Lab · Devotio Rewards<br>"
        "stripe_pull.py v1.0</small>",
        unsafe_allow_html=True,
    )

# (Google Sheets sync is always-on via sync_to_sheets() — no sidebar controls needed)


# ── Main area ─────────────────────────────────────────────────────────────────
st.markdown("""
<div class='page-header'>
  <h1>💳 Devotio — Reporte Stripe</h1>
  <p>Historial acumulativo · 100% de cobertura · Sin gaps entre pulls</p>
</div>
""", unsafe_allow_html=True)

# ── Run pull if requested ─────────────────────────────────────────────────────
if generate:
    st.markdown("---")
    st.markdown(
        f"### 📡 Actualizando: "
        f"{pull_start_dt.strftime('%d %b %Y %H:%M')} UTC → ahora"
    )

    progress_bar = st.progress(0, text="Iniciando conexión con Stripe…")
    status_box   = st.empty()
    captured     = io.StringIO()

    try:
        sys.path.insert(0, str(PROJECT_DIR))
        import stripe_pull

        old_stdout = sys.stdout
        sys.stdout = captured

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = tmp.name

        progress_bar.progress(10, text="[1/4] Consultando facturas de suscripción…")
        stripe_pull.pull(api_key, pull_start_dt, pull_end_dt, tmp_path)

        sys.stdout = old_stdout
        progress_bar.progress(80, text="Actualizando historial…")

        added, total = merge_into_master(tmp_path)

        # Sync to Google Sheets (always on)
        progress_bar.progress(90, text="Sincronizando con Google Sheets…")
        sheets_synced = sync_to_sheets()

        write_pull_log(pull_start_dt, pull_end_dt, added, total)
        os.unlink(tmp_path)

        progress_bar.progress(100, text="✅ Historial actualizado")

        col_a, col_b = st.columns(2)
        col_a.success(f"✅ **{added}** registros nuevos agregados")
        col_b.info(f"📦 **{total:,}** registros en historial total")

        ok, msg = sheets_synced
        if ok:
            st.success(f"📊 Google Sheets actualizado: {msg}")
        else:
            st.warning(f"📊 Google Sheets: {msg}")

        log_text = captured.getvalue()
        if log_text.strip():
            with st.expander("🔧 Log de ejecución"):
                st.code(log_text)

        st.markdown("---")

    except Exception as e:
        sys.stdout = old_stdout if "old_stdout" in dir() else sys.stdout
        progress_bar.empty()
        st.error(f"❌ Error al conectar con Stripe: {e}")
        st.stop()

    # Reload pull_log after update
    pull_log = read_pull_log()


# ── Dashboard ─────────────────────────────────────────────────────────────────
master_exists = MASTER_CSV.exists()

if not master_exists:
    st.markdown("---")
    c1, c2, c3 = st.columns(3)
    c1.markdown("""<div class='metric-card'>
        <div class='metric-value green'>100%</div>
        <div class='metric-label'>Cobertura garantizada</div></div>""",
        unsafe_allow_html=True)
    c2.markdown("""<div class='metric-card'>
        <div class='metric-value'>Sin gaps</div>
        <div class='metric-label'>Pull continúa donde quedó</div></div>""",
        unsafe_allow_html=True)
    c3.markdown("""<div class='metric-card'>
        <div class='metric-value'>MASTER.csv</div>
        <div class='metric-label'>Historial acumulativo permanente</div></div>""",
        unsafe_allow_html=True)

    st.markdown("---")
    st.info(
        "👈 **Primera vez.** Selecciona la fecha de inicio del historial en el panel izquierdo "
        "e ingresa tu API Key para iniciar el pull."
    )
    with st.expander("ℹ ¿Cómo funciona el historial acumulativo?"):
        st.markdown("""
        1. **Primer pull** — seleccionas desde qué fecha quieres el historial completo
        2. **Pulls siguientes** — el sistema detecta automáticamente el último pull y continúa desde ahí
        3. **Sin gaps** — siempre cubre el rango completo desde la última vez hasta ahora
        4. **Sin duplicados** — cada transacción se guarda exactamente una vez, aunque el rango se traslape
        5. **MASTER.csv** — en `data/devotio_stripe_MASTER.csv` — crece con cada pull, nunca se sobrescribe
        """)
    st.stop()

# ── Load data ─────────────────────────────────────────────────────────────────
df = load_master(view_start, view_end)

if df.empty:
    st.markdown("---")
    total_in_master = len(pd.read_csv(MASTER_CSV)) if MASTER_CSV.exists() else 0
    st.warning(
        f"No hay registros en el rango seleccionado. "
        f"El historial completo tiene **{total_in_master:,}** registros — "
        f"cambia el filtro de período en el panel izquierdo."
    )
    st.stop()

# Apply month filter (if user de-selected some months)
if selected_months:
    df["_month"] = pd.to_datetime(df["fecha_utc"], errors="coerce").dt.to_period("M").astype(str)
    df = df[df["_month"].isin(selected_months)].drop(columns="_month").reset_index(drop=True)

if df.empty:
    st.warning("No hay registros para los meses seleccionados. Ajusta el filtro de mes.")
    st.stop()

# ── Fill sidebar download placeholder (df is ready here) ─────────────────────
fname_dl = (
    f"devotio_stripe_{view_start.strftime('%Y%m%d')}_{view_end.strftime('%Y%m%d')}.csv"
    if view_start and view_end else "devotio_stripe_MASTER.csv"
)
with sidebar_dl_placeholder.container():
    st.download_button(
        label="⬇ Vista actual (filtrada)",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=fname_dl,
        mime="text/csv",
        use_container_width=True,
    )
    if MASTER_CSV.exists():
        st.download_button(
            label=f"⬇ Historial completo",
            data=MASTER_CSV.read_bytes(),
            file_name="devotio_stripe_MASTER.csv",
            mime="text/csv",
            use_container_width=True,
        )

# ── Period label ──────────────────────────────────────────────────────────────
if view_start and view_end:
    period_label = f"{view_start.strftime('%d %b %Y')} → {view_end.strftime('%d %b %Y')}"
else:
    period_label = "Historial completo"

st.markdown(
    f"<p style='color:#A0C0E8;font-size:0.9rem;margin:0 0 4px;'>📊 Período visualizado</p>"
    f"<h3 style='color:#FFFFFF;margin:0 0 12px;'>{period_label}</h3>",
    unsafe_allow_html=True,
)
st.markdown("---")

# ── Summary metrics ───────────────────────────────────────────────────────────
paid     = df[df["estado"] == "paid"]
failed   = df[df["estado"] == "failed"]
canceled = df[df["tipo"]   == "subscription_deleted"]
refunds  = df[df["tipo"]   == "refund"]

total_ref_amt = refunds["monto_usd"].astype(float).sum() if not refunds.empty else 0.0

pct_exito = round(len(paid) / len(df) * 100) if len(df) > 0 else 0

c1, c2, c3, c4 = st.columns(4)
c1.markdown(f"""<div class='metric-card'>
    <div class='metric-value green'>{len(paid)}</div>
    <div class='metric-label'>Pagos exitosos</div></div>""",
    unsafe_allow_html=True)
c2.markdown(f"""<div class='metric-card'>
    <div class='metric-value red'>{len(failed)}</div>
    <div class='metric-label'>Intentos fallidos</div></div>""",
    unsafe_allow_html=True)
c3.markdown(f"""<div class='metric-card'>
    <div class='metric-value orange'>{len(canceled)}</div>
    <div class='metric-label'>Cancelaciones</div></div>""",
    unsafe_allow_html=True)
c4.markdown(f"""<div class='metric-card'>
    <div class='metric-value'>{pct_exito}%</div>
    <div class='metric-label'>Tasa de cobro exitoso</div></div>""",
    unsafe_allow_html=True)

if total_ref_amt > 0:
    st.caption(f"Reembolsos en el período: {len(refunds)} registros")

st.markdown("<br>", unsafe_allow_html=True)
st.markdown("---")

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs([
    "✅ Pagos exitosos",
    "❌ Pagos fallidos",
    "⚠ Clientes en riesgo",
    "🔍 Todos los datos",
])

with tab1:
    st.markdown("#### Pagos Exitosos")
    if not paid.empty:
        by_origin = (
            paid.groupby("origen")
            .agg(transacciones=("invoice_id", "count"))
            .reset_index()
            .sort_values("transacciones", ascending=False)
        )
        st.dataframe(by_origin, use_container_width=True, hide_index=True)
        st.markdown("---")
        cols = ["fecha_utc", "cliente_email", "origen", "invoice_id", "subscription_id"]
        existing_cols = [c for c in cols if c in paid.columns]
        st.dataframe(
            paid[existing_cols].sort_values("fecha_utc", ascending=False),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No hay pagos exitosos en este período.")

with tab2:
    st.markdown("#### Intentos de Cobro Fallidos")
    if not failed.empty:
        by_reason = (
            failed.groupby("razon_fallo")
            .size()
            .reset_index(name="intentos")
            .sort_values("intentos", ascending=False)
        )
        st.dataframe(by_reason, use_container_width=True, hide_index=True)
        st.markdown("---")
        cols = ["fecha_utc", "cliente_email", "razon_fallo", "charge_id"]
        existing_cols = [c for c in cols if c in failed.columns]
        st.dataframe(
            failed[existing_cols].sort_values("fecha_utc", ascending=False),
            use_container_width=True, hide_index=True,
        )
    else:
        st.success("No hay intentos fallidos en este período.")

with tab3:
    st.markdown("#### Clientes con Múltiples Intentos Fallidos")
    if not failed.empty:
        failed_w_month = failed.copy()
        failed_w_month["_mes"] = (
            pd.to_datetime(failed_w_month["fecha_utc"], errors="coerce")
            .dt.strftime("%b %Y")
        )

        at_risk = (
            failed.groupby("cliente_email")
            .agg(
                intentos=("charge_id", "count"),
                ultima_razon=("razon_fallo", "last"),
            )
            .reset_index()
        )
        # Build month list per customer
        meses_por_cliente = (
            failed_w_month.groupby("cliente_email")["_mes"]
            .apply(lambda x: ", ".join(sorted(x.dropna().unique())))
            .reset_index()
            .rename(columns={"_mes": "meses"})
        )
        at_risk = at_risk.merge(meses_por_cliente, on="cliente_email", how="left")
        at_risk = at_risk[at_risk["intentos"] >= 2].sort_values("intentos", ascending=False).copy()

        def prioridad(n):
            if n >= 7: return "🔴 CRÍTICO"
            if n >= 4: return "🟠 ALTO"
            return "🟡 MEDIO"

        if not at_risk.empty:
            at_risk["prioridad"] = at_risk["intentos"].apply(prioridad)
            st.dataframe(
                at_risk[["cliente_email", "intentos", "prioridad", "meses", "ultima_razon"]],
                use_container_width=True, hide_index=True,
            )
            criticos = len(at_risk[at_risk["prioridad"] == "🔴 CRÍTICO"])
            if criticos:
                st.error(f"⚠  {criticos} cliente(s) CRÍTICO(s) — contactar esta semana")
        else:
            st.success("No hay clientes en riesgo en este período.")
    else:
        st.success("No hay intentos fallidos en este período.")

with tab4:
    st.markdown("#### Todos los Registros del Período")
    st.caption(f"{len(df)} filas · {len(df.columns)} columnas · Filtro: {period_label}")
    st.dataframe(df, use_container_width=True, hide_index=True)
