"""
Test WebSocket broadcast — simulate 2 client connect ke /ws/telemetry,
lalu trigger event via REST API, verify KEDUA client menerima broadcast.

Butuh: websockets library.
"""
import asyncio
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8769"
WS_URL = BASE.replace("http", "ws") + "/ws/telemetry/"


async def client(name, received_events, ready_event, stop_event):
    import websockets
    async with websockets.connect(WS_URL) as ws:
        ready_event.set()
        try:
            while not stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    data = json.loads(msg)
                    received_events[name].append(data["event"])
                except asyncio.TimeoutError:
                    continue
        except Exception as e:
            print(f"client {name} error: {e}")


def post(path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body else b""
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=5).read()


async def main():
    received = {"A": [], "B": []}
    ready_a = asyncio.Event()
    ready_b = asyncio.Event()
    stop = asyncio.Event()

    task_a = asyncio.create_task(client("A", received, ready_a, stop))
    task_b = asyncio.create_task(client("B", received, ready_b, stop))

    # Tunggu kedua client connect
    await asyncio.wait_for(ready_a.wait(), timeout=5)
    await asyncio.wait_for(ready_b.wait(), timeout=5)
    await asyncio.sleep(0.5)   # settle

    print("Both clients connected. Triggering events via REST…")

    # Trigger control update
    post("/api/control", {"yolo_enabled": False})
    await asyncio.sleep(0.4)

    # Trigger manual waypoint (butuh GPS fix — fake worker sudah emit)
    post("/api/waypoint")
    await asyncio.sleep(0.4)

    # Trigger clear
    post("/api/waypoints/clear")
    await asyncio.sleep(0.4)

    stop.set()
    await asyncio.sleep(0.2)
    task_a.cancel()
    task_b.cancel()

    print(f"\nClient A received: {received['A']}")
    print(f"Client B received: {received['B']}")

    # Verify: kedua client harus dapat control_updated, waypoint_added, waypoints_cleared
    required = {"control_updated", "waypoint_added", "waypoints_cleared"}
    a_ok = required.issubset(set(received["A"]))
    b_ok = required.issubset(set(received["B"]))

    print(f"\nClient A has all required events: {a_ok}")
    print(f"Client B has all required events: {b_ok}")
    if a_ok and b_ok:
        print("RESULT: PASS — both clients synced")
        return 0
    else:
        print(f"RESULT: FAIL — missing {required - set(received['A'])} (A), "
              f"{required - set(received['B'])} (B)")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
