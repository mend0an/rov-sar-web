"""
ASGI config — entrypoint untuk Django + Channels (WebSocket).

Daphne (ASGI server) yang dipakai saat `python manage.py runserver` karena
sudah include di INSTALLED_APPS.
"""
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rov_sar_web.settings")

# Django ASGI app harus di-load sebelum import yang menyentuh model/apps
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter   # noqa: E402
from detection.routing import websocket_urlpatterns          # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": URLRouter(websocket_urlpatterns),
})
