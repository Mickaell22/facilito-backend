import base64
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx
from anthropic import Anthropic
from django.conf import settings
from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Sum
from django.utils import timezone
from openai import OpenAI

from apps.inventario.models import Movimiento, Producto
from apps.proveedores.models import Proveedor
from apps.ventas.models import DetalleVenta, Venta

MODELO_HAIKU = 'claude-haiku-4-5-20251001'
MODELO_DEEPSEEK = 'deepseek-chat'
DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'


class ChatError(Exception):
    """Error del asistente con un mensaje seguro para mostrar al usuario."""


# ── Contexto y prompt ────────────────────────────────────────────────────────

def _resumen_negocio(negocio):
    hoy = timezone.now().date()
    inicio_mes = hoy.replace(day=1)

    def _ventas(desde):
        return (
            Venta.objects.for_tenant(negocio)
            .filter(fecha__date__gte=desde)
            .aggregate(t=Sum('total'))['t']
            or Decimal('0')
        )

    def _gastos(desde):
        return (
            Movimiento.objects.for_tenant(negocio)
            .filter(creado_en__date__gte=desde, tipo='entrada', motivo='compra')
            .aggregate(
                t=Sum(
                    ExpressionWrapper(
                        F('cantidad') * F('producto__costo'),
                        output_field=DecimalField(max_digits=14, decimal_places=4),
                    )
                )
            )['t']
            or Decimal('0')
        )

    ventas_hoy, gastos_hoy = _ventas(hoy), _gastos(hoy)
    ventas_mes, gastos_mes = _ventas(inicio_mes), _gastos(inicio_mes)
    return (
        f"Hoy ({hoy.strftime('%d/%m/%Y')}): ventas ${ventas_hoy:.2f}, "
        f"gastos ${gastos_hoy:.2f}, utilidad ${ventas_hoy - gastos_hoy:.2f}\n"
        f"Mes en curso: ventas ${ventas_mes:.2f}, "
        f"gastos ${gastos_mes:.2f}, utilidad ${ventas_mes - gastos_mes:.2f}"
    )


def _linea_producto(p):
    if p['categoria'] == 'plato':
        precio = p['precio_venta'] or Decimal('0')
        costo = p['costo'] or Decimal('0')
        return (
            f"- {p['nombre']} (plato, precio venta: ${precio:.2f}, "
            f"costo: ${costo:.2f}, margen: ${precio - costo:.2f})"
        )
    bajo = ' [STOCK BAJO]' if p['stock_actual'] <= p['stock_minimo'] else ''
    return (
        f"- {p['nombre']} (insumo, stock: {p['stock_actual']} {p['unidad']}, "
        f"mínimo: {p['stock_minimo']} {p['unidad']}){bajo}"
    )


def _build_system_prompt(negocio):
    productos = Producto.objects.for_tenant(negocio).filter(activo=True).values(
        'nombre', 'categoria', 'stock_actual', 'unidad', 'costo', 'precio_venta', 'stock_minimo'
    )
    proveedores = Proveedor.objects.for_tenant(negocio).filter(activo=True).values_list(
        'nombre', flat=True
    )

    productos_txt = '\n'.join(_linea_producto(p) for p in productos) or '(ninguno registrado)'
    proveedores_txt = '\n'.join(f'- {n}' for n in proveedores) or '(ninguno registrado)'

    return f"""Eres un asistente de gestión de inventario para negocios gastronómicos en Ecuador.
Responde SOLO con un JSON válido, sin texto adicional ni bloques de código markdown.

NEGOCIO: {negocio.nombre} ({negocio.tipo})
FECHA: {date.today().strftime('%d/%m/%Y')}

RESUMEN FINANCIERO:
{_resumen_negocio(negocio)}

PRODUCTOS REGISTRADOS:
{productos_txt}

PROVEEDORES REGISTRADOS:
{proveedores_txt}

ACCIONES DISPONIBLES:
1. registrar_movimiento — entradas o salidas de insumos
   datos: producto(nombre), tipo(entrada|salida), cantidad(número), motivo(compra|ajuste para entradas; consumo|merma para salidas), nota(opcional)

2. crear_producto — agregar nuevo producto o insumo
   datos: nombre, categoria(insumo|plato), costo, unidad, stock_minimo, precio_venta(solo platos), proveedor(nombre, opcional)

3. crear_proveedor — registrar nuevo proveedor
   datos: nombre, telefono(opcional), email(opcional), contacto(opcional)

4. actualizar_producto — modificar datos de un producto existente
   datos: producto(nombre actual), campos a actualizar(nombre, precio_venta, costo, stock_minimo, unidad)

5. registrar_venta — registrar venta de platos a clientes
   datos: detalles([{{"producto": nombre, "cantidad": número}}]), nota(opcional)

6. responder — para preguntas informativas (cómo va el negocio, cuánto se vendió o gastó, qué insumos están bajos, cuál plato es más rentable, etc.)
   No lleva datos. Pon la respuesta completa y concreta en lenguaje natural en el campo "resumen", usando los números reales del RESUMEN FINANCIERO y los datos de PRODUCTOS y PROVEEDORES de arriba.

FORMATO DE RESPUESTA:
{{
  "accion": "<accion>",
  "datos": {{ ... }},
  "resumen": "<descripción breve en español de lo que se hará, o la respuesta a la pregunta>"
}}

Usa "responder" cuando el usuario haga una pregunta en lugar de pedir registrar algo.
NUNCA devuelvas crear_proveedor, crear_producto, registrar_movimiento o registrar_venta con datos vacíos o incompletos. Si faltan datos esenciales (nombre del proveedor o producto, cantidad o producto de un movimiento, etc.), devuelve accion "responder" con la pregunta de qué falta en "resumen".
Ejemplo: si el usuario dice "agregar un proveedor" sin dar el nombre, responde {{"accion": "responder", "datos": {{}}, "resumen": "¿Cómo se llama el proveedor que deseas agregar?"}}.
Si el mensaje no corresponde a ninguna acción responde con accion "no_reconocido" y explica amablemente en "resumen" qué puede hacer el asistente."""


def _parse_llm_response(text):
    text = re.sub(r'```(?:json)?\s*', '', text).strip('`').strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        raise ChatError('No entendí la respuesta del asistente. Intenta reformular tu mensaje.')


# ── Procesamiento de entrada ─────────────────────────────────────────────────

def procesar_mensaje(texto, negocio):
    if not settings.DEEPSEEK_API_KEY:
        raise ChatError('El asistente de texto no está configurado en el servidor.')
    try:
        response = httpx.post(
            DEEPSEEK_URL,
            headers={'Authorization': f'Bearer {settings.DEEPSEEK_API_KEY}'},
            json={
                'model': MODELO_DEEPSEEK,
                'max_tokens': 1024,
                'response_format': {'type': 'json_object'},
                'messages': [
                    {'role': 'system', 'content': _build_system_prompt(negocio)},
                    {'role': 'user', 'content': texto},
                ],
            },
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.TimeoutException as e:
        raise ChatError('El asistente tardó demasiado en responder. Intenta nuevamente.') from e
    except httpx.HTTPStatusError as e:
        raise ChatError('El servicio de IA no está disponible en este momento.') from e
    except httpx.HTTPError as e:
        raise ChatError('No se pudo conectar con el asistente. Intenta nuevamente.') from e

    contenido = response.json()['choices'][0]['message']['content']
    return _parse_llm_response(contenido)


def transcribir_audio(audio_file):
    if not settings.OPENAI_API_KEY:
        raise ChatError('La transcripción de audio no está disponible todavía.')
    client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=60.0)
    transcript = client.audio.transcriptions.create(
        model='whisper-1',
        file=audio_file,
        language='es',
    )
    return transcript.text


def procesar_foto(imagen_file, negocio):
    if not settings.ANTHROPIC_API_KEY:
        raise ChatError('El análisis de fotos no está disponible todavía.')
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=30.0)
    image_data = base64.standard_b64encode(imagen_file.read()).decode('utf-8')
    media_type = imagen_file.content_type or 'image/jpeg'

    message = client.messages.create(
        model=MODELO_HAIKU,
        max_tokens=512,
        system=_build_system_prompt(negocio),
        messages=[
            {
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {
                            'type': 'base64',
                            'media_type': media_type,
                            'data': image_data,
                        },
                    },
                    {
                        'type': 'text',
                        'text': 'Analiza esta factura o recibo y extrae los datos para registrar el movimiento o venta correspondiente.',
                    },
                ],
            }
        ],
    )
    return _parse_llm_response(message.content[0].text)


# ── Búsquedas internas ───────────────────────────────────────────────────────

def _a_decimal(valor, campo):
    """Convierte input del cliente a Decimal; ValueError (→ 400) si es inválido."""
    if valor is None:
        raise ValueError(f'Falta el valor de "{campo}".')
    try:
        return Decimal(str(valor))
    except InvalidOperation as e:
        raise ValueError(f'El valor de "{campo}" no es un número válido.') from e


def _buscar_producto(nombre, negocio):
    if not nombre:
        raise ValueError('Indica el nombre del producto.')
    qs = Producto.objects.for_tenant(negocio).filter(activo=True)
    try:
        return qs.get(nombre__iexact=nombre)
    except Producto.DoesNotExist:
        matches = qs.filter(nombre__icontains=nombre)
        if matches.count() == 1:
            return matches.first()
        raise ValueError(f'Producto "{nombre}" no encontrado. Verifica el nombre en el inventario.')


def _buscar_proveedor(nombre, negocio):
    if not nombre:
        return None
    qs = Proveedor.objects.for_tenant(negocio).filter(activo=True)
    try:
        return qs.get(nombre__iexact=nombre)
    except Proveedor.DoesNotExist:
        matches = qs.filter(nombre__icontains=nombre)
        return matches.first()


# ── Confirmación de acciones ─────────────────────────────────────────────────

def confirmar_accion(propuesta, negocio, usuario):
    accion = propuesta.get('accion')
    datos = propuesta.get('datos')
    if not isinstance(datos, dict):
        raise ValueError('El campo "datos" debe ser un objeto con los datos de la acción.')

    handlers = {
        'registrar_movimiento': _confirmar_movimiento,
        'crear_producto': _confirmar_crear_producto,
        'crear_proveedor': _confirmar_crear_proveedor,
        'actualizar_producto': _confirmar_actualizar_producto,
        'registrar_venta': _confirmar_venta,
    }

    handler = handlers.get(accion)
    if not handler:
        raise ValueError(f'Acción no reconocida: "{accion}"')

    if accion in ('registrar_movimiento', 'registrar_venta'):
        return handler(datos, negocio, usuario)
    return handler(datos, negocio)


def _format_for_flutter(propuesta):
    return {
        'ok': True,
        'accion': propuesta.get('accion'),
        'resumen': propuesta.get('resumen', ''),
        'datos': propuesta.get('datos', {}),
    }


def _confirmar_movimiento(datos, negocio, usuario):
    producto = _buscar_producto(datos.get('producto'), negocio)
    tipo = datos.get('tipo')
    if tipo not in ('entrada', 'salida'):
        raise ValueError('El tipo de movimiento debe ser "entrada" o "salida".')
    cantidad = _a_decimal(datos.get('cantidad'), 'cantidad')
    if cantidad <= 0:
        raise ValueError('La cantidad debe ser mayor que cero.')
    nota = datos.get('nota') or ''
    motivo = datos.get('motivo', 'compra' if tipo == 'entrada' else 'consumo')
    delta = cantidad if tipo == 'entrada' else -cantidad

    with transaction.atomic():
        producto_lock = Producto.objects.select_for_update().get(pk=producto.pk)
        if tipo == 'salida' and producto_lock.stock_actual < cantidad:
            raise ValueError(
                f'Stock insuficiente. Disponible: {producto_lock.stock_actual} {producto_lock.unidad}'
            )
        movimiento = Movimiento.objects.create(
            negocio=negocio,
            producto=producto_lock,
            tipo=tipo,
            motivo=motivo,
            cantidad=cantidad,
            nota=nota,
            creado_por=usuario,
        )
        Producto.objects.filter(pk=producto_lock.pk).update(stock_actual=F('stock_actual') + delta)

    return {
        'ok': True,
        'tipo': 'movimiento',
        'id': str(movimiento.id),
        'detalle': f'{tipo.capitalize()} de {cantidad} {producto.unidad} de {producto.nombre} registrada.',
    }


def _confirmar_crear_producto(datos, negocio):
    nombre = (datos.get('nombre') or '').strip()
    if not nombre:
        raise ValueError('Indica al menos el nombre del producto.')
    proveedor = _buscar_proveedor(datos.get('proveedor'), negocio)

    producto = Producto.objects.create(
        negocio=negocio,
        nombre=nombre,
        categoria=datos.get('categoria', 'insumo'),
        costo=_a_decimal(datos.get('costo') or 0, 'costo'),
        unidad=datos.get('unidad', 'unidad'),
        stock_minimo=_a_decimal(datos.get('stock_minimo') or 0, 'stock_minimo'),
        precio_venta=(
            _a_decimal(datos['precio_venta'], 'precio_venta')
            if datos.get('precio_venta') is not None else None
        ),
        proveedor=proveedor,
    )
    return {
        'ok': True,
        'tipo': 'producto',
        'id': str(producto.id),
        'detalle': f'Producto "{producto.nombre}" creado correctamente.',
    }


def _confirmar_crear_proveedor(datos, negocio):
    nombre = (datos.get('nombre') or '').strip()
    if not nombre:
        raise ValueError('Indica al menos el nombre del proveedor.')
    proveedor = Proveedor.objects.create(
        negocio=negocio,
        nombre=nombre,
        telefono=datos.get('telefono') or '',
        email=datos.get('email') or '',
        contacto=datos.get('contacto') or '',
    )
    return {
        'ok': True,
        'tipo': 'proveedor',
        'id': str(proveedor.id),
        'detalle': f'Proveedor "{proveedor.nombre}" creado correctamente.',
    }


def _confirmar_actualizar_producto(datos, negocio):
    producto = _buscar_producto(datos.get('producto'), negocio)
    campos_decimales = {'precio_venta', 'costo', 'stock_minimo'}
    campos_permitidos = campos_decimales | {'unidad', 'nombre'}

    for campo, valor in datos.items():
        if campo == 'producto' or campo not in campos_permitidos or valor is None:
            continue
        if campo == 'nombre' and not str(valor).strip():
            raise ValueError('El nombre del producto no puede quedar vacío.')
        setattr(producto, campo, _a_decimal(valor, campo) if campo in campos_decimales else valor)

    producto.save()
    return {
        'ok': True,
        'tipo': 'producto',
        'id': str(producto.id),
        'detalle': f'Producto "{producto.nombre}" actualizado correctamente.',
    }


def _confirmar_venta(datos, negocio, usuario):
    detalles_data = datos.get('detalles')
    if not isinstance(detalles_data, list) or not detalles_data:
        raise ValueError('La venta requiere al menos un producto.')
    if not all(isinstance(item, dict) for item in detalles_data):
        raise ValueError('Cada detalle de la venta debe incluir producto y cantidad.')

    with transaction.atomic():
        total = Decimal('0')
        detalles_objs = []

        for item in detalles_data:
            producto = _buscar_producto(item.get('producto'), negocio)
            if producto.precio_venta is None:
                raise ValueError(f'"{producto.nombre}" no tiene precio de venta definido.')
            cantidad = _a_decimal(item.get('cantidad'), 'cantidad')
            if cantidad <= 0:
                raise ValueError('La cantidad de cada detalle debe ser mayor que cero.')
            subtotal = cantidad * producto.precio_venta
            total += subtotal
            detalles_objs.append(
                DetalleVenta(
                    producto=producto,
                    cantidad=cantidad,
                    precio_unitario=producto.precio_venta,
                    subtotal=subtotal,
                )
            )

        venta = Venta.objects.create(
            negocio=negocio,
            atendido_por=usuario,
            total=total,
            nota=datos.get('nota') or '',
        )
        for d in detalles_objs:
            d.venta = venta
        DetalleVenta.objects.bulk_create(detalles_objs)

    return {
        'ok': True,
        'tipo': 'venta',
        'id': str(venta.id),
        'detalle': f'Venta de ${total} registrada correctamente.',
    }
