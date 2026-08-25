"""
WebSocket Consumer — push real-time telemetry (GPS, waypoint events) ke browser.

Browser connect ke ws://host/ws/telemetry, lalu menerima messages:
    {"event": "gps",            "payload": {"lat": ..., "lon": ..., "heading": ...}}
    {"event": "waypoint_added", "payload": {"lat": ..., "lon": ..., "label": ..., ...}}

Worker (GpsWorker, CaptureWorker) push ke channel layer group 'telemetry',
semua consumer yang subscribe akan menerima.
"""
import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer

logger = logging.getLogger(__name__)

GROUP_NAME = "telemetry"


class TelemetryConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)
        await self.accept()
        logger.debug("WebSocket client connected")

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)
        logger.debug("WebSocket client disconnected")

    async def receive(self, text_data=None, bytes_data=None):
        # Client → server messages tidak dibutuhkan untuk sekarang.
        # Bisa di-extend nanti untuk ack/heartbeat.
        pass

    async def telemetry_event(self, event):
        """Dipanggil saat ada message di group 'telemetry'."""
        await self.send(text_data=json.dumps({
            "event": event["event"],
            "payload": event["payload"],
        }))
