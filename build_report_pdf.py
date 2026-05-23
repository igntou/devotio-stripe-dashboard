#!/usr/bin/env python3
"""
Genera Devotio_Reporte_Validacion_May12-18_2026.pdf
Uso: python3 build_report_pdf.py
"""

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch, cm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether, PageBreak
)
from reportlab.platypus.flowables import Flowable
from reportlab.lib.colors import HexColor
import os

# ── Palette ───────────────────────────────────────────────────────────────────
BRAND_DARK   = HexColor("#1A1A2E")
BRAND_MID    = HexColor("#162136")
ACCENT_BLUE  = HexColor("#0F3A75")
ACCENT_LIGHT = HexColor("#E8F0FB")
RED          = HexColor("#C0392B")
ORANGE       = HexColor("#D36800")
GREEN        = HexColor("#1A7A40")
GREEN_BG     = HexColor("#EAF7EE")
ORANGE_BG    = HexColor("#FFF4E5")
RED_BG       = HexColor("#FDECEA")
TABLE_HDR    = HexColor("#0F3A75")
TABLE_ALT    = HexColor("#F0F4FA")
WHITE        = colors.white
MUTED        = HexColor("#666666")
BORDER       = HexColor("#CCCCCC")
TEXT         = HexColor("#1C1C1C")

W, H = LETTER

# ── Styles ────────────────────────────────────────────────────────────────────
def make_styles():
    base = getSampleStyleSheet()
    s = {}

    s["cover_title"] = ParagraphStyle("cover_title",
        fontName="Helvetica-Bold", fontSize=22, textColor=BRAND_DARK,
        alignment=TA_CENTER, spaceAfter=6)

    s["cover_sub"] = ParagraphStyle("cover_sub",
        fontName="Helvetica", fontSize=13, textColor=BRAND_MID,
        alignment=TA_CENTER, spaceAfter=4)

    s["cover_meta"] = ParagraphStyle("cover_meta",
        fontName="Helvetica", fontSize=9, textColor=MUTED,
        alignment=TA_CENTER, spaceAfter=16)

    s["h1"] = ParagraphStyle("h1",
        fontName="Helvetica-Bold", fontSize=13, textColor=BRAND_DARK,
        spaceBefore=18, spaceAfter=6,
        borderPad=(0,0,0,8), leftIndent=10,
        borderColor=ACCENT_BLUE, borderWidth=0)

    s["h2"] = ParagraphStyle("h2",
        fontName="Helvetica-Bold", fontSize=11, textColor=BRAND_MID,
        spaceBefore=10, spaceAfter=4)

    s["body"] = ParagraphStyle("body",
        fontName="Helvetica", fontSize=9.5, textColor=TEXT,
        spaceBefore=2, spaceAfter=5, leading=14)

    s["bullet"] = ParagraphStyle("bullet",
        fontName="Helvetica", fontSize=9.5, textColor=TEXT,
        spaceBefore=1, spaceAfter=2, leading=13,
        leftIndent=14, firstLineIndent=-10)

    s["callout"] = ParagraphStyle("callout",
        fontName="Helvetica-Oblique", fontSize=9, textColor=HexColor("#444444"),
        spaceBefore=4, spaceAfter=6, leading=13,
        leftIndent=14, rightIndent=14,
        backColor=ORANGE_BG, borderPad=6,
        borderColor=ORANGE, borderWidth=1)

    s["callout_red"] = ParagraphStyle("callout_red",
        fontName="Helvetica-Oblique", fontSize=9, textColor=HexColor("#444444"),
        spaceBefore=4, spaceAfter=6, leading=13,
        leftIndent=14, rightIndent=14,
        backColor=RED_BG, borderPad=6,
        borderColor=RED, borderWidth=1)

    s["callout_green"] = ParagraphStyle("callout_green",
        fontName="Helvetica-Oblique", fontSize=9, textColor=HexColor("#444444"),
        spaceBefore=4, spaceAfter=6, leading=13,
        leftIndent=14, rightIndent=14,
        backColor=GREEN_BG, borderPad=6,
        borderColor=GREEN, borderWidth=1)

    s["table_hdr"] = ParagraphStyle("table_hdr",
        fontName="Helvetica-Bold", fontSize=8.5, textColor=WHITE,
        alignment=TA_CENTER, leading=11)

    s["table_cell"] = ParagraphStyle("table_cell",
        fontName="Helvetica", fontSize=8.5, textColor=TEXT,
        leading=11)

    s["footer"] = ParagraphStyle("footer",
        fontName="Helvetica-Oblique", fontSize=7.5, textColor=MUTED,
        alignment=TA_CENTER)

    s["amount_big"] = ParagraphStyle("amount_big",
        fontName="Helvetica-Bold", fontSize=10, textColor=RED)

    s["amount_ok"] = ParagraphStyle("amount_ok",
        fontName="Helvetica-Bold", fontSize=10, textColor=GREEN)

    return s

ST = make_styles()

# ── Helpers ───────────────────────────────────────────────────────────────────

def H1(text):
    label = f'<font color="#0F3A75">▌</font> {text}'
    return Paragraph(label, ST["h1"])

def H2(text):
    return Paragraph(text, ST["h2"])

def Body(text):
    return Paragraph(text, ST["body"])

def Bullet(text):
    return Paragraph(f"• {text}", ST["bullet"])

def Callout(text, style="callout"):
    return Paragraph(f"ℹ  {text}", ST[style])

def Divider():
    return HRFlowable(width="100%", thickness=0.5, color=BORDER,
                      spaceAfter=6, spaceBefore=6)

def Spacer_(h=6):
    return Spacer(1, h)

def kv_row(label, value, val_color=None):
    """Single key-value paragraph line."""
    vc = val_color or TEXT
    hex_vc = vc.hexval() if hasattr(vc, 'hexval') else str(vc)
    return Paragraph(
        f'<font name="Helvetica-Bold">{label}</font>  '
        f'<font color="{hex_vc}">{value}</font>',
        ST["body"]
    )


def make_table(headers, data, col_widths, zebra=True, hdr_color=None):
    hdr_bg = hdr_color or TABLE_HDR
    all_rows = []

    # Header row
    hdr_cells = [Paragraph(h, ST["table_hdr"]) for h in headers]
    all_rows.append(hdr_cells)

    # Data rows
    for row in data:
        cells = []
        for val in row:
            text = str(val)
            if text in ("CRÍTICO", "🔴 CRÍTICO"):
                p = Paragraph(f'<font color="#C0392B"><b>{text}</b></font>', ST["table_cell"])
            elif text in ("ALTO", "🟠 ALTO"):
                p = Paragraph(f'<font color="#D36800"><b>{text}</b></font>', ST["table_cell"])
            elif text in ("MEDIO", "🟡 MEDIO"):
                p = Paragraph(f'<font color="#B8860B"><b>{text}</b></font>', ST["table_cell"])
            elif text.startswith("✅") or text == "0 — Fix confirmado ✅":
                p = Paragraph(f'<font color="#1A7A40">{text}</font>', ST["table_cell"])
            else:
                p = Paragraph(text, ST["table_cell"])
            cells.append(p)
        all_rows.append(cells)

    t = Table(all_rows, colWidths=col_widths, repeatRows=1)

    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), hdr_bg),
        ("TEXTCOLOR",  (0, 0), (-1, 0), WHITE),
        ("ALIGN",      (0, 0), (-1, 0), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, 0), 8.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [TABLE_ALT, WHITE] if zebra else [WHITE]),
        ("GRID",       (0, 0), (-1, -1), 0.4, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
    ]
    t.setStyle(TableStyle(style_cmds))
    return t


# ── Page template ─────────────────────────────────────────────────────────────

def on_first_page(canvas, doc):
    canvas.saveState()
    # Top accent bar
    canvas.setFillColor(ACCENT_BLUE)
    canvas.rect(0, H - 0.18*inch, W, 0.18*inch, fill=1, stroke=0)
    # Bottom bar
    canvas.setFillColor(HexColor("#F0F4FA"))
    canvas.rect(0, 0, W, 0.4*inch, fill=1, stroke=0)
    canvas.setFont("Helvetica-Oblique", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawCentredString(W/2, 0.15*inch,
        "Reporte generado con script de reconciliación automatizado · Blue Phoenix Lab · Mayo 2026")
    canvas.restoreState()


def on_later_pages(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(ACCENT_BLUE)
    canvas.rect(0, H - 0.12*inch, W, 0.12*inch, fill=1, stroke=0)
    canvas.setFillColor(HexColor("#F0F4FA"))
    canvas.rect(0, 0, W, 0.35*inch, fill=1, stroke=0)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(inch*0.75, 0.13*inch, "Devotio Rewards — Reporte de Validación Stripe · Blue Phoenix Lab")
    canvas.drawRightString(W - inch*0.75, 0.13*inch, f"Pág. {doc.page}")
    canvas.restoreState()


# ── Content builder ───────────────────────────────────────────────────────────

def build_pdf(out_path):
    doc = SimpleDocTemplate(
        out_path,
        pagesize=LETTER,
        leftMargin=0.75*inch, rightMargin=0.75*inch,
        topMargin=0.55*inch, bottomMargin=0.6*inch,
    )

    story = []
    TW = W - 1.5*inch  # text width

    # ── Cover ─────────────────────────────────────────────────────────────────
    story.append(Spacer_(20))
    story.append(Paragraph("REPORTE DE VALIDACIÓN", ST["cover_title"]))
    story.append(Paragraph("Sistema Stripe Automatizado — Devotio Rewards", ST["cover_sub"]))
    story.append(Paragraph(
        "Período: 12 de mayo – 18 de mayo, 2026  &nbsp;&nbsp;|&nbsp;&nbsp;"
        "Emitido: 19 de mayo, 2026  &nbsp;&nbsp;|&nbsp;&nbsp;  Blue Phoenix Lab",
        ST["cover_meta"]))
    story.append(Divider())
    story.append(Spacer_(8))

    # ── 1. Resumen Ejecutivo ──────────────────────────────────────────────────
    story.append(H1("1. Resumen Ejecutivo"))
    story.append(Body(
        "Validación cruzada entre el reporte manual exportado de Stripe Dashboard "
        "(unified_payments 8) y el reporte automático generado por N8N "
        "(Devotio_financial_report). El análisis cubre el período del "
        "<b>12 al 18 de mayo de 2026</b>."
    ))
    story.append(Spacer_(4))

    # Summary KPI table
    kpi_data = [
        ["Total transacciones en Stripe (manual)", "96"],
        ["Transacciones exitosas (Paid)", "53 — $4,738.30"],
        ["Transacciones fallidas (Failed)", "42"],
        ["Match por Invoice ID", "71"],
        ["Match secundario (payment_intent)", "8"],
        ["Cobertura del sistema", "70.6%"],
        ["Monto matcheado correctamente", "$3,346.20"],
        ["Pagos exitosos SIN capturar", "16 — $1,392.10"],
        ["Bug de doble conteo (invoice.paid)", "0 — Fix confirmado ✅"],
    ]
    story.append(make_table(
        ["Métrica", "Valor"],
        kpi_data,
        [TW*0.62, TW*0.38],
    ))

    story.append(Callout(
        "Cobertura de 70.6% — menor a la semana anterior (89.8%). "
        "Se detectan 16 pagos exitosos no registrados por el sistema automático. "
        "El análisis confirma que NO son caídas de N8N sino fallos selectivos "
        "de entrega de webhook. Ver Sección 3 para el diagnóstico detallado.",
        style="callout"
    ))

    story.append(Divider())

    # ── 2. Estado del Sistema ─────────────────────────────────────────────────
    story.append(H1("2. Estado del Sistema Automatizado"))

    story.append(H2("✅  Funcionando correctamente"))
    for txt in [
        "<b>Fix de doble conteo (Bug #1):</b> Confirmado — 0 eventos invoice.paid duplicados. Corrección estable.",
        "<b>Match principal:</b> 71 transacciones matcheadas por Invoice ID.",
        "<b>Match secundario:</b> 8 transacciones adicionales por email + monto + timestamp.",
        "<b>Eventos activos:</b> Los 9 tipos configurados continúan disparando correctamente.",
    ]:
        story.append(Bullet(txt))

    story.append(Spacer_(6))
    story.append(H2("⚠️  Gap detectado — Mayo 17"))
    story.append(Body(
        "El sistema registró un período de inactividad el <b>17 de mayo de 01:50 UTC → 17:23 UTC (15.5 horas)</b>. "
        "Este gap NO afecta directamente los 16 pagos sin capturar de este reporte "
        "(todos ocurrieron el 12–15 y 18 de mayo), pero indica una posible suspensión o "
        "reinicio del workflow en N8N. Requiere verificación."
    ))

    story.append(Spacer_(6))
    story.append(H2("⚠️  Nuevos gaps históricos detectados"))
    gap_data = [
        ["May 7, 23:47 UTC", "May 8, 13:18 UTC", "13.5 h", "Nuevo"],
        ["May 9, 22:18 UTC", "May 10, 16:25 UTC", "18.1 h", "Nuevo"],
        ["May 17, 01:50 UTC", "May 17, 17:23 UTC", "15.5 h", "Nuevo — en período de reporte"],
        ["Abr 22, 01:09 UTC", "Abr 23, 10:33 UTC", "33.4 h", "Previo — suspensión N8N"],
    ]
    story.append(make_table(
        ["Inicio", "Fin", "Duración", "Estado"],
        gap_data,
        [TW*0.27, TW*0.27, TW*0.16, TW*0.30],
    ))

    story.append(Divider())

    # ── 3. Diagnóstico: Pagos No Capturados ───────────────────────────────────
    story.append(H1("3. Diagnóstico — Pagos Exitosos Sin Capturar"))

    story.append(Body(
        "Los 16 pagos missing se clasifican en dos patrones. En ambos casos "
        "N8N estaba <b>activo</b> al momento del cobro — no es una caída general."
    ))

    story.append(H2("Patrón A — 3 casos: invoice.payment_succeeded no registrado"))
    story.append(Body(
        "N8N <b>sí capturó</b> el evento <i>payment_intent.succeeded</i> para estos pagos "
        "pero no el <i>invoice.payment_succeeded</i>. Ambos webhooks llegan del mismo cobro. "
        "Probable causa: procesamiento concurrente en N8N — cuando dos webhooks llegan "
        "casi simultáneamente, uno puede ser descartado internamente."
    ))
    pat_a = [
        ["esalcedo@venite.com.gt",           "$135.00", "May 14, 18:16 UTC", "PI capturado, invoice missing"],
        ["compras@charlesbbq.com",           "$84.75",  "May 15, 03:13 UTC", "PI capturado, invoice missing"],
        ["bryan.andresloria@gmail.com",      "$75.00",  "May 15, 19:44 UTC", "PI capturado, invoice missing"],
    ]
    story.append(make_table(
        ["Cliente", "Monto", "Fecha/Hora", "Diagnóstico"],
        pat_a,
        [TW*0.32, TW*0.12, TW*0.24, TW*0.32],
    ))

    story.append(Spacer_(8))
    story.append(H2("Patrón B — 13 casos: Sin traza alguna en el sistema"))
    story.append(Body(
        "N8N no registró <b>ningún evento</b> de estos clientes en el momento del cobro. "
        "El sistema estaba activo minutos antes y después, descartando una caída general. "
        "Causa probable: el endpoint de N8N recibió el webhook de Stripe pero el workflow "
        "falló silenciosamente (error interno no notificado, timeout del nodo Google Sheets, "
        "o rechazo de webhook sin reintento registrado)."
    ))
    pat_b = [
        ["edgarmontenegro@m2co.com.gt",         "$135.00", "May 12, 17:53", "in_1TWKUmFNzeVGXAdU0kX3h6tZ"],
        ["estevedelpinal@live.com",              "$75.00",  "May 12, 18:11", "in_1TWJowFNzeVGXAdUiZp1JQ9C"],
        ["rramirez199104@gmail.com",             "$75.00",  "May 12, 18:52", "in_1TWKT1FNzeVGXAdUIXceRh66"],
        ["mfratti@goworkgt.com",                 "$75.00",  "May 12, 19:02", "in_1TWKcaFNzeVGXAdUzSwUYsXZ"],
        ["leticiavillamayor@raiobemba.com",      "$75.00",  "May 12, 22:09", "in_1TWNYFFNzeVGXAdULJDaqnCm"],
        ["johanzcr@gmail.com",                   "$95.00",  "May 13, 19:57", "in_1TWhyVFNzeVGXAdUgNkAB1GX"],
        ["jsalazar@cafelunacr.com",              "$107.35", "May 13, 23:17", "in_1TWl4oFNzeVGXAdU5rDDZQDG"],
        ["anitaveleza@hotmail.com",              "$75.00",  "May 15, 13:04", "in_1TXKTIFNzeVGXAdUdYCkW7J6"],
        ["direccion.ejecutiva@laescalon.org",    "$75.00",  "May 15, 17:59", "in_1TXP4hFNzeVGXAdUMQ1DuQoM"],
        ["info@cincoht.com.ec",                  "$65.00",  "May 15, 22:09", "in_1TXSyCFNzeVGXAdUwe44LUsN"],
        ["veraguassportcity@gmail.com",          "$75.00",  "May 15, 23:18", "in_1TXU45FNzeVGXAdUx6rWgWXV"],
        ["rodrigolisiu7@gmail.com",              "$75.00",  "May 18, 16:51", "in_1TYUNrFNzeVGXAdUMfkqlzsJ"],
        ["mariangelmartinez@grupolucia.com",     "$95.00",  "May 18, 17:12", "in_1TYTldFNzeVGXAdUWuK19hGK"],
    ]
    story.append(make_table(
        ["Cliente", "Monto", "Fecha (UTC)", "Invoice ID"],
        pat_b,
        [TW*0.30, TW*0.11, TW*0.18, TW*0.41],
    ))

    story.append(Spacer_(6))
    p_total = Paragraph(
        '<font name="Helvetica">Total pendiente de backfill:  </font>'
        '<font name="Helvetica-Bold" color="#C0392B">16 transacciones — $1,392.10</font>',
        ST["body"])
    story.append(p_total)

    story.append(Callout(
        "ACCIÓN REQUERIDA: Verificar en Stripe Dashboard → Developers → Webhooks → "
        "[endpoint N8N] → Recent deliveries los Invoice IDs listados. "
        "Si aparecen con respuesta 2xx → el fallo es interno en N8N (revisar Execution History). "
        "Si aparecen con 5xx/timeout → N8N no respondió en ese momento. "
        "Una vez confirmados, insertar manualmente los 16 registros en el Google Sheet.",
        style="callout"
    ))

    story.append(Callout(
        "CLUSTER MAY 12 (5 pagos entre 17:53-22:09 UTC): Cuatro de los cinco "
        "ocurrieron dentro de una ventana de 1.8h donde N8N estaba activo capturando otros eventos. "
        "A las 19:02 UTC N8N procesó exitosamente otro invoice.payment_succeeded al mismo tiempo "
        "que falló en capturar in_1TWKcaFNzeVGXAdUzSwUYsXZ — apunta a un problema de "
        "concurrencia cuando múltiples webhooks llegan simultáneamente.",
        style="callout"
    ))

    story.append(Divider())

    # ── 4. Clientes en Riesgo ─────────────────────────────────────────────────
    story.append(H1("4. Clientes en Riesgo de Cancelación"))

    story.append(Body(
        "El sistema detecta clientes con intentos de cobro fallidos acumulados. "
        "Stripe cancela suscripciones al agotar su ventana de reintentos (~9 intentos). "
        "<b>Se agregaron 4 clientes nuevos al nivel CRÍTICO respecto a la semana anterior.</b>"
    ))

    story.append(H2("🔴  CRÍTICO — 9 intentos (13 clientes · cancelación inminente)"))
    crit_data = [
        ["drnelsonamador@gmail.com",         "$75.00",  "CRÍTICO"],
        ["amedina@veronavet.cl",             "$75.00",  "CRÍTICO"],
        ["gerverehgt020883@icloud.com",      "$85.00",  "CRÍTICO"],
        ["reneemendez01@gmail.com",          "$75.00",  "CRÍTICO"],
        ["renechavez11@gmail.com",           "$75.00",  "CRÍTICO"],
        ["marianols85@gmail.com",            "$75.00",  "CRÍTICO"],
        ["ipamepc2002@gmail.com",            "$75.00",  "CRÍTICO"],
        ["robertoe28@hotmail.com",           "$105.00", "CRÍTICO"],
        ["casco@beautyandthebutcher.com",    "$75.00",  "CRÍTICO"],
        ["moisesmontalvowa@gmail.com",       "$85.00",  "CRÍTICO ⚠ cobro doble previo"],
        ["felipe.rojas7131@gmail.com",       "$75.00",  "CRÍTICO (nuevo)"],
        ["admin@cmiconstructions.com",       "$85.00",  "CRÍTICO (nuevo)"],
        ["diegomadrinan@hotmail.com",        "$85.00",  "CRÍTICO (nuevo)"],
    ]
    story.append(make_table(
        ["Email", "Monto mensual", "Estado"],
        crit_data,
        [TW*0.56, TW*0.18, TW*0.26],
    ))
    story.append(Paragraph(
        '<font name="Helvetica">Valor mensual en riesgo CRÍTICO: </font>'
        '<font name="Helvetica-Bold" color="#C0392B">$1,010.00</font>',
        ST["body"]))

    story.append(Spacer_(8))
    story.append(H2("🟠  ALTO — 6–8 intentos (7 clientes)"))
    alto_data = [
        ["antonioebruno986@gmail.com",  "8", "$10.00"],
        ["poncekathy99@gmail.com",      "8", "$75.00  (nuevo)"],
        ["ivan@todofarma.cl",           "8", "$75.00  (nuevo)"],
        ["fragallegos@me.com",          "7", "$75.00"],
        ["raquelsteakhouse@gmail.com",  "7", "$105.00"],
        ["cferlu@hotmail.com",          "7", "$84.75  (nuevo)"],
        ["novasfacturae@gmail.com",     "6", "$75.00"],
    ]
    story.append(make_table(
        ["Email", "Intentos", "Monto mensual"],
        alto_data,
        [TW*0.56, TW*0.14, TW*0.30],
    ))
    story.append(Paragraph(
        '<font name="Helvetica">Valor mensual en riesgo ALTO: </font>'
        '<font name="Helvetica-Bold" color="#D36800">$499.75</font>',
        ST["body"]))

    story.append(Spacer_(6))
    story.append(Callout(
        "moisesmontalvowa@gmail.com escaló a CRÍTICO (9 intentos). "
        "Este cliente ya tenía cobros duplicados en la semana del 24 de abril. "
        "Se recomienda contacto directo inmediato para aclarar la situación.",
        style="callout_red"
    ))

    story.append(Divider())

    # ── 5. Cobros Dobles (historial) ──────────────────────────────────────────
    story.append(H1("5. Cobros Duplicados — Historial (Abr 24–28)"))
    story.append(Body(
        "Nota de contexto: Los cobros dobles detectados en semanas anteriores "
        "(semana del 24 de abril) <b>no aparecen en el reporte manual actual</b> "
        "porque se originaron antes del período cubierto. Se mantiene el registro "
        "en este reporte como referencia hasta confirmar su resolución."
    ))
    story.append(Callout(
        "Bug #1 (doble conteo por invoice.paid) — RESUELTO y confirmado por tercera semana consecutiva.",
        style="callout_green"
    ))
    dc_data = [
        ["moisesmontalvowa@gmail.com",       "$85 × 2", "6 minutos",  "2 subscription_update",   "Pendiente confirmación"],
        ["cristinamonge@chokolat.com.ec",    "$135 × 2","11.2 horas", "update + cycle",           "Pendiente confirmación"],
        ["nmoras12@hotmail.com",             "$75 × 2", "18.4 horas", "update + cycle",           "Pendiente confirmación"],
        ["sebastianbt@hotmail.es",           "$75 × 2", "5.1 horas",  "update + cycle",           "Pendiente confirmación"],
        ["admin@cmiconstructions.com",       "~$85 × 2","< 1 min",    "2 subscription_create",    "Pendiente confirmación"],
    ]
    story.append(make_table(
        ["Cliente", "Monto", "Diferencia", "Tipo", "Estado"],
        dc_data,
        [TW*0.28, TW*0.12, TW*0.13, TW*0.22, TW*0.25],
    ))

    story.append(Divider())

    # ── 6. Plan de Acciones ───────────────────────────────────────────────────
    story.append(H1("6. Plan de Acciones"))

    actions = [
        ["🔴 Inmediata", "Contactar los 13 clientes CRÍTICOS (9 intentos fallidos — cancelación inminente)", "Devotio Rewards"],
        ["🔴 Inmediata", "Verificar en Stripe Webhook Logs los 16 Invoice IDs sin capturar", "Blue Phoenix Lab"],
        ["🔴 Inmediata", "Backfill manual de 16 registros en Google Sheet ($1,392.10)", "Blue Phoenix Lab"],
        ["🟠 Esta semana", "Investigar gap del 17 de mayo (15.5h) — posible reinicio de workflow N8N", "Blue Phoenix Lab"],
        ["🟠 Esta semana", "Implementar error handler en N8N para notificar fallos silenciosos del workflow", "Blue Phoenix Lab"],
        ["🟠 Esta semana", "Agregar keep-alive / heartbeat para prevenir hibernación de N8N en horas nocturnas", "Blue Phoenix Lab"],
        ["🟠 Esta semana", "Investigar problema de concurrencia (cluster May 12 — 5 pagos en 1.8h no capturados)", "Blue Phoenix Lab"],
        ["🟡 Próx. semanas", "Verificar resolución de cobros dobles de abril (solicitar confirmación de Stripe)", "Devotio Rewards"],
        ["🟡 Próx. semanas", "Implementar enriquecimiento automático de subscription.deleted con email del cliente", "Blue Phoenix Lab"],
    ]
    story.append(make_table(
        ["Prioridad", "Acción", "Responsable"],
        actions,
        [TW*0.20, TW*0.56, TW*0.24],
    ))

    story.append(Spacer_(12))
    story.append(Paragraph(
        "Reporte generado con script de reconciliación automatizado · Blue Phoenix Lab · Mayo 2026",
        ST["footer"]))

    # ── Build ─────────────────────────────────────────────────────────────────
    doc.build(story, onFirstPage=on_first_page, onLaterPages=on_later_pages)
    print(f"✅  PDF guardado: {out_path}")


if __name__ == "__main__":
    out = "/Users/ignaciotou/Devotio_Stripe/Validation Stripe May 19/Devotio_Reporte_Validacion_May12-18_2026.pdf"
    build_pdf(out)
