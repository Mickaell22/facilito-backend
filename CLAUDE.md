# CLAUDE.md — Facilito Backend

## Comandos esenciales

```bash
# Activar entorno virtual (siempre antes de cualquier comando)
source venv/bin/activate

# Servidor de desarrollo
python manage.py runserver

# Migraciones (después de cambiar modelos)
python manage.py makemigrations <app>
python manage.py migrate

# Seed de datos de prueba (requiere negocio "Cevichería El Marino" existente)
# Re-ejecutable: borra ventas/movimientos previos del negocio y los recrea
# distribuidos en los últimos 3 días (compras, consumos, mermas y ventas).
python manage.py seed

# Verificar configuración
python manage.py check

# Tests (lógica pura del chat: parseo LLM y validación de entrada)
python manage.py test apps.chat

# Crear superusuario para el admin
python manage.py createsuperuser
```

## Deploy en Railway

- **URL producción:** `https://web-production-8e7ef.up.railway.app`
- **Proyecto Railway:** `Facilito` (ID `109ea15a-2e38-49e7-ae65-0ea71795886d`)
- **Servicio web:** `web` | **BD:** `Postgres` (servicio `Postgres` con volumen persistente)
- **Variables configuradas:** `SECRET_KEY`, `DEBUG=False`, `DATABASE_URL=${{Postgres.DATABASE_URL}}`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `DEEPSEEK_API_KEY` (chat de texto). **No configuradas aún:** `OPENAI_API_KEY` (audio/Whisper) ni `ANTHROPIC_API_KEY` (foto/visión).
- Driver de BD: `psycopg[binary]>=3.1` (no `psycopg2-binary` — incompatible con Python 3.13 en Railway)
- `httpx==0.27.2` fijado en `requirements.txt` (lo usa el chat para llamar a DeepSeek; pin compatible con los SDK de anthropic/openai).

### Ejecutar comandos contra la BD de Railway

```bash
source venv/bin/activate
DATABASE_URL="postgresql://postgres:<PGPASSWORD>@zephyr.proxy.rlwy.net:34010/railway" python manage.py <comando>
```

> `PGPASSWORD` se obtiene de Railway → proyecto Facilito → servicio Postgres → Variables.

### Credenciales de prueba (Railway)

- **Email:** `dueno@marino.com` | **Contraseña:** `12345678`
- **Negocio:** Cevichería El Marino

## Base de datos local

PostgreSQL local con usuario de app de mínimos privilegios:
- **Host:** localhost:5432
- **BD:** `ecuainventario`
- **Usuario:** `ecuainventario_app` / `Ecua2026!App`
- Configurado en `.env` vía `DATABASE_URL`

El `.env` no está en git. Copiar `.env.example` y completar `DEEPSEEK_API_KEY` (chat de texto) y, opcionalmente, `OPENAI_API_KEY` (audio) y `ANTHROPIC_API_KEY` (foto/visión) para usar el Chat IA.

**`DATABASE_URL` es obligatorio** — si no está seteada el servidor falla explícito (el fallback a SQLite fue eliminado intencionalmente).

## Arquitectura

SaaS multitenancy para negocios gastronómicos. Cada negocio es un tenant aislado por FK — **no se usan schemas de PostgreSQL** (decisión de MVP; migrar a `django-tenants` cuando escale).

### Patrón de multitenancy (crítico)

Todo modelo de negocio hereda de `TenantModel` (`apps/core/models.py`):

```python
class TenantModel(models.Model):
    negocio = ForeignKey('negocios.Negocio', on_delete=CASCADE)
    objects = TenantManager()   # .for_tenant(negocio) → QuerySet filtrado
    class Meta:
        abstract = True
```

Todo ViewSet de negocio hereda `TenantViewSetMixin` (`apps/core/views.py`) **antes** de `ModelViewSet`. Este mixin sobreescribe `get_queryset()` para filtrar por `request.user.negocio`. **Nunca omitir este mixin en ViewSets de datos de negocio.**

### Apps y responsabilidades

| App | Contenido |
|---|---|
| `apps/core` | `TenantModel`, `TenantViewSetMixin` — sin URLs ni migraciones propias |
| `apps/autenticacion` | Custom `Usuario` (email como USERNAME_FIELD), JWT registro/login/perfil/cambiar-password |
| `apps/negocios` | Modelo `Negocio` (tenant raíz), endpoint GET/PATCH de configuración |
| `apps/inventario` | `Producto` (insumo\|plato) + `Movimiento` (entradas/salidas de insumos con campo `motivo`) |
| `apps/proveedores` | `Proveedor` con soft-delete (`activo=False`) |
| `apps/ventas` | `Venta` + `DetalleVenta` — ventas de platos a clientes, separadas de movimientos |
| `apps/dashboard` | Vista agregada: ventas del día, alertas de stock bajo, últimos movimientos |
| `apps/chat` | Chat IA: DeepSeek (texto, temporal) + Whisper (audio) + Claude (foto); toda la lógica en `services.py` |

### Modelo de datos resumido

```
Negocio
├── Usuario (AUTH_USER_MODEL)
├── Proveedor
├── Producto (categoria: insumo|plato)
│   └── Movimiento (solo para insumos — motivo: compra|ajuste|consumo|merma)
└── Venta
    └── DetalleVenta (snapshot de precio_unitario al momento de la venta)
```

**Importante:** `Movimiento` y `Venta` son entidades distintas. Los movimientos trackean stock de insumos; las ventas registran revenue de platos. El dashboard suma `Venta.total` (no movimientos) para los ingresos del día, y solo cuenta como gastos los movimientos con `motivo='compra'` (los ajustes de inventario NO son gastos).

### Chat IA (`apps/chat/services.py`)

Flujo: texto/audio/foto → propuesta JSON → usuario confirma → `confirmar_accion()` escribe a BD.

- **LLM (texto):** DeepSeek `deepseek-chat` vía API compatible OpenAI, llamada directa con `httpx` (no el SDK de OpenAI, que daba conflicto de versión `proxies`) — timeout 30s, `max_tokens=1024` y modo JSON nativo (`response_format={'type':'json_object'}`). **Temporal, para pruebas.** Requiere `DEEPSEEK_API_KEY`.
- **Audio:** Whisper (`whisper-1`) vía OpenAI SDK — timeout 60s. Requiere `OPENAI_API_KEY` (no configurada en Railway).
- **Foto:** Claude Haiku con visión (imagen en base64) vía Anthropic SDK — timeout 30s. Requiere `ANTHROPIC_API_KEY` (no configurada). DeepSeek no tiene visión.
- **El LLM nunca escribe directamente a la BD** — siempre pasa por `/api/chat/confirmar/`
- **Archivos permitidos:** audio (mp3/mp4/wav/ogg/webm, máx 10 MB), foto (jpg/png/webp, máx 5 MB)

Acciones que puede proponer el LLM: `registrar_movimiento`, `crear_producto`, `crear_proveedor`, `actualizar_producto`, `registrar_venta`, y `responder` (preguntas informativas — la respuesta va en `resumen`, no escribe a BD; el frontend la muestra como texto).

`_build_system_prompt()` inyecta en cada llamada el inventario del negocio (con costo, precio y margen de los platos, y marca de stock bajo) más `_resumen_negocio()` (ventas/gastos/utilidad de hoy y del mes en curso). El prompt instruye al LLM a pedir datos faltantes con `responder` en vez de proponer registros incompletos; además `_confirmar_crear_proveedor` y `_confirmar_crear_producto` validan que el nombre no esté vacío (`ValueError` → 400).

Todas las vistas del chat (`ChatMensajeView`, `ChatAudioView`, `ChatFotoView`, `ChatConfirmarView`) tienen `throttle_classes = [ChatRateThrottle]`.

**Validación en `confirmar_accion` (límite de confianza):** el body de
`/api/chat/confirmar/` viene del cliente, así que los handlers validan todo y
lanzan `ValueError` (→ 400) en vez de reventar con 500: `datos` debe ser dict,
`tipo` ∈ {entrada, salida}, cantidades > 0 (una "entrada" con cantidad negativa
descontaría stock saltándose el chequeo de stock insuficiente), decimales vía
`_a_decimal()` (convierte `None`/texto inválido en `ValueError`, no en
`InvalidOperation`), `precio_venta` se compara con `is not None` (0 es válido),
y los `None` en actualizar_producto se ignoran. Cubierto por `apps/chat/tests.py`.

**Guardas de API keys:** `procesar_mensaje` / `transcribir_audio` /
`procesar_foto` lanzan `ChatError` si falta su API key, y las tres vistas
(mensaje, audio, foto) devuelven `ChatError` como **503** con el mensaje — sin
clave configurada el cliente recibe "no está disponible todavía" en vez de un
500 opaco.

**Robustez del chat de texto:** `procesar_mensaje` usa el modo JSON nativo de DeepSeek y envuelve los fallos de red en `ChatError` (timeout, HTTP 4xx/5xx, conexión) con un mensaje seguro; `ChatMensajeView` los devuelve como **503** con ese mensaje y deja el `except Exception` genérico como 500. `_parse_llm_response` es tolerante: quita los fences de markdown, intenta `json.loads` y, si falla, extrae el primer bloque `{...}` (regex DOTALL); si no hay JSON válido lanza `ChatError`. Es compartido con `procesar_foto`, donde Claude a veces envuelve el JSON en markdown. El prompt de acciones está alineado con los handlers (`proveedor` opcional en `crear_producto`; `nombre` editable en `actualizar_producto`).

### Autenticación y seguridad

- JWT Bearer token en todos los endpoints excepto `registro/` y `login/`.
- Access token: 8h. Refresh: 30d con rotación automática.
- **Blacklist activa:** `BLACKLIST_AFTER_ROTATION=True` — los refresh tokens rotados quedan invalidados.
- `CambiarPasswordView` invalida **todos** los refresh tokens activos del usuario al cambiar contraseña. Si el blacklisteo falla, la excepción **se propaga** (500) — decisión deliberada: no se reporta éxito mientras queden sesiones viejas válidas.
- **Rate limiting:** anónimos 20/hora, usuarios 1000/día, chat 30/hora (`ChatRateThrottle`).
- Password mínimo: **8 caracteres**.

### Respuesta de login (`POST /api/auth/login/`)

```json
{
  "access": "...",
  "refresh": "...",
  "usuario": { "id", "email", "nombre", "apellido" },
  "negocio": { "id", "nombre", "tipo", "seed_color", "theme_mode" }
}
```

### Dashboard (`GET /api/dashboard/?fecha=YYYY-MM-DD`)

Sin parámetro = hoy. Respuesta:

```json
{
  "fecha": "YYYY-MM-DD",
  "ingresos": "Decimal",
  "gastos": "Decimal",
  "utilidad": "Decimal",
  "stock_critico": { "count": 0, "productos": [...] },
  "ultimos_movimientos": [
    { "id", "tipo", "motivo", "cantidad", "nota", "producto_nombre", "producto_unidad", "creado_por_nombre", "creado_en" }
  ]
}
```

- `stock_critico` filtra por `categoria='insumo'` (los platos no manejan stock, así que no cuentan como críticos aunque tengan `stock_actual=0`).
- `ultimos_movimientos` usa `MovimientoSerializer`, que incluye `producto_unidad` (`source='producto.unidad'`) para que el Home muestre la unidad junto a la cantidad.

### Registro (`POST /api/auth/registro/`)

Acepta `negocio_seed_color` (hex `#RRGGBB`, default `#1976D2`) para persistir el color de marca elegido en el onboarding del frontend.

## Convenciones

- PKs UUID en todos los modelos de negocio (`id = UUIDField(primary_key=True, default=uuid4)`)
- Soft-delete en `Proveedor` y `Producto`: `activo=False`, nunca `DELETE` real
- Stock se actualiza con `F('stock_actual') + delta` dentro de `transaction.atomic()` + `select_for_update()` para evitar race conditions
- `DetalleVenta.subtotal` se recalcula en `save()` — no confiar en el valor del cliente
- Variables de entorno siempre vía `python-decouple` (`config()`), nunca `os.environ`
- Errores de servicios externos (Claude, Whisper) se loggean con `logger.exception()` y devuelven mensaje genérico al cliente — nunca `str(e)` en producción
- Paginación activa: `DEFAULT_PAGINATION_CLASS = PageNumberPagination`, `PAGE_SIZE = 50`. Los endpoints de lista devuelven `{"count": N, "next": "...", "previous": "...", "results": [...]}`. El Flutter ya maneja este formato en `product_repository.dart` y `supplier_repository.dart`.
