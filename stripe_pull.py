#!/usr/bin/env python3
"""
stripe_pull.py — Alternativa a N8N webhooks

Extrae directamente de la API de Stripe todos los eventos de pago
para un rango de fechas y los exporta a CSV (listo para Power BI o Google Sheets).

Uso:
  python3 stripe_pull.py --key sk_live_... --start 2026-05-12 --end 2026-05-19
  python3 stripe_pull.py --key sk_live_... --days 7          # últimos 7 días
  python3 stripe_pull.py --key sk_live_... --days 30         # backfill 30 días

Output:
  devotio_stripe_YYYYMMDD_YYYYMMDD.csv  ← abre directo en Power BI o Google Sheets

Variables de entorno (alternativa a --key):
  STRIPE_SECRET_KEY=sk_live_...

Arquitectura:
  Fuente primaria → stripe.Invoice.list()   (suscripciones: paid + failed/open)
  Fuente secundaria → stripe.Charge.list()  (payment links y cargos directos sin invoice)
  Suplemento → stripe.Event.list()          (cancelaciones de suscripción)
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import stripe

UTC = timezone.utc
CST = timezone(timedelta(hours=-6))

# ── Columnas de salida ────────────────────────────────────────────────────────
FIELDNAMES = [
    "fecha_utc",
    "fecha_cst",
    "tipo",                  # suscripcion / cargo_directo / payment_link / subscription_deleted / refund
    "origen",                # subscription_cycle / subscription_create / subscription_update / manual / payment_link
    "estado",                # paid / failed / open / refunded / canceled
    "monto_usd",
    "monto_reembolsado_usd",
    "moneda",
    "cliente_email",
    "cliente_nombre",
    "invoice_id",
    "charge_id",
    "payment_intent_id",
    "subscription_id",
    "producto_nombre",
    "razon_fallo",
    "intento_numero",
    "descripcion",
    "metadatos",
]


def to_cst(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def to_utc_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S")


def safe_get(obj, *keys, default=""):
    for k in keys:
        if obj is None:
            return default
        try:
            obj = obj[k] if isinstance(obj, dict) else getattr(obj, k, None)
        except (KeyError, AttributeError, TypeError):
            return default
    return obj if obj is not None else default


# ── Stripe fetch helpers ──────────────────────────────────────────────────────

def fetch_all(resource_fn, params: dict, label: str) -> list:
    results = []
    params = {**params, "limit": 100}
    page = 0
    while True:
        page += 1
        resp = resource_fn(**params)
        results.extend(resp.data)
        print(f"  {label}: página {page} — {len(results)} registros", end="\r")
        if not resp.has_more:
            break
        params["starting_after"] = resp.data[-1].id
        time.sleep(0.08)
    print(f"  {label}: {len(results)} registros totales          ")
    return results


# ── Invoice → row ─────────────────────────────────────────────────────────────

def row_from_invoice(inv) -> dict:
    """Primary source for subscription billing (paid + open/failed)."""
    billing_reason = safe_get(inv, "billing_reason") or ""

    if billing_reason in ("subscription_cycle", "subscription_create", "subscription_update"):
        tipo = "suscripcion"
        origen = billing_reason
    elif billing_reason == "manual":
        tipo = "cobro_manual"
        origen = "manual"
    else:
        tipo = "suscripcion"
        origen = billing_reason or "subscription_cycle"

    status = safe_get(inv, "status") or ""
    if status == "paid":
        estado = "paid"
    elif status in ("open", "uncollectible"):
        estado = "failed"
    else:
        estado = status

    amount_paid = (safe_get(inv, "amount_paid") or 0) / 100
    amount_due  = (safe_get(inv, "amount_due")  or 0) / 100
    monto = amount_paid if estado == "paid" else amount_due

    # Customer info — customer is expanded in the list call
    cust = safe_get(inv, "customer")
    email  = safe_get(inv, "customer_email") or safe_get(cust, "email") or ""
    nombre = safe_get(inv, "customer_name")  or safe_get(cust, "name")  or ""

    # Subscription ID from new parent structure
    sub_id = ""
    parent = safe_get(inv, "parent")
    if parent is not None:
        sub_details = safe_get(parent, "subscription_details")
        if sub_details is not None:
            sub_id = safe_get(sub_details, "subscription") or ""

    # Decline / failure reason
    razon_fallo = ""
    if estado == "failed":
        last_err = safe_get(inv, "last_finalization_error") or {}
        if isinstance(last_err, dict):
            razon_fallo = last_err.get("code") or last_err.get("decline_code") or ""
        else:
            razon_fallo = safe_get(last_err, "code") or safe_get(last_err, "decline_code") or ""

    # Use paid_at timestamp for paid invoices (reflects actual collection date)
    if estado == "paid":
        st = safe_get(inv, "status_transitions")
        paid_at = (st.get("paid_at") if isinstance(st, dict) else getattr(st, "paid_at", None)) if st else None
        ts = paid_at or (safe_get(inv, "created") or 0)
    else:
        ts = safe_get(inv, "created") or 0

    return {
        "fecha_utc":             to_utc_str(ts) if ts else "",
        "fecha_cst":             to_cst(ts) if ts else "",
        "tipo":                  tipo,
        "origen":                origen,
        "estado":                estado,
        "monto_usd":             f"{monto:.2f}",
        "monto_reembolsado_usd": "0.00",
        "moneda":                (safe_get(inv, "currency") or "usd").upper(),
        "cliente_email":         email,
        "cliente_nombre":        nombre,
        "invoice_id":            safe_get(inv, "id") or "",
        "charge_id":             "",  # charge→invoice link broken in new Stripe API; use invoice_id
        "payment_intent_id":     safe_get(inv, "payment_intent") or "",
        "subscription_id":       sub_id,
        "producto_nombre":       "",
        "razon_fallo":           razon_fallo,
        "intento_numero":        str(safe_get(inv, "attempt_count") or ""),
        "descripcion":           f"invoice/{billing_reason}",
        "metadatos":             str(safe_get(inv, "metadata") or ""),
    }


# ── Charge → row (direct charges only — no invoice) ──────────────────────────

def row_from_charge(ch) -> dict:
    """Secondary source: failed subscription retry attempts."""
    tipo   = "suscripcion"
    origen = "subscription_retry"

    status = safe_get(ch, "status") or ""
    if safe_get(ch, "refunded"):
        estado = "refunded"
    elif status == "succeeded":
        estado = "paid"
    elif status == "failed":
        estado = "failed"
    else:
        estado = status

    amount          = (safe_get(ch, "amount") or 0) / 100
    amount_refunded = (safe_get(ch, "amount_refunded") or 0) / 100

    cust  = safe_get(ch, "customer") or {}
    email  = safe_get(ch, "billing_details", "email") or safe_get(cust, "email") or ""
    nombre = safe_get(ch, "billing_details", "name")  or safe_get(cust, "name")  or ""

    outcome     = safe_get(ch, "outcome") or {}
    razon_fallo = ""
    if isinstance(outcome, dict):
        razon_fallo = outcome.get("decline_code") or outcome.get("reason") or ""
    else:
        razon_fallo = safe_get(outcome, "decline_code") or safe_get(outcome, "reason") or ""

    ts = safe_get(ch, "created") or 0

    return {
        "fecha_utc":             to_utc_str(ts) if ts else "",
        "fecha_cst":             to_cst(ts) if ts else "",
        "tipo":                  tipo,
        "origen":                origen,
        "estado":                estado,
        "monto_usd":             f"{amount:.2f}",
        "monto_reembolsado_usd": f"{amount_refunded:.2f}",
        "moneda":                (safe_get(ch, "currency") or "usd").upper(),
        "cliente_email":         email,
        "cliente_nombre":        nombre,
        "invoice_id":            "",
        "charge_id":             safe_get(ch, "id") or "",
        "payment_intent_id":     safe_get(ch, "payment_intent") or "",
        "subscription_id":       "",
        "producto_nombre":       "",
        "razon_fallo":           razon_fallo,
        "intento_numero":        "",
        "descripcion":           safe_get(ch, "description") or "",
        "metadatos":             str(safe_get(ch, "metadata") or ""),
    }


# ── Subscription deleted → row ────────────────────────────────────────────────

def row_from_subscription_event(sub, cust_map: dict) -> dict:
    ts = safe_get(sub, "canceled_at") or safe_get(sub, "ended_at") or safe_get(sub, "created") or 0
    cust_id = safe_get(sub, "customer") or ""
    cust    = cust_map.get(cust_id) or {}
    email  = safe_get(cust, "email") or ""
    nombre = safe_get(cust, "name")  or ""

    items    = safe_get(sub, "items") or {}
    items_data = items.get("data", []) if isinstance(items, dict) else getattr(items, "data", [])
    monto = 0.0
    if items_data:
        price   = safe_get(items_data[0], "price") or {}
        monto   = (safe_get(price, "unit_amount") or 0) / 100

    cancel_details = safe_get(sub, "cancellation_details") or {}
    origen = (cancel_details.get("reason") if isinstance(cancel_details, dict) else safe_get(cancel_details, "reason")) or "admin"

    return {
        "fecha_utc":             to_utc_str(ts) if ts else "",
        "fecha_cst":             to_cst(ts) if ts else "",
        "tipo":                  "subscription_deleted",
        "origen":                origen,
        "estado":                "canceled",
        "monto_usd":             f"{monto:.2f}",
        "monto_reembolsado_usd": "0.00",
        "moneda":                (safe_get(sub, "currency") or "usd").upper(),
        "cliente_email":         email,
        "cliente_nombre":        nombre,
        "invoice_id":            "",
        "charge_id":             "",
        "payment_intent_id":     "",
        "subscription_id":       safe_get(sub, "id") or "",
        "producto_nombre":       "",
        "razon_fallo":           "",
        "intento_numero":        "",
        "descripcion":           "subscription_deleted",
        "metadatos":             str(safe_get(sub, "metadata") or ""),
    }


# ── Main pull ─────────────────────────────────────────────────────────────────

def pull(api_key: str, start_dt: datetime, end_dt: datetime, out_path: str):
    stripe.api_key = api_key

    start_ts = int(start_dt.timestamp())
    end_ts   = int(end_dt.timestamp())

    print(f"\nExtrayendo datos de Stripe: {start_dt.date()} → {end_dt.date()}")
    print(f"{'─'*58}")

    rows = []
    seen: set[str] = set()

    def add(row: dict, key: str):
        if key not in seen:
            seen.add(key)
            rows.append(row)

    # ── 1. Invoices (primary — subscription payments) ────────────────────────
    # Invoices are created days before payment (Stripe bills in advance, then
    # retries failed payments). Filter by status_transitions.paid_at, not
    # invoice.created, using a 30-day lookback window to catch all invoices
    # paid in the target date range regardless of when they were created.
    LOOKBACK_DAYS = 30
    lookback_ts   = start_ts - (LOOKBACK_DAYS * 86400)
    print(f"[1/4] Facturas de suscripción (últimos {LOOKBACK_DAYS}d de lookback)...")
    all_invoices = fetch_all(
        stripe.Invoice.list,
        {
            "created": {"gte": lookback_ts, "lte": end_ts},
            "expand": ["data.customer"],
        },
        "invoices",
    )
    invoice_ids: set[str] = set()
    for inv in all_invoices:
        status = safe_get(inv, "status") or ""
        if status != "paid":
            continue  # failed/open invoices are captured via charges
        st = safe_get(inv, "status_transitions")
        paid_at = (st.get("paid_at") if isinstance(st, dict) else getattr(st, "paid_at", None)) if st else None
        if paid_at is None or not (start_ts <= paid_at <= end_ts):
            continue
        row = row_from_invoice(inv)
        inv_id = row["invoice_id"]
        invoice_ids.add(inv_id)
        add(row, f"inv_{inv_id}")

    # ── 2. Failed charges (retry attempts for any subscription invoice) ───────
    # In Stripe's new billing API, ch.invoice is always None — the link has been
    # severed at the object level. Invoices cover all PAID subscription payments.
    # Failed charges must come from the charge list because retry attempts have
    # charge.created in the report window while the original invoice.created can
    # be days or weeks earlier.
    # Note: pmd.type="link" means Stripe Link (saved-card checkout), NOT a
    # standalone Payment Link — all such charges are subscription-backed and are
    # already in the invoice pull. Only include status=failed charges.
    print("[2/4] Intentos fallidos (charges)...")
    charges = fetch_all(
        stripe.Charge.list,
        {
            "created": {"gte": start_ts, "lte": end_ts},
            "expand": ["data.customer"],
        },
        "charges",
    )
    refund_charges: list = []
    for ch in charges:
        ch_status = ch.get("status") or ""

        if ch_status == "failed":
            row = row_from_charge(ch)
            add(row, f"charge_{ch['id']}")

        # Collect refunds regardless
        if (ch.get("amount_refunded") or 0) > 0:
            refund_charges.append(ch)

    # ── 3. Refunds ────────────────────────────────────────────────────────────
    print("[3/4] Reembolsos...")
    refund_count = 0
    for ch in refund_charges:
        refunds = ch.get("refunds", {})
        refund_data = refunds.get("data", []) if isinstance(refunds, dict) else getattr(refunds, "data", [])
        for ref in refund_data:
            ref_ts = safe_get(ref, "created") or 0
            if not (start_ts <= ref_ts <= end_ts):
                continue
            ref_id = safe_get(ref, "id") or ""
            ref_amount = (safe_get(ref, "amount") or 0) / 100
            row = {
                "fecha_utc":             to_utc_str(ref_ts),
                "fecha_cst":             to_cst(ref_ts),
                "tipo":                  "refund",
                "origen":                safe_get(ref, "reason") or "manual",
                "estado":                "refunded",
                "monto_usd":             f"{ref_amount:.2f}",
                "monto_reembolsado_usd": f"{ref_amount:.2f}",
                "moneda":                (safe_get(ref, "currency") or "usd").upper(),
                "cliente_email":         safe_get(ch, "billing_details", "email") or safe_get(ch, "customer", "email") or "",
                "cliente_nombre":        safe_get(ch, "billing_details", "name")  or safe_get(ch, "customer", "name")  or "",
                "invoice_id":            "",
                "charge_id":             safe_get(ch, "id") or "",
                "payment_intent_id":     safe_get(ch, "payment_intent") or "",
                "subscription_id":       "",
                "producto_nombre":       "",
                "razon_fallo":           "",
                "intento_numero":        "",
                "descripcion":           "refund",
                "metadatos":             str(safe_get(ref, "metadata") or ""),
            }
            add(row, f"refund_{ref_id}")
            refund_count += 1
    print(f"  refunds: {refund_count} registros totales          ")

    # ── 4. Subscription cancellations ─────────────────────────────────────────
    print("[4/4] Cancelaciones de suscripción...")
    cust_map: dict[str, dict] = {}
    try:
        events = fetch_all(
            stripe.Event.list,
            {"type": "customer.subscription.deleted", "created": {"gte": start_ts, "lte": end_ts}},
            "subscription.deleted",
        )
        for evt in events:
            sub = safe_get(evt, "data", "object") or {}
            cust_id = safe_get(sub, "customer") or ""
            if cust_id and cust_id not in cust_map:
                try:
                    c = stripe.Customer.retrieve(cust_id)
                    cust_map[cust_id] = {"email": c.get("email",""), "name": c.get("name","")}
                except Exception:
                    cust_map[cust_id] = {}
            row = row_from_subscription_event(sub, cust_map)
            sub_id = row["subscription_id"]
            ts_key = row["fecha_utc"]
            add(row, f"sub_del_{sub_id}_{ts_key}")
    except stripe.error.StripeError as e:
        print(f"  [WARN] Cancelaciones no disponibles: {e}")

    # ── Sort and write ────────────────────────────────────────────────────────
    rows.sort(key=lambda r: r["fecha_utc"])

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    # ── Summary ───────────────────────────────────────────────────────────────
    paid      = [r for r in rows if r["estado"] == "paid"]
    failed    = [r for r in rows if r["estado"] in ("failed", "open")]
    refunds   = [r for r in rows if r["tipo"] == "refund"]
    cancelled = [r for r in rows if r["tipo"] == "subscription_deleted"]
    direct    = [r for r in rows if r["tipo"] in ("payment_link", "cargo_directo")]

    total_paid = sum(float(r["monto_usd"]) for r in paid)
    total_ref  = sum(float(r["monto_usd"]) for r in refunds)

    from collections import Counter
    origin_counts = Counter(r["origen"] for r in paid)

    print(f"\n{'═'*58}")
    print("RESUMEN")
    print(f"{'═'*58}")
    print(f"Registros totales:          {len(rows)}")
    print(f"Pagos exitosos (Paid):      {len(paid)} — ${total_paid:,.2f}")
    for k, v in origin_counts.most_common():
        sub = [r for r in paid if r["origen"] == k]
        print(f"  · {k:30}  {v:3}  ${sum(float(r['monto_usd']) for r in sub):,.2f}")
    print(f"Pagos fallidos:             {len(failed)}")
    print(f"Reembolsos:                 {len(refunds)} — ${total_ref:,.2f}")
    print(f"Cancelaciones:              {len(cancelled)}")
    print(f"Cargos directos (no sub):   {len(direct)}")
    print(f"\nCSV guardado: {out_path}")
    print(f"{'═'*58}\n")

    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extrae datos de Stripe directamente (alternativa a webhooks N8N)"
    )
    parser.add_argument("--key",   help="Stripe secret key (o usa STRIPE_SECRET_KEY env var)")
    parser.add_argument("--start", help="Fecha inicio YYYY-MM-DD")
    parser.add_argument("--end",   help="Fecha fin YYYY-MM-DD (inclusive, default: hoy)")
    parser.add_argument("--days",  type=int, help="Últimos N días (alternativa a --start/--end)")
    parser.add_argument("--out",   help="Ruta de salida del CSV (default: auto-generada)")
    args = parser.parse_args()

    api_key = args.key or os.environ.get("STRIPE_SECRET_KEY")
    if not api_key:
        print("ERROR: Provee --key sk_live_... o define STRIPE_SECRET_KEY")
        sys.exit(1)

    now = datetime.now(UTC)

    if args.days:
        end_dt   = now
        start_dt = now - timedelta(days=args.days)
    elif args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
        end_dt   = (
            datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC)
            .replace(hour=23, minute=59, second=59)
            if args.end else now
        )
    else:
        end_dt   = now
        start_dt = now - timedelta(days=7)

    out_path = args.out or (
        f"devotio_stripe_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}.csv"
    )

    pull(api_key, start_dt, end_dt, out_path)


if __name__ == "__main__":
    main()
