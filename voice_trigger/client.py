"""One JSON command per TCP connection. Stop is never queued behind a take."""

import json
import socket


class AbletonClient(object):
    def __init__(self, host="127.0.0.1", port=9877, timeout=1.5):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)

    def send(self, command_type, params=None):
        payload = json.dumps({
            "type": command_type,
            "params": params or {},
        }).encode("utf-8")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((self.host, self.port))
            sock.sendall(payload)
            chunks = []
            while True:
                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                try:
                    return json.loads(b"".join(chunks).decode("utf-8"))
                except ValueError:
                    continue
            raw = b"".join(chunks)
            if not raw:
                raise IOError("no reply from Remote Script")
            return json.loads(raw.decode("utf-8"))
        finally:
            try:
                sock.close()
            except Exception:
                pass
