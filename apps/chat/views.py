import logging

from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from . import services

logger = logging.getLogger(__name__)

_AUDIO_MIME_ALLOWED = {'audio/mpeg', 'audio/mp4', 'audio/wav', 'audio/ogg', 'audio/webm', 'audio/x-m4a'}
_FOTO_MIME_ALLOWED = {'image/jpeg', 'image/png', 'image/webp'}
_AUDIO_MAX_BYTES = 10 * 1024 * 1024   # 10 MB
_FOTO_MAX_BYTES = 5 * 1024 * 1024     # 5 MB


class ChatRateThrottle(UserRateThrottle):
    scope = 'chat'


class ChatMensajeView(APIView):
    throttle_classes = [ChatRateThrottle]

    def post(self, request):
        texto = request.data.get('mensaje', '').strip()
        if not texto:
            return Response(
                {'error': 'El campo "mensaje" es requerido.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            propuesta = services.procesar_mensaje(texto, request.user.negocio)
            return Response(services._format_for_flutter(propuesta))
        except services.ChatError as e:
            logger.warning('ChatError en ChatMensajeView: %s', e)
            return Response(
                {'error': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            logger.exception('Error en ChatMensajeView')
            return Response(
                {'error': 'No se pudo procesar el mensaje. Intenta nuevamente.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ChatAudioView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [ChatRateThrottle]

    def post(self, request):
        audio = request.FILES.get('audio')
        if not audio:
            return Response(
                {'error': 'Se requiere un archivo de audio.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if audio.content_type not in _AUDIO_MIME_ALLOWED:
            return Response(
                {'error': f'Tipo de archivo no permitido. Usa: {", ".join(_AUDIO_MIME_ALLOWED)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if audio.size > _AUDIO_MAX_BYTES:
            return Response(
                {'error': 'El audio no puede superar 10 MB.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            texto = services.transcribir_audio(audio)
            propuesta = services.procesar_mensaje(texto, request.user.negocio)
            respuesta = services._format_for_flutter(propuesta)
            respuesta['transcripcion'] = texto
            return Response(respuesta)
        except services.ChatError as e:
            logger.warning('ChatError en ChatAudioView: %s', e)
            return Response(
                {'error': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            logger.exception('Error en ChatAudioView')
            return Response(
                {'error': 'No se pudo procesar el audio. Intenta nuevamente.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ChatFotoView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [ChatRateThrottle]

    def post(self, request):
        foto = request.FILES.get('foto')
        if not foto:
            return Response(
                {'error': 'Se requiere una imagen.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if foto.content_type not in _FOTO_MIME_ALLOWED:
            return Response(
                {'error': f'Tipo de imagen no permitido. Usa: {", ".join(_FOTO_MIME_ALLOWED)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if foto.size > _FOTO_MAX_BYTES:
            return Response(
                {'error': 'La imagen no puede superar 5 MB.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            propuesta = services.procesar_foto(foto, request.user.negocio)
            return Response(services._format_for_flutter(propuesta))
        except services.ChatError as e:
            logger.warning('ChatError en ChatFotoView: %s', e)
            return Response(
                {'error': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            logger.exception('Error en ChatFotoView')
            return Response(
                {'error': 'No se pudo procesar la imagen. Intenta nuevamente.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ChatConfirmarView(APIView):
    throttle_classes = [ChatRateThrottle]

    def post(self, request):
        accion = request.data.get('accion')
        datos = request.data.get('datos', {})
        if not accion:
            return Response(
                {'error': 'Se requiere el campo "accion".'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            resultado = services.confirmar_accion(
                {'accion': accion, 'datos': datos},
                request.user.negocio,
                request.user,
            )
            return Response(resultado, status=status.HTTP_201_CREATED)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception('Error en ChatConfirmarView')
            return Response(
                {'error': 'No se pudo confirmar la acción. Intenta nuevamente.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
