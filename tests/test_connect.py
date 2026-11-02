"""Connecting the internal APIs: secrets without environment variables, http_json presets, `api probe` / `api test`."""
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from django.core.management import call_command

from llmwiki import keystore
from llmwiki.conf import Endpoint, Pop3Account, Settings


def test_keystore_file_and_credential_manager(tmp_path):
    name = "llmwiki-test-secret"
    assert keystore.where(name) == ""
    keystore.put(name, "s3cr3t-file", to_file=True)
    assert keystore.get(name) == "s3cr3t-file" and keystore.where(name) == "data/secrets.yaml"
    if sys.platform == "win32":                       # the real Windows Credential Manager wins over the file
        keystore.put(name, "s3cr3t-win")
        try:
            assert keystore.get(name) == "s3cr3t-win" and keystore.where(name) == "credential manager"
            assert name in keystore.stored_names()
        finally:
            keystore.delete(name)
    keystore.delete(name)
    assert keystore.get(name, "none") == "none"
    assert keystore.fill("Bearer {secret:" + name + "}") == "Bearer "


def test_old_setting_names_still_work():
    ep = Endpoint.model_validate({"name": "a", "api_key_env": "OLD_NAME"})
    acc = Pop3Account.model_validate({"user": "u", "password_env": "PW"})
    assert ep.api_key_secret == "OLD_NAME" and acc.password_secret == "PW"
    s = Settings.model_validate(json.loads(Settings(pool={"endpoints": [ep]}).model_dump_json()))
    assert s.pool.endpoints[0].api_key_secret == "OLD_NAME"


class FakeAPI(BaseHTTPRequestHandler):
    """/v1/chat/completions = OpenAI style (needs the key), /chain/invoke = LangServe style; everything else 404."""
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.seen.append((self.path, dict(self.headers), body))
        if self.path == "/v1/chat/completions":
            if self.headers.get("Authorization") != "Bearer k-123":
                return self._send(401, {"error": "bad key"})
            txt = '{"ok": true, "word": "pong"}' if body["messages"][-1]["content"] else ""
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": txt}}]})
        if self.path == "/chain/invoke":
            if not isinstance(body.get("input"), list):
                return self._send(422, {"detail": "input must be a list of messages"})
            return self._send(200, {"output": {"type": "ai", "content": '{"ok": true, "word": "pong"}'}, "metadata": {}})
        self._send(404, {"detail": "not found"})

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def api():
    srv = HTTPServer(("127.0.0.1", 0), FakeAPI)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    keystore.put("fake-key", "k-123", to_file=True)
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    keystore.delete("fake-key")


def _run(*args) -> str:
    out = io.StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()


def test_http_json_presets_probe_and_test(api):
    from llmwiki.conf import get_settings, use_settings
    from llmwiki.llm.adapters import LlmError, make_adapter
    oa = Endpoint(name="oa", kind="http_json", base_url=api + "/v1", api_key_secret="fake-key", options={"preset": "openai", "proxy": "none"})
    assert "pong" in make_adapter(oa).complete("sys", "hi", 10, 50, {"title": "Pong", "type": "object"})
    path, headers, body = FakeAPI.seen[-1]
    assert body["response_format"]["type"] == "json_schema" and body["messages"][0] == {"role": "system", "content": "sys"}
    ls = Endpoint(name="ls", kind="http_json", base_url=api + "/chain", options={"preset": "langserve", "proxy": "none"})
    assert "pong" in make_adapter(ls).complete("sys", "hi", 10, 50)                     # answer found at output.content
    bad = Endpoint(name="bad", kind="http_json", base_url=api + "/v1", options={"preset": "openai", "proxy": "none"})
    with pytest.raises(LlmError, match="401"):                                         # no key → a 401 with a hint
        make_adapter(bad).complete("sys", "hi", 10, 50)
    # probe: finds the LangServe shape on /chain and prints a settings block
    out = _run("api", "probe", "--url", api + "/chain", "--proxy", "none", "--timeout", "10")
    assert "langserve" in out and "← works" in out and "kind: http_json" in out and "response_path: output.content" in out
    out = _run("api", "probe", "--url", api + "/v1", "--secret", "fake-key", "--proxy", "none", "--timeout", "10")
    assert "preset: openai" in out
    # test: a configured endpoint, request shown with the key masked, answer + JSON check
    s = get_settings().model_copy(deep=True)
    s.pool.endpoints = [oa]
    with use_settings(s):
        out = _run("api", "test", "oa")
    assert "HTTP 200" in out and "OK: endpoint oa works" in out and "k-123" not in out
