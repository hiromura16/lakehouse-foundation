"""明示した固定版Vector本体で、HTTP JSONの配列形状を検証する。"""

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml


@unittest.skipUnless(os.environ.get("VECTOR_BINARY"), "VECTOR_BINARY未指定")
class VectorHttpWireTests(unittest.TestCase):
    """実際のHTTP送信をloopbackだけで受け、二重配列を検出する。"""

    def test_openobserve_receives_objects_in_one_array(self):
        """HTTP 200だけでは検知できない全件破棄を送信形状で防ぐ。"""
        bodies = []

        class Receiver(BaseHTTPRequestHandler):
            """合成リクエストだけを保持し、ヘッダーや本文を出力しない。"""

            def do_POST(self):
                """有限サイズのJSONを受けて200で応答する。"""
                size = int(self.headers["Content-Length"])
                if size > 65536:
                    self.send_error(413)
                    return
                bodies.append(json.loads(self.rfile.read(size)))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                """テスト入力・認証ヘッダーを標準出力へ出さない。"""

        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = Path(__file__).resolve().parents[1]
        config = yaml.safe_load(
            (root / "infra/monitoring/vector/openobserve.yaml").read_text()
        )
        sink = config["sinks"]["openobserve"]
        sink.pop("auth")
        sink.update(
            inputs=["fixture"],
            uri=f"http://127.0.0.1:{server.server_port}/ingest",
            compression="none",
            buffer={"type": "memory", "max_events": 100},
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                selected = {
                    "data_dir": directory,
                    "sources": {
                        "fixture": {
                            "type": "stdin",
                            "decoding": {"codec": "json"},
                        }
                    },
                    "sinks": {"openobserve": sink},
                }
                path = Path(directory) / "wire.yaml"
                path.write_text(yaml.safe_dump(selected))
                result = subprocess.run(
                    [os.environ["VECTOR_BINARY"], "--config", str(path)],
                    input='{"source_event_id":"one"}\n'
                    '{"source_event_id":"two"}\n',
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, "Vector送信に失敗")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertTrue(bodies, "HTTPリクエストが届いていない")
        records = []
        for body in bodies:
            self.assertIsInstance(body, list)
            self.assertTrue(all(isinstance(row, dict) for row in body))
            records.extend(body)
        self.assertEqual(
            sorted(row["source_event_id"] for row in records), ["one", "two"]
        )
