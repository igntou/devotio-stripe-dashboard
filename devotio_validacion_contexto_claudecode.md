# CONTEXTO DEL PROYECTO: Devotio Rewards — Validación Stripe Webhooks → Google Sheets

## ¿Qué es este sistema?

Blue Phoenix Lab construyó una automatización en **N8N** que captura eventos del webhook de Stripe y los guarda en un **Google Sheet** como reporte financiero en tiempo real para el cliente Devotio Rewards.

El objetivo del sistema es reemplazar el proceso manual de exportar CSVs desde el Stripe Dashboard.

---

## Stack tecnológico

- **Stripe Webhooks** → dispara eventos de pago
- **N8N** → recibe los webhooks y procesa los datos
- **Google Sheets** → almacena los eventos como reporte financiero
- **Claude (este proyecto)** → valida los datos capturados vs el reporte manual de Stripe

---

## Los 10 event types configurados en el webhook

```
1. invoice.payment_succeeded     ← cobro exitoso de suscripción
2. invoice.paid                  ← factura marcada como pagada (ELIMINADO — ver bug #1)
3. invoice.payment_failed        ← fallo de cobro de suscripción
4. payment_intent.succeeded      ← pago exitoso directo (sin factura)
5. payment_intent.payment_failed ← fallo de pago directo
6. payment_intent.canceled       ← intento de pago cancelado
7. payment_intent.requires_action← pago pendiente 3DS/autenticación
8. charge.refunded               ← reembolso procesado
9. customer.subscription.updated ← cambio en suscripción
10. customer.subscription.deleted← cancelación de suscripción
```

---

## Metodología de validación (cómo cruzamos los datos)

### Fuentes de datos
- **Reporte manual**: exportado de Stripe Dashboard (CSV o .numbers) — fuente de verdad
- **Reporte auto**: Google Sheet generado por la automatización N8N

### Llave primaria de cruce
```
Invoice ID (campo "Invoice ID" en manual = campo "invoice_id" en auto)
Formato: in_1Xxxxx...
Tipo de join: FULL OUTER JOIN
```

### Cruce secundario (cuando no hay invoice_id)
Para eventos `payment_intent.succeeded` el auto NO guarda `invoice_id`. Se cruza por:
```
customer_email (exacto, case-insensitive) + amount_paid + timestamp (±2 min, ajustando UTC vs CST-6)
```

### Deduplicación del auto
`invoice.paid` e `invoice.payment_succeeded` disparan para el mismo cobro. Se descarta `invoice.paid` si ya existe `invoice.payment_succeeded` para el mismo `invoice_id`.

---

## Estructura del reporte manual (Stripe CSV export)

Columnas relevantes:
```
id                  → charge ID (ch_xxx o py_xxx)
Created date (UTC)  → timestamp en UTC
Amount              → monto en USD
Amount Refunded     → monto reembolsado (0 si no hay)
Status              → "Paid" | "Failed" | "Refunded"
Customer Email      → email del cliente
Invoice ID          → in_xxx (puede ser null para payment links directos)
Decline Reason      → razón de fallo (insufficient_funds, generic_decline, etc.)
Fee                 → comisión Stripe
```

---

## Estructura del reporte auto (Google Sheet / N8N)

Columnas relevantes:
```
event_id            → evt_xxx
created_at          → timestamp (UTC o CST-6, verificar zona horaria configurada)
event_type          → uno de los 10 tipos configurados
invoice_id          → in_xxx (NULL para payment_intent.succeeded sin factura)
customer_email      → email
customer_name       → nombre
amount_paid         → monto
amount_due          → monto adeudado (relevante para fallos)
status              → "paid" | "open" | "uncollectible"
decline_reason      → razón de fallo
attempt_count       → número de intento (relevante para detectar clientes en riesgo)
billing_reason      → "subscription_cycle" | "subscription_create" | etc.
subscription_id     → sub_xxx
```

---

## Clasificación de estados de match (lógica de validación)

```python
MATCH — Invoice ID
  → invoice_id existe en ambos reportes. Transacción correctamente capturada.

MATCH — via payment_intent
  → Sin invoice_id en auto, pero email + monto + timestamp coinciden.
  → Ocurre con payment_intent.succeeded cuando el pago no genera factura Stripe.
  → Capturado correctamente, solo requiere cruce secundario.

SOLO MANUAL — Gap N8N (backfill)
  → En manual pero no en auto, Y timestamp cae dentro de una ventana de inactividad de N8N.
  → No es bug del webhook — el sistema estaba apagado.
  → Requiere backfill manual en el Sheet.

SOLO MANUAL — Exitoso no capturado (post-gap)
  → Pago Paid en manual, NO en auto, Y el sistema ya estaba activo.
  → No aparece por invoice_id NI por email/amount.
  → REQUIERE INVESTIGACIÓN: revisar Stripe Webhook Logs para ese invoice_id.

SOLO MANUAL — Fallido sin invoice
  → Failed en manual sin invoice_id. 
  → Común en payment links directos o primeros intentos de suscripción nueva.
  → Generalmente normal, no es gap crítico.

SOLO AUTO — Sin factura manual
  → Evento en auto sin contraparte en manual.
  → Principalmente payment_intent.succeeded de payment links directos
    o eventos fuera del rango de fechas del reporte manual.
```

---

## Bugs encontrados y resueltos

### Bug #1 — Double counting (RESUELTO)
**Causa**: `invoice.paid` e `invoice.payment_succeeded` se disparaban para el mismo cobro. N8N guardaba ambos como registros separados inflando el total.

**Impacto semana 1**: ~$1,993.75 de inflación en el total reportado.

**Fix aplicado**: Se eliminó `invoice.paid` del webhook en Stripe y del branch en N8N.

**Verificación semana 2**: `invoice.paid = 0 eventos`. Fix confirmado.

---

### Bug #2 — Gap por suspensión de cuenta N8N (DOCUMENTADO, no es bug técnico)
**Causa**: La cuenta de N8N fue suspendida ~33 horas.

**Ventana ciega semana 1**: 15 abril 12:45 PM CST (hora de primera conexión a producción). Todo lo anterior es pre-conexión.

**Ventana ciega semana 2**: Apr 21 19:09 CST → Apr 23 04:33 CST (suspensión de cuenta N8N).

**Impacto**: Las transacciones en esas ventanas no serán reenviadas por Stripe automáticamente (Stripe agota retries en ~3 días).

**Manejo**: Se clasifican como "Gap N8N — backfill" y se insertan manualmente en el Sheet.

---

### Issue #3 — 8 exitosos post-gap sin capturar (PENDIENTE INVESTIGACIÓN)
**Síntoma**: 8 pagos Paid del 24-27 de abril, con N8N activo, que no aparecen en auto por ninguna vía.

**Hipótesis**: Timeouts puntuales del endpoint de N8N → Stripe marcó como error → retries agotados sin registrar.

**Siguiente paso**: Revisar Stripe Dashboard → Developers → Webhooks → endpoint → Webhook Logs → buscar los invoice IDs específicos.

**Invoice IDs a investigar**:
```
in_1TPnx9FNzeVGXAdU1ZoogoDm  (tarpons.parched-9s@icloud.com)
in_1TQBT2FNzeVGXAdU9WbFvEEM  (robertoe28@hotmail.com)
in_1TQGHmFNzeVGXAdU9cF4CXbz  (jbrito@funkids.cl)
in_1TQWoXFNzeVGXAdUby2vTXIA  (facturasrepuestoseuropeos@gmail.com)
in_1TQaudFNzeVGXAdUkNIsK2Ul  (marioav07@icloud.com)
in_1TQbnnFNzeVGXAdUWLylLcnz  (ggalarza@codicia.ec)
in_1TQdsLFNzeVGXAdUAu5SWD1A  (jnflores@gmail.com)
in_1TQrZ7FNzeVGXAdUUru4InWB  (mimascota@mimascota.com.gt)
```

---

### Issue #4 — customer.subscription.deleted sin datos de cliente (INFO)
**Síntoma**: El payload del evento `customer.subscription.deleted` no incluye `customer_email` ni `customer_name`.

**Workaround**: Cruzar el `subscription_id` en Stripe Dashboard para identificar al cliente.

**Posible mejora**: En N8N, al recibir este evento, hacer una llamada adicional a la API de Stripe (`GET /v1/subscriptions/{id}`) para enriquecer el registro con el email del cliente antes de guardarlo en el Sheet.

---

## Validación semana 1 — Resumen (Apr 14-20)

| Métrica | Valor |
|---|---|
| Manual total | 107 transacciones |
| Auto (real data, desde Apr 15 12:45 PM) | 151 eventos |
| MATCH invoice_id | 45 facturas pagadas |
| Gap pre-conexión (expected) | 13 transacciones |
| Gap real post-conexión (backfill) | 6 facturas pagadas — $504.95 |
| Double counting eliminado | $1,993.75 de inflación corregida |
| Event types activos de 10 | 7/10 (3 sin datos: charge.refunded, sub.updated, sub.deleted) |

---

## Validación semana 2 — Resumen (Apr 21-27)

| Métrica | Valor |
|---|---|
| Manual total | 145 transacciones |
| Manual Paid | 81 — $6,845.71 |
| Manual Failed | 63 |
| Manual Refunded | 1 |
| Auto eventos capturados | 180 |
| MATCH invoice_id | 99 |
| MATCH via payment_intent | 10 |
| Gap N8N backfill | 16 txns (15 Paid $1,372.05 + 1 Failed) |
| Exitosos no capturados post-gap | 8 — investigar |
| Double counting | 0 — fix confirmado |
| Primer charge.refunded capturado | ✓ Moises Montalvo $85.00 — Apr 24 |
| Suscripciones canceladas | 8 (sin email en payload) |

---

## Clientes con fallos repetidos (al 27 de abril)

Estos clientes tienen múltiples `attempt_count` acumulados. Stripe cancela la suscripción automáticamente cuando agota el retry window (~3 días, ~9 intentos):

```
Gerver Hernandez       Gerverehgt020883@icloud.com    9 intentos ← CRÍTICO
Roberto C Esquivel     robertoe28@hotmail.com          9 intentos ← CRÍTICO
IRMA PACHECO           ipamepc2002@gmail.com           7 intentos
JHORMAN MARTINEZ       casco@beautyandthebutcher.com   6 intentos
Francisco Gallegos     fragallegos@me.com              5 intentos
Moises Montalvo        moisesmontalvowa@gmail.com      4 intentos
Nova's Design Vega     novasfacturae@gmail.com         4 intentos
Daniel Jarquin         djarquinm@icloud.com            4 intentos
Denisse Calderon       denisse.calderond@hotmail.com   4 intentos
Cristina Monge         cristinamonge@chokolat.com.ec   3 intentos
```

**Razones de fallo más frecuentes** (del reporte manual):
```
insufficient_funds            35 casos
generic_decline               14 casos
partner_insufficient_funds     7 casos
do_not_honor                   3 casos
```

---

## Lo que queremos mejorar con Claude Code

### Validación 1 — Script de reconciliación automatizado
Actualmente el cruce manual→auto se hace en Python ad-hoc. Se necesita un script reutilizable que:
- Reciba ambos CSVs como input
- Aplique el join por invoice_id + cruce secundario por email/amount/timestamp
- Clasifique cada fila en los 6 estados de match definidos
- Detecte automáticamente ventanas de gap (períodos sin eventos en auto)
- Calcule métricas de cobertura
- Exporte un CSV unificado y un resumen en JSON

### Validación 2 — Detección de double counting
Verificar que `invoice.paid` no vuelva a aparecer en el auto. Si aparece, alertar con los invoice_ids afectados.

### Validación 3 — Clientes en riesgo por attempt_count
Del reporte auto, extraer clientes con `attempt_count >= 4` agrupados por email, ordenados por intentos, con el último `decline_reason` y `amount_due`.

### Validación 4 — Gap detection automático
Dado el reporte auto, identificar automáticamente ventanas de tiempo donde no hubo eventos (posibles cortes de N8N), definiendo un umbral configurable (ej: más de X horas sin ningún evento = posible gap).

### Validación 5 — Enriquecimiento de subscription.deleted
Hacer llamadas a la API de Stripe para los eventos `customer.subscription.deleted` sin email y enriquecer el registro con los datos del cliente.

---

## Archivos de referencia disponibles

```
unified_payments__4_.csv          → Reporte manual semana 1 (Apr 14-20)
unified_payments__4__2.csv        → Reporte manual semana 2 (Apr 21-27)
Devotio_financial_report_-_devotio_raw.csv      → Reporte auto semana 1 (incluye test data)
Devotio_financial_report_-_devotio_raw__1_.csv  → Reporte auto semana 2
```

**Nota importante**: El reporte auto semana 1 incluye eventos de prueba antes del 15 de abril. Los datos reales de producción inician el **15 de abril de 2026 a las 12:45 PM CST**.

---

## Zona horaria

- **Reporte manual (Stripe export)**: UTC
- **Reporte auto (N8N → Sheet)**: CST / UTC-6

Al comparar timestamps, siempre ajustar +6 horas al campo `created_at` del auto para comparar con el manual. Tolerancia aceptable para match: ±2 minutos (Stripe puede tener pequeñas diferencias entre el timestamp del charge y el del evento webhook).

---

## Próximas semanas

El proceso de validación se hará semanalmente:
1. Cliente exporta CSV de Stripe Dashboard (manual)
2. Se descarga el Google Sheet (auto)
3. Se corre el script de reconciliación
4. Se revisan gaps, no capturados, y clientes en riesgo
5. Se genera reporte de evidencia .docx

La meta es que el script sea lo suficientemente robusto para que la validación tome minutos, no horas.
