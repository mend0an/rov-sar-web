"""WebSocket URL routing — di-import oleh asgi.py."""
from django.urls import re_path

from .consumers import TelemetryConsumer

websocket_urlpatterns = [
    re_path(r"^ws/telemetry/?$", TelemetryConsumer.as_asgi()),
]
