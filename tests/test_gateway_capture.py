import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx

from omnigent.gateway_capture import GatewayCaptureProxy, read_gateway_evidence


def test_multiple_requests_receive_distinct_ids_without_recording_secrets():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            seen.append(dict(self.headers))
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"type":"response.completed"}\n\ndata: [DONE]\n\n')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proxy = GatewayCaptureProxy(f"http://127.0.0.1:{server.server_port}/v1")
    capture = SimpleNamespace()
    proxy.bind(capture)
    try:
        for _ in range(2):
            response = httpx.post(
                proxy.base_url + "/responses",
                json={"model": "requested-alias"},
                headers={"authorization": "Bearer private-test-value"},
            )
            assert response.status_code == 200
            assert "[DONE]" in response.text
        assert len(capture.gateway_requests) == 2
        ids = [row["gateway_call_id"] for row in capture.gateway_requests]
        assert len(set(ids)) == 2
        for row, headers in zip(capture.gateway_requests, seen, strict=True):
            assert headers["x-request-id"] == row["gateway_call_id"]
            assert headers["x-omnigent-gateway-call-id"] == row["gateway_call_id"]
        assert "private-test-value" not in json.dumps(capture.gateway_requests)
        assert "requested-alias" not in json.dumps(capture.gateway_requests)
        proxy.bind(None)
        httpx.post(proxy.base_url + "/responses", json={})
        assert "x-omnigent-gateway-call-id" not in seen[-1]
    finally:
        proxy.close()
        server.shutdown()
        server.server_close()


def test_execution_absent_stays_null(tmp_path):
    assert read_gateway_evidence(tmp_path, str(uuid.uuid4())) is None
    assert read_gateway_evidence(tmp_path, "../../secret") is None


def test_gateway_projection_excludes_secrets_and_rejects_wrong_join(tmp_path):
    call_id = str(uuid.uuid4())
    spool = tmp_path / ".gateway"
    spool.mkdir()
    data = {
        "evidence_source": "omniroute_executor_fetch",
        "gateway_call_id": call_id,
        "omniroute_request_id": str(uuid.uuid4()),
        "authorization": "Bearer secret",
        "attempts": [
            {
                "attempt_id": str(uuid.uuid4()),
                "attempt_index": 0,
                "provider": "codex",
                "canonical_model": "actual-model",
                "safe_connection_id": str(uuid.uuid4()),
                "http_status": 200,
                "usage": {"input_tokens": 12, "api_key": "secret"},
                "headers": {"cookie": "secret"},
                "terminal_status": "succeeded",
            }
        ],
    }
    path = spool / (call_id + ".json")
    path.write_text(json.dumps(data))
    result = read_gateway_evidence(tmp_path, call_id)
    assert result["attempts"][0]["canonical_model"] == "actual-model"
    assert result["attempts"][0]["usage"] == {"input_tokens": 12}
    assert "secret" not in json.dumps(result)
    data["gateway_call_id"] = str(uuid.uuid4())
    path.write_text(json.dumps(data))
    assert read_gateway_evidence(tmp_path, call_id) is None
