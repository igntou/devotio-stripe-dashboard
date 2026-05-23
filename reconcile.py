#!/usr/bin/env python3
"""
Devotio Rewards — Script de reconciliación Stripe Manual vs N8N Auto
Uso: python3 reconcile.py --manual "unified_payments (7).csv" --auto "Devotio_financial_report - devotio_raw (3).csv"
Flags opcionales:
  --start YYYY-MM-DD   Filtrar desde esta fecha (UTC manual, convierte auto CST→UTC)
  --end   YYYY-MM-DD   Filtrar hasta esta fecha (inclusive)
  --gap-hours N        Umbral de horas sin eventos para detectar ventana ciega (default: 4)
  --stripe-key sk_...  API key para enriquecer subscription.deleted sin email
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=-6))
UTC = timezone.utc
TIMESTAMP_TOLERANCE_SEC = 120  # ±2 minutos


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_utc(s: str) -> datetime | None:
    """Parse 'YYYY-MM-DD HH:MM:SS' as UTC."""
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_cst_iso(s: str) -> datetime | None:
    """Parse ISO8601 with offset (N8N format: 2026-04-11T12:37:02.000-06:00)."""
    try:
        return datetime.fromisoformat(s.strip()).astimezone(UTC)
    except ValueError:
        return None


def parse_amount(s: str) -> float:
    try:
        return round(float(str(s).replace("$", "").replace(",", "").strip()), 2)
    except (ValueError, AttributeError):
        return 0.0


# ---------------------------------------------------------------------------
# Load CSVs
# ---------------------------------------------------------------------------

def load_manual(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r["_ts"] = parse_utc(r.get("Created date (UTC)", ""))
            r["_amount"] = parse_amount(r.get("Amount", 0))
            r["_amount_refunded"] = parse_amount(r.get("Amount Refunded", 0))
            r["_email"] = (r.get("Customer Email") or "").strip().lower()
            r["_invoice_id"] = (r.get("Invoice ID") or "").strip()
            r["_status"] = (r.get("Status") or "").strip()
            r["_id"] = (r.get("id") or "").strip()
            rows.append(r)
    return rows


def load_auto(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r["_ts"] = parse_cst_iso(r.get("created_at", ""))
            r["_amount"] = parse_amount(r.get("amount_paid", 0))
            r["_email"] = (r.get("customer_email") or "").strip().lower()
            r["_invoice_id"] = (r.get("invoice_id") or "").strip()
            r["_event_type"] = (r.get("event_type") or "").strip()
            r["_event_id"] = (r.get("event_id") or "").strip()
            rows.append(r)
    return rows


# ---------------------------------------------------------------------------
# Date filtering
# ---------------------------------------------------------------------------

def filter_by_date(rows: list[dict], start: datetime | None, end: datetime | None) -> list[dict]:
    out = []
    for r in rows:
        ts = r.get("_ts")
        if ts is None:
            continue
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Deduplication: drop invoice.paid when invoice.payment_succeeded exists
# ---------------------------------------------------------------------------

def deduplicate_auto(rows: list[dict]) -> list[dict]:
    succeeded_invoices = {
        r["_invoice_id"]
        for r in rows
        if r["_event_type"] == "invoice.payment_succeeded" and r["_invoice_id"]
    }
    out = []
    double_count_ids = []
    for r in rows:
        if r["_event_type"] == "invoice.paid" and r["_invoice_id"] in succeeded_invoices:
            double_count_ids.append(r["_invoice_id"])
            continue
        out.append(r)
    return out, double_count_ids


# ---------------------------------------------------------------------------
# Double charge detection: same subscription charged twice within N days
# ---------------------------------------------------------------------------

def detect_double_charges(auto_rows: list[dict], window_days: float = 7) -> list[dict]:
    """
    Detect potential double charges. Two patterns:
    1. Same subscription_id charged twice within window_days (standard).
    2. Same customer_email with subscription_update + any other charge within window_days
       (covers cases where subscription_id is missing in payload — e.g., after plan change).
    """
    succeeded = [
        r for r in auto_rows
        if r["_event_type"] == "invoice.payment_succeeded" and r["_invoice_id"]
    ]

    # Group by subscription_id first, fallback to email when sub_id missing
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in succeeded:
        sub_id = (r.get("subscription_id") or "").strip()
        key = sub_id if sub_id else f"email:{r['_email']}"
        by_key[key].append(r)

    doubles = []
    seen_pairs: set[tuple] = set()

    for key, rows in by_key.items():
        rows_sorted = sorted(
            rows, key=lambda x: x["_ts"] or datetime.min.replace(tzinfo=UTC)
        )
        for i in range(len(rows_sorted) - 1):
            r1 = rows_sorted[i]
            r2 = rows_sorted[i + 1]
            t1, t2 = r1["_ts"], r2["_ts"]
            if not (t1 and t2):
                continue
            diff_days = (t2 - t1).total_seconds() / 86400
            if diff_days > window_days:
                continue

            # Skip if this pair of invoices was already added
            pair_key = tuple(sorted([r1["_invoice_id"], r2["_invoice_id"]]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            br1 = r1.get("billing_reason", "")
            br2 = r2.get("billing_reason", "")
            # Flag if either charge is subscription_update OR if same amount charged twice quickly
            is_suspicious = (
                "subscription_update" in (br1, br2)
                or (abs(r1["_amount"] - r2["_amount"]) < 0.01 and diff_days < 1)
            )
            if not is_suspicious:
                continue

            doubles.append({
                "grouping_key": key,
                "customer_email": r1["_email"],
                "charge_1_ts": str(t1),
                "charge_1_invoice": r1["_invoice_id"],
                "charge_1_amount": r1["_amount"],
                "charge_1_billing_reason": br1,
                "charge_2_ts": str(t2),
                "charge_2_invoice": r2["_invoice_id"],
                "charge_2_amount": r2["_amount"],
                "charge_2_billing_reason": br2,
                "diff_hours": round((t2 - t1).total_seconds() / 3600, 1),
                "diff_days": round(diff_days, 2),
                "tipo": "MISMO_MONTO_RAPIDO" if abs(r1["_amount"] - r2["_amount"]) < 0.01 and diff_days < 1
                        else "UPDATE_MAS_CYCLE",
            })
    return doubles


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

MATCH_INVOICE = "MATCH — Invoice ID"
MATCH_PAYMENT_INTENT = "MATCH — via payment_intent"
SOLO_MANUAL_GAP = "SOLO MANUAL — Gap N8N (backfill)"
SOLO_MANUAL_NOT_CAPTURED = "SOLO MANUAL — Exitoso no capturado (post-gap)"
SOLO_MANUAL_FAILED_NO_INV = "SOLO MANUAL — Fallido sin invoice"
SOLO_AUTO = "SOLO AUTO — Sin factura manual"


def detect_gap_windows(auto_rows: list[dict], gap_hours: float) -> list[tuple[datetime, datetime]]:
    """Return list of (gap_start, gap_end) windows where no events were received."""
    times = sorted(
        r["_ts"] for r in auto_rows if r["_ts"] is not None
    )
    if len(times) < 2:
        return []
    gaps = []
    threshold = timedelta(hours=gap_hours)
    for i in range(len(times) - 1):
        if times[i + 1] - times[i] > threshold:
            gaps.append((times[i], times[i + 1]))
    return gaps


def in_gap(ts: datetime, gaps: list[tuple]) -> bool:
    for start, end in gaps:
        if start <= ts <= end:
            return True
    return False


def reconcile(
    manual_rows: list[dict],
    auto_rows: list[dict],
    gaps: list[tuple],
) -> list[dict]:
    # Index auto by invoice_id
    auto_by_invoice = defaultdict(list)
    for r in auto_rows:
        if r["_invoice_id"]:
            auto_by_invoice[r["_invoice_id"]].append(r)

    matched_auto_event_ids = set()
    results = []

    # --- Pass 1: manual rows
    for m in manual_rows:
        inv = m["_invoice_id"]
        status = m["_status"]
        ts = m["_ts"]

        # Try invoice_id match
        if inv and inv in auto_by_invoice:
            auto_matches = auto_by_invoice[inv]
            for a in auto_matches:
                matched_auto_event_ids.add(a["_event_id"])
            results.append({
                **_manual_fields(m),
                "match_status": MATCH_INVOICE,
                "auto_event_id": ";".join(a["_event_id"] for a in auto_matches),
                "auto_event_type": ";".join(a["_event_type"] for a in auto_matches),
                "auto_created_at": ";".join(str(a["_ts"]) for a in auto_matches),
                "auto_amount": auto_matches[0]["_amount"],
                "notes": "",
            })
            continue

        # Failed with no invoice — normal pattern
        if status == "Failed" and not inv:
            results.append({
                **_manual_fields(m),
                "match_status": SOLO_MANUAL_FAILED_NO_INV,
                "auto_event_id": "",
                "auto_event_type": "",
                "auto_created_at": "",
                "auto_amount": "",
                "notes": "Failed sin invoice_id — payment link directo o primer intento",
            })
            continue

        # Failed with invoice — try to match in auto by invoice
        if status == "Failed":
            if inv and inv in auto_by_invoice:
                auto_matches = auto_by_invoice[inv]
                for a in auto_matches:
                    matched_auto_event_ids.add(a["_event_id"])
                results.append({
                    **_manual_fields(m),
                    "match_status": MATCH_INVOICE,
                    "auto_event_id": ";".join(a["_event_id"] for a in auto_matches),
                    "auto_event_type": ";".join(a["_event_type"] for a in auto_matches),
                    "auto_created_at": ";".join(str(a["_ts"]) for a in auto_matches),
                    "auto_amount": auto_matches[0]["_amount"],
                    "notes": "",
                })
            else:
                gap_note = "En ventana ciega N8N" if ts and in_gap(ts, gaps) else "Post-gap — verificar Stripe Webhook Logs"
                results.append({
                    **_manual_fields(m),
                    "match_status": SOLO_MANUAL_GAP if (ts and in_gap(ts, gaps)) else SOLO_MANUAL_NOT_CAPTURED,
                    "auto_event_id": "",
                    "auto_event_type": "",
                    "auto_created_at": "",
                    "auto_amount": "",
                    "notes": gap_note,
                })
            continue

        # Paid — try secondary match: email + amount + timestamp ±2min
        if status == "Paid" and ts:
            secondary = _find_secondary_match(m, auto_rows, matched_auto_event_ids)
            if secondary:
                matched_auto_event_ids.add(secondary["_event_id"])
                results.append({
                    **_manual_fields(m),
                    "match_status": MATCH_PAYMENT_INTENT,
                    "auto_event_id": secondary["_event_id"],
                    "auto_event_type": secondary["_event_type"],
                    "auto_created_at": str(secondary["_ts"]),
                    "auto_amount": secondary["_amount"],
                    "notes": "Cruce secundario email+monto+timestamp",
                })
                continue

        # Not matched
        if ts and in_gap(ts, gaps):
            match_status = SOLO_MANUAL_GAP
            notes = "En ventana ciega N8N — backfill manual requerido"
        elif status == "Paid":
            match_status = SOLO_MANUAL_NOT_CAPTURED
            notes = "Pago exitoso sin captura en auto — revisar Stripe Webhook Logs"
        else:
            match_status = SOLO_MANUAL_NOT_CAPTURED
            notes = f"Estado {status} — sin captura en auto"

        results.append({
            **_manual_fields(m),
            "match_status": match_status,
            "auto_event_id": "",
            "auto_event_type": "",
            "auto_created_at": "",
            "auto_amount": "",
            "notes": notes,
        })

    # --- Pass 2: auto rows not matched (SOLO AUTO)
    for a in auto_rows:
        if a["_event_id"] not in matched_auto_event_ids:
            results.append({
                "manual_charge_id": "",
                "manual_date_utc": "",
                "manual_status": "",
                "manual_amount": "",
                "manual_amount_refunded": "",
                "manual_email": "",
                "manual_invoice_id": "",
                "manual_decline_reason": "",
                "manual_description": "",
                "match_status": SOLO_AUTO,
                "auto_event_id": a["_event_id"],
                "auto_event_type": a["_event_type"],
                "auto_created_at": str(a["_ts"]),
                "auto_amount": a["_amount"],
                "notes": "Evento en auto sin contraparte en manual (payment link, fuera de rango, o sub.deleted)",
            })

    return results


def _manual_fields(m: dict) -> dict:
    return {
        "manual_charge_id": m["_id"],
        "manual_date_utc": str(m["_ts"]) if m["_ts"] else "",
        "manual_status": m["_status"],
        "manual_amount": m["_amount"],
        "manual_amount_refunded": m["_amount_refunded"],
        "manual_email": m["_email"],
        "manual_invoice_id": m["_invoice_id"],
        "manual_decline_reason": m.get("Decline Reason", ""),
        "manual_description": m.get("Description", ""),
    }


def _find_secondary_match(
    manual_row: dict,
    auto_rows: list[dict],
    already_matched: set,
) -> dict | None:
    email = manual_row["_email"]
    amount = manual_row["_amount"]
    ts = manual_row["_ts"]
    tolerance = timedelta(seconds=TIMESTAMP_TOLERANCE_SEC)

    for a in auto_rows:
        if a["_event_id"] in already_matched:
            continue
        if a["_event_type"] not in ("payment_intent.succeeded",):
            continue
        if a["_email"] != email:
            continue
        if abs(a["_amount"] - amount) > 0.01:
            continue
        if a["_ts"] and abs(a["_ts"] - ts) <= tolerance:
            return a
    return None


# ---------------------------------------------------------------------------
# At-risk customers from auto
# ---------------------------------------------------------------------------

def at_risk_customers(auto_rows: list[dict], min_attempts: int = 4) -> list[dict]:
    """Customers with attempt_count >= min_attempts, grouped by email."""
    by_email = defaultdict(list)
    for r in auto_rows:
        if r["_event_type"] in ("invoice.payment_failed", "payment_intent.payment_failed"):
            email = r["_email"]
            try:
                attempts = int(r.get("attempt_count") or 0)
            except (ValueError, TypeError):
                attempts = 0
            if attempts >= 1:
                by_email[email].append({
                    "email": email,
                    "customer_name": r.get("customer_name", ""),
                    "attempt_count": attempts,
                    "amount_due": r.get("amount_due", ""),
                    "decline_reason": r.get("decline_reason", ""),
                    "last_ts": r["_ts"],
                    "subscription_id": r.get("subscription_id", ""),
                })

    results = []
    for email, events in by_email.items():
        max_attempts = max(e["attempt_count"] for e in events)
        if max_attempts >= min_attempts:
            last = max(events, key=lambda x: x["last_ts"] or datetime.min.replace(tzinfo=UTC))
            results.append({
                "email": email,
                "customer_name": last["customer_name"],
                "max_attempt_count": max_attempts,
                "amount_due": last["amount_due"],
                "last_decline_reason": last["decline_reason"],
                "last_failed_ts": str(last["last_ts"]),
                "subscription_id": last["subscription_id"],
                "alert": "CRITICO" if max_attempts >= 9 else ("ALTO" if max_attempts >= 6 else "MEDIO"),
            })

    return sorted(results, key=lambda x: -x["max_attempt_count"])


# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------

def build_summary(
    results: list[dict],
    double_count_ids: list[str],
    double_charges: list[dict],
    at_risk: list[dict],
    gaps: list[tuple],
) -> dict:
    by_status = defaultdict(list)
    for r in results:
        by_status[r["match_status"]].append(r)

    paid_matched = [
        r for r in by_status[MATCH_INVOICE] + by_status[MATCH_PAYMENT_INTENT]
        if r["manual_status"] == "Paid"
    ]
    paid_not_captured = [
        r for r in by_status[SOLO_MANUAL_NOT_CAPTURED]
        if r["manual_status"] == "Paid"
    ]
    paid_backfill = [
        r for r in by_status[SOLO_MANUAL_GAP]
        if r["manual_status"] == "Paid"
    ]

    total_manual_paid = sum(
        float(r["manual_amount"] or 0)
        for r in results
        if r["manual_status"] == "Paid" and r["manual_amount"]
    )
    total_matched_paid = sum(float(r["manual_amount"] or 0) for r in paid_matched)

    return {
        "totales": {
            "manual_rows": len([r for r in results if r["manual_charge_id"]]),
            "auto_events": len([r for r in results if r["auto_event_id"]]),
            "match_invoice_id": len(by_status[MATCH_INVOICE]),
            "match_payment_intent": len(by_status[MATCH_PAYMENT_INTENT]),
            "solo_manual_gap_backfill": len(by_status[SOLO_MANUAL_GAP]),
            "solo_manual_not_captured": len(by_status[SOLO_MANUAL_NOT_CAPTURED]),
            "solo_manual_failed_no_invoice": len(by_status[SOLO_MANUAL_FAILED_NO_INV]),
            "solo_auto": len(by_status[SOLO_AUTO]),
        },
        "financiero": {
            "total_manual_paid_usd": round(total_manual_paid, 2),
            "total_matched_paid_usd": round(total_matched_paid, 2),
            "cobertura_pct": round(total_matched_paid / total_manual_paid * 100, 1) if total_manual_paid else 0,
            "pagos_exitosos_no_capturados": len(paid_not_captured),
            "monto_no_capturado_usd": round(sum(float(r["manual_amount"] or 0) for r in paid_not_captured), 2),
            "pagos_backfill_requeridos": len(paid_backfill),
            "monto_backfill_usd": round(sum(float(r["manual_amount"] or 0) for r in paid_backfill), 2),
        },
        "double_counting": {
            "invoice_paid_duplicados_detectados": len(double_count_ids),
            "invoice_ids": double_count_ids[:20],
        },
        "cobros_dobles_suscripcion": {
            "total": len(double_charges),
            "detalle": double_charges[:20],
        },
        "gaps_detectados": [
            {
                "inicio_utc": str(g[0]),
                "fin_utc": str(g[1]),
                "duracion_horas": round((g[1] - g[0]).total_seconds() / 3600, 1),
            }
            for g in gaps
        ],
        "clientes_en_riesgo": {
            "total_en_riesgo": len(at_risk),
            "criticos": len([c for c in at_risk if c["alert"] == "CRITICO"]),
            "detalle": at_risk[:20],
        },
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "match_status",
    "manual_charge_id",
    "manual_date_utc",
    "manual_status",
    "manual_amount",
    "manual_amount_refunded",
    "manual_email",
    "manual_invoice_id",
    "manual_decline_reason",
    "manual_description",
    "auto_event_id",
    "auto_event_type",
    "auto_created_at",
    "auto_amount",
    "notes",
]


def write_csv(results: list[dict], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


def write_json(data: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# Stripe enrichment for subscription.deleted
# ---------------------------------------------------------------------------

def enrich_subscription_deleted(auto_rows: list[dict], stripe_key: str) -> list[dict]:
    try:
        import urllib.request
        import base64
    except ImportError:
        print("[WARN] No se pudo importar urllib — enriquecimiento omitido")
        return auto_rows

    needs_enrich = [
        r for r in auto_rows
        if r["_event_type"] == "customer.subscription.deleted" and not r["_email"]
    ]
    if not needs_enrich:
        return auto_rows

    print(f"[INFO] Enriqueciendo {len(needs_enrich)} subscription.deleted sin email...")
    auth = base64.b64encode(f"{stripe_key}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}

    for r in needs_enrich:
        sub_id = (r.get("subscription_id") or "").strip()
        if not sub_id:
            continue
        try:
            req = urllib.request.Request(
                f"https://api.stripe.com/v1/subscriptions/{sub_id}",
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                cust_id = data.get("customer")
                if cust_id:
                    creq = urllib.request.Request(
                        f"https://api.stripe.com/v1/customers/{cust_id}",
                        headers=headers,
                    )
                    with urllib.request.urlopen(creq, timeout=10) as cresp:
                        cdata = json.loads(cresp.read())
                        r["customer_email"] = cdata.get("email", "")
                        r["customer_name"] = cdata.get("name", "")
                        r["_email"] = r["customer_email"].strip().lower()
                        print(f"  ✓ {sub_id} → {r['customer_email']}")
        except Exception as e:
            print(f"  ✗ {sub_id}: {e}")

    return auto_rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Reconciliación Stripe Manual vs N8N Auto")
    parser.add_argument("--manual", required=True, help="CSV exportado de Stripe Dashboard")
    parser.add_argument("--auto", required=True, help="CSV del reporte N8N (Google Sheet)")
    parser.add_argument("--start", help="Fecha inicio filtro YYYY-MM-DD (UTC)")
    parser.add_argument("--end", help="Fecha fin filtro YYYY-MM-DD (UTC, inclusive)")
    parser.add_argument("--gap-hours", type=float, default=4.0, help="Horas sin eventos para gap (default: 4)")
    parser.add_argument("--stripe-key", help="Stripe secret key para enriquecer subscription.deleted")
    parser.add_argument("--out-dir", default=".", help="Directorio de salida")
    parser.add_argument("--double-charge-days", type=float, default=7.0,
                        help="Ventana en días para detectar cobros dobles (default: 7)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Parse date filters
    start_dt = None
    end_dt = None
    if args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
    if args.end:
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(
            tzinfo=UTC, hour=23, minute=59, second=59
        )

    print(f"[1/7] Cargando manual: {args.manual}")
    manual_all = load_manual(args.manual)

    print(f"[2/7] Cargando auto: {args.auto}")
    auto_all = load_auto(args.auto)

    # Enrich subscription.deleted if key provided
    if args.stripe_key:
        print("[2b] Enriqueciendo subscription.deleted...")
        auto_all = enrich_subscription_deleted(auto_all, args.stripe_key)

    # Deduplication (invoice.paid + invoice.payment_succeeded)
    print("[3/7] Deduplicando eventos invoice.paid...")
    auto_all, double_count_ids = deduplicate_auto(auto_all)
    if double_count_ids:
        print(f"  ⚠️  {len(double_count_ids)} invoice.paid duplicados eliminados — Bug #1 regresó!")
    else:
        print("  ✓ Sin invoice.paid duplicados (Bug #1 bajo control)")

    # Date filters
    print(f"[4/7] Filtrando fechas: {args.start or 'inicio'} → {args.end or 'fin'}")
    manual = filter_by_date(manual_all, start_dt, end_dt)
    auto = filter_by_date(auto_all, start_dt, end_dt)
    print(f"  Manual: {len(manual)} rows | Auto: {len(auto)} events")

    # Gap detection (use full auto for context, filtered for display)
    print(f"[5/7] Detectando ventanas de gap (umbral: {args.gap_hours}h)...")
    gaps = detect_gap_windows(auto_all, args.gap_hours)
    print(f"  {len(gaps)} gaps detectados")
    for g in gaps:
        hours = (g[1] - g[0]).total_seconds() / 3600
        print(f"  → {g[0]} → {g[1]} ({hours:.1f}h)")

    # Double charge detection — runs on FULL auto (not date-filtered) to capture all history
    print(f"[5b] Detectando cobros dobles en dataset completo (ventana: {args.double_charge_days} días)...")
    double_charges = detect_double_charges(auto_all, args.double_charge_days)
    if double_charges:
        print(f"  ⚠️  {len(double_charges)} pares de cobros dobles detectados!")
        for dc in double_charges:
            print(f"  → {dc['customer_email']} — {dc['diff_hours']}h entre cobros | {dc['tipo']} | {dc['charge_1_billing_reason']}+{dc['charge_2_billing_reason']}")
    else:
        print("  ✓ Sin cobros dobles detectados")

    # Reconciliation
    print("[6/7] Ejecutando reconciliación...")
    results = reconcile(manual, auto, gaps)

    # At-risk customers
    print("[7/7] Identificando clientes en riesgo...")
    at_risk = at_risk_customers(auto_all)
    print(f"  {len(at_risk)} clientes con attempt_count alto")

    # Build summary
    summary = build_summary(results, double_count_ids, double_charges, at_risk, gaps)

    # Output files
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_out = out_dir / f"reconciliacion_{timestamp}.csv"
    json_out = out_dir / f"reconciliacion_{timestamp}_resumen.json"

    write_csv(results, str(csv_out))
    write_json(summary, str(json_out))

    # Print summary
    print("\n" + "=" * 60)
    print("RESUMEN DE RECONCILIACIÓN")
    print("=" * 60)

    t = summary["totales"]
    f = summary["financiero"]

    print(f"\nFilas manual:              {t['manual_rows']}")
    print(f"Eventos auto:              {t['auto_events']}")
    print(f"\n✅ MATCH Invoice ID:        {t['match_invoice_id']}")
    print(f"✅ MATCH payment_intent:    {t['match_payment_intent']}")
    print(f"⚠️  Gap N8N (backfill):      {t['solo_manual_gap_backfill']}")
    print(f"🔴 No capturados (post-gap): {t['solo_manual_not_captured']}")
    print(f"ℹ️  Failed sin invoice:       {t['solo_manual_failed_no_invoice']}")
    print(f"ℹ️  Solo auto:               {t['solo_auto']}")

    print(f"\n💰 Total manual Paid:       ${f['total_manual_paid_usd']:,.2f}")
    print(f"💰 Total matcheado:         ${f['total_matched_paid_usd']:,.2f}")
    print(f"📊 Cobertura:               {f['cobertura_pct']}%")
    print(f"❌ Monto no capturado:      ${f['monto_no_capturado_usd']:,.2f} ({f['pagos_exitosos_no_capturados']} pagos)")
    print(f"📝 Monto backfill:          ${f['monto_backfill_usd']:,.2f} ({f['pagos_backfill_requeridos']} pagos)")

    dc = summary["double_counting"]
    print(f"\n🔁 Double counting (invoice.paid): {dc['invoice_paid_duplicados_detectados']}")

    cd = summary["cobros_dobles_suscripcion"]
    print(f"🔁 Cobros dobles detectados (dataset completo): {cd['total']}")
    for dc in cd["detalle"][:5]:
        print(f"   {dc['customer_email']} — {dc['diff_hours']}h | {dc['tipo']}")

    risk = summary["clientes_en_riesgo"]
    print(f"\n🚨 Clientes en riesgo:     {risk['total_en_riesgo']} ({risk['criticos']} CRITICOS)")
    for c in risk["detalle"][:5]:
        print(f"   {c['alert']:8} {c['email']} — {c['max_attempt_count']} intentos — {c['last_decline_reason']}")

    print(f"\n📁 CSV:  {csv_out}")
    print(f"📁 JSON: {json_out}")
    print("=" * 60)


if __name__ == "__main__":
    main()
