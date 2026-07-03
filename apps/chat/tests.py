from decimal import Decimal

from django.test import SimpleTestCase

from apps.chat.services import ChatError, _a_decimal, _parse_llm_response, confirmar_accion


class ADecimalTests(SimpleTestCase):
    def test_convierte_numeros_y_strings(self):
        self.assertEqual(_a_decimal(5, 'cantidad'), Decimal('5'))
        self.assertEqual(_a_decimal('2.5', 'cantidad'), Decimal('2.5'))
        self.assertEqual(_a_decimal(0, 'costo'), Decimal('0'))

    def test_none_es_valueerror(self):
        with self.assertRaises(ValueError):
            _a_decimal(None, 'cantidad')

    def test_texto_invalido_es_valueerror(self):
        with self.assertRaises(ValueError):
            _a_decimal('tres', 'cantidad')


class ParseLlmResponseTests(SimpleTestCase):
    def test_json_limpio(self):
        self.assertEqual(_parse_llm_response('{"accion": "responder"}'), {'accion': 'responder'})

    def test_json_con_fences_markdown(self):
        texto = '```json\n{"accion": "responder"}\n```'
        self.assertEqual(_parse_llm_response(texto), {'accion': 'responder'})

    def test_json_con_texto_extra(self):
        texto = 'Claro, aquí tienes: {"accion": "responder"} espero que sirva'
        self.assertEqual(_parse_llm_response(texto), {'accion': 'responder'})

    def test_sin_json_es_chaterror(self):
        with self.assertRaises(ChatError):
            _parse_llm_response('no hay json aquí')

    def test_json_roto_es_chaterror(self):
        with self.assertRaises(ChatError):
            _parse_llm_response('{"accion": rota sin cerrar')


class ConfirmarAccionTests(SimpleTestCase):
    def test_datos_no_dict_es_valueerror(self):
        with self.assertRaises(ValueError):
            confirmar_accion({'accion': 'registrar_venta', 'datos': 'texto'}, None, None)

    def test_accion_desconocida_es_valueerror(self):
        with self.assertRaises(ValueError):
            confirmar_accion({'accion': 'borrar_todo', 'datos': {}}, None, None)
