"""Privacy and model access: PII removal, which endpoint may see which stage, failover on blocked input,
structured output (schema) to the API, the CLI adapters (Gemini, Codex) and machine-specific settings."""
import json
import os
import sys
import textwrap

import pytest

from llmwiki import privacy
from llmwiki.conf import Endpoint, get_settings, use_settings
from llmwiki.llm import adapters
from llmwiki.llm.adapters import LlmError
from llmwiki.llm.pool import Pool, StageFailed
from llmwiki.models import LlmCall

PII_TEXT = ("Call me on +31 6 1234 5678 or 06-12345678, Korean mobile 010-1234-5678, tel: 020 555 1234. "
            "Mail jan.devries@nordline.example. IBAN NL91 ABNA 0417 1643 00. RRN 900101-1234567. "
            "Deliver to Keizersgracht 123, 1015 CJ Amsterdam or 서울특별시 강남구 테헤란로 152. "
            "PO 4500123456, delivery 80012345, container MSKU1234567 and dl-ct@corp-europe.example stay.")


def test_redact_removes_pii_but_keeps_reference_numbers():
    out, n = privacy.redact(PII_TEXT, keep_internal=True)
    for gone in ("1234 5678", "12345678", "010-1234-5678", "555 1234", "jan.devries", "ABNA", "900101-1234567", "Keizersgracht 123", "테헤란로 152"):
        assert gone not in out, gone
    for kept in ("4500123456", "80012345", "MSKU1234567", "dl-ct@corp-europe.example"):
        assert kept in out, kept
    assert "[phone]" in out and "[email]" in out and "[iban]" in out and "[address]" in out and n >= 8
    assert "dl-ct@corp-europe.example" not in privacy.redact(PII_TEXT, keep_internal=False)[0]


def settings_with(endpoints, **privacy_over):
    s = get_settings().model_copy(deep=True)
    s.pool.endpoints = endpoints
    s.pool.backoff_base_s = 1
    s.schedule.chat_idle_resume_s = 0
    for k, v in privacy_over.items():
        setattr(s.privacy, k, v)
    return s


def build(cap, scale):
    return "Return JSON.", "TASK: s1\n### [1] 2026-10-20 09:00 · A <a@x>\nSubject: hello\nPlease call +31 6 1234 5678. A decision was made.", []


def parse_ok(o):
    return (o, []) if isinstance(o, dict) and o.get("notes") else (None, ["need notes"])


def test_external_endpoint_only_gets_allowed_stages():
    gem = Endpoint(name="gemini", kind="mock", external=True, rpm=600, max_concurrent=2)
    with use_settings(settings_with([gem])):
        p = Pool()
        with pytest.raises(LlmError, match="external"):
            p.run_structured("s1", "t", build, parse_ok)            # email stage: never external on real mail
        assert p.eligible("review") == ["gemini"]                    # the wiki lint may go out (PII removed)
    with use_settings(settings_with([gem], test_data_only=True)):
        r = Pool().run_structured("s1", "t-test-data", build, parse_ok)
        assert r.endpoint == "gemini"
        call = LlmCall.objects.filter(unit="t-test-data").latest("id")
        assert "[phone]" in call.prompt and "1234 5678" not in call.prompt and call.redactions >= 1


def test_blocked_input_fails_over_to_the_next_endpoint():
    blocker = Endpoint(name="api-b", kind="chaos", rpm=600, max_concurrent=2, priority=0,
                       options={"inner": "mock", "p429": 0, "ptimeout": 0, "p5xx": 0, "pbroken": 0, "pempty": 0, "pblock": 1.0})
    # the 180 s API takes only small stages normally, and anything as the failover target
    other = Endpoint(name="api-a", kind="mock", rpm=600, max_concurrent=2, priority=1, stages=["s4"], fallback_stages=["*"])
    with use_settings(settings_with([blocker, other])):
        assert Pool().eligible("s1") == ["api-b"]
        r = Pool().run_structured("s1", "failover", build, parse_ok)
        assert r.endpoint == "api-a"
        st = list(LlmCall.objects.filter(unit="failover").order_by("id").values_list("endpoint", "status"))
        assert st == [("api-b", "blocked"), ("api-a", "ok")]
        from llmwiki.api import refusals
        from llmwiki.models import Event
        assert Event.objects.filter(kind="refusal", message__contains="api-b refused s1").exists()   # every refusal is logged
        rf = refusals()
        assert rf["totals"]["api-b"] >= 1 and any(r["then"] == {"endpoint": "api-a", "status": "ok"} for r in rf["recent"] if r["unit"] == "failover")
    with use_settings(settings_with([blocker])):
        with pytest.raises(StageFailed, match="blocked"):
            Pool().run_structured("s1", "failover-alone", build, parse_ok)


def test_schema_goes_to_the_company_adapter_and_outputs_are_redacted(tmp_path, monkeypatch):
    rec = tmp_path / "seen.json"
    ad = tmp_path / "company_adapter.py"
    ad.write_text(textwrap.dedent(f"""
        import json
        def call(system, prompt, timeout, endpoint=None, schema=None):
            open(r"{rec}", "w").write(json.dumps({{"endpoint": endpoint, "schema": schema}}))
            # a LangChain with_structured_output backend returns an object, not text
            return {{"notes": [{{"email": 1, "note": "Asked to call +31 6 1234 5678 back", "kind": "request", "importance": "important"}}]}}
    """), encoding="utf-8")
    try:
        ep = Endpoint(name="api-180s", kind="custom", rpm=600, max_concurrent=2, structured_output=True, options={"module": str(ad)})
        with use_settings(settings_with([ep])):
            r = Pool().run_structured("s1", "schema", build, parse_ok)
        seen = json.loads(rec.read_text())
        assert seen["endpoint"] == "api-180s" and seen["schema"]["title"] == "Notes" and "notes" in seen["schema"]["properties"]
        assert r.obj["notes"][0]["note"] == "Asked to call [phone] back"      # privacy.redact_outputs
    finally:
        adapters.Custom._mods.clear()


def _stub(tmp_path, name: str, body: str) -> str:
    py = tmp_path / f"{name}_stub.py"
    py.write_text(textwrap.dedent(body), encoding="utf-8")
    cmd = tmp_path / f"{name}.cmd"
    cmd.write_text(f'@"{sys.executable}" "{py}" %*\r\n', encoding="utf-8")
    return str(cmd)


@pytest.mark.skipif(os.name != "nt", reason="the stubs are .cmd files")
def test_gemini_and_codex_cli_adapters(tmp_path, monkeypatch):
    gem = _stub(tmp_path, "gemini", """
        import json, os, sys
        system = open(os.environ["GEMINI_SYSTEM_MD"], encoding="utf-8").read()
        prompt = sys.stdin.read()
        assert "--approval-mode" in sys.argv and "plan" in sys.argv and "-o" in sys.argv
        print(json.dumps({"response": json.dumps({"notes": [{"email": 1, "note": "sys=" + str("Return JSON" in system) + " model=" + sys.argv[sys.argv.index("-m") + 1]}]}),
                          "stats": {}}))
    """)
    cod = _stub(tmp_path, "codex", """
        import json, sys
        args = sys.argv[1:]
        prompt = sys.stdin.read()
        assert args[0] == "exec" and "--sandbox" in args and args[args.index("--sandbox") + 1] == "read-only" and args[-1] == "-"
        schema = json.load(open(args[args.index("--output-schema") + 1])) if "--output-schema" in args else None
        out = args[args.index("-o") + 1]
        open(out, "w", encoding="utf-8").write(json.dumps({"notes": [{"email": 1, "note": "instructions=" + str("# Instructions" in prompt) + " schema=" + str(bool(schema))}]}))
    """)
    monkeypatch.setenv("LLMWIKI_GEMINI", gem)
    monkeypatch.setenv("LLMWIKI_CODEX", cod)
    eps = [Endpoint(name="gemini", kind="gemini_cli", model="gemini-2.5-pro", rpm=600, max_concurrent=1, timeout_s=60),
           Endpoint(name="codex", kind="codex_cli", model="gpt-5", rpm=600, max_concurrent=1, timeout_s=60, structured_output=True, priority=1)]
    with use_settings(settings_with(eps)):
        p = Pool()
        r = p.run_structured("s1", "gem", build, parse_ok)
        assert r.endpoint == "gemini" and r.obj["notes"][0]["note"] == "sys=True model=gemini-2.5-pro"
    with use_settings(settings_with(eps[1:])):
        r = Pool().run_structured("s1", "codex", build, parse_ok)
        assert r.obj["notes"][0]["note"] == "instructions=True schema=True"


def test_gemini_errors_are_classified():
    assert isinstance(adapters.classify("RESOURCE_EXHAUSTED: quota exceeded"), adapters.RateLimited)
    assert isinstance(adapters.classify("HTTP 400: request blocked by the content filter (PII)", 400), adapters.Blocked)
    assert isinstance(adapters.classify("HTTP 503: unavailable", 503), adapters.ServerError)


def test_machine_keys_in_settings_yaml_win_over_a_copied_database():
    from llmwiki.conf import invalidate, pinned_keys, save_settings
    assert "pool.endpoints" in pinned_keys()                 # the test settings.yaml sets the endpoints
    before = get_settings().model_copy(deep=True)
    other = before.model_copy(deep=True)
    other.pool.endpoints = [Endpoint(name="laptop-gemini", kind="gemini_cli", external=True)]
    other.reports.stories = 5
    save_settings(other, note="as if copied from the work laptop")
    try:
        invalidate()
        s = get_settings()
        assert [e.name for e in s.pool.endpoints] == ["mock"]   # this machine's endpoints
        assert s.reports.stories == 5                           # shared settings travel with the data
    finally:
        save_settings(before, note="restore")
