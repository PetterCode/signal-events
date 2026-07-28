import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from signal_events import analysis, llm


def _fake_summary():
    threat = analysis.ThreatAssessment(level="yellow", score=6, reasons=["Reg.nr ABC123: Återkommer 2 gånger"])
    vehicle = analysis.RecurrenceGroup(
        label="Reg.nr ABC123", kind="plate", object_type=None,
        events=[analysis.EventRef(1, "10:00", "Norra grinden", "2026-01-01T10:00:00")],
        distinct_places={"Norra grinden"}, suspicious_hits=0, score=3,
        reasons=["Återkommer 2 gånger (registreringsnummer)"],
    )
    return analysis.Summary(
        total_events=5, period_label="7d", vehicle_groups=[vehicle],
        person_groups=[], other_groups=[], threat=threat,
    )


def test_build_prompt_includes_threat_and_groups():
    prompt = llm._build_prompt(_fake_summary(), site_name="Kvarn")
    assert "GUL" in prompt
    assert "Reg.nr ABC123" in prompt
    assert "Kvarn" in prompt
    assert "7d" in prompt


def _mock_response(payload: dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_generate_narrative_success():
    with patch("urllib.request.urlopen", return_value=_mock_response({"response": "  En lugn vecka.  "})):
        text = llm.generate_narrative(_fake_summary(), site_name="Kvarn")
    assert text == "En lugn vecka."


def test_generate_narrative_connection_error():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        with pytest.raises(llm.LLMError, match="Ollama"):
            llm.generate_narrative(_fake_summary(), site_name="Kvarn")


def test_generate_narrative_model_not_found_gives_specific_message():
    error = urllib.error.HTTPError(
        url="http://localhost:11434/api/generate", code=404, msg="Not Found",
        hdrs=None, fp=MagicMock(read=lambda: b'{"error":"model not found"}'),
    )
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(llm.LLMError, match="ollama pull"):
            llm.generate_narrative(_fake_summary(), site_name="Kvarn")


def test_generate_narrative_empty_response_raises():
    with patch("urllib.request.urlopen", return_value=_mock_response({"response": ""})):
        with pytest.raises(llm.LLMError):
            llm.generate_narrative(_fake_summary(), site_name="Kvarn")


def test_generate_narrative_invalid_json_raises():
    resp = MagicMock()
    resp.read.return_value = b"not json"
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=resp):
        with pytest.raises(llm.LLMError):
            llm.generate_narrative(_fake_summary(), site_name="Kvarn")


def test_generate_chat_reply_sends_context_as_system_message_and_returns_content():
    history = [{"role": "user", "content": "Har vi sett bilen förut?"}]
    with patch("urllib.request.urlopen", return_value=_mock_response(
        {"message": {"role": "assistant", "content": "  Ja, en gång tidigare.  "}}
    )) as mock_urlopen:
        text = llm.generate_chat_reply(history, context="Egna sparade händelser: inga.")

    assert text == "Ja, en gång tidigare."
    request = mock_urlopen.call_args[0][0]
    assert request.full_url.endswith("/api/chat")
    sent = json.loads(request.data.decode("utf-8"))
    assert sent["messages"][0]["role"] == "system"
    assert "Egna sparade händelser: inga." in sent["messages"][0]["content"]
    assert sent["messages"][1] == history[0]


def test_generate_chat_reply_connection_error():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        with pytest.raises(llm.LLMError, match="Ollama"):
            llm.generate_chat_reply([{"role": "user", "content": "Hej"}], context="")


def test_generate_chat_reply_empty_response_raises():
    with patch("urllib.request.urlopen", return_value=_mock_response({"message": {"content": ""}})):
        with pytest.raises(llm.LLMError):
            llm.generate_chat_reply([{"role": "user", "content": "Hej"}], context="")


def test_resolve_ollama_url_without_a_port_returns_config_default():
    from signal_events import config

    assert llm.resolve_ollama_url(None) == config.OLLAMA_URL
    assert llm.resolve_ollama_url("") == config.OLLAMA_URL


def test_resolve_ollama_url_splices_in_the_given_port():
    assert llm.resolve_ollama_url("11500") == "http://localhost:11500"


def test_generate_narrative_uses_the_resolved_base_url():
    with patch("urllib.request.urlopen", return_value=_mock_response({"response": "Text."})) as mock_urlopen:
        llm.generate_narrative(_fake_summary(), site_name="Kvarn", base_url="http://localhost:11500")

    request = mock_urlopen.call_args[0][0]
    assert request.full_url == "http://localhost:11500/api/generate"


def test_generate_chat_reply_uses_the_resolved_base_url():
    with patch("urllib.request.urlopen", return_value=_mock_response({"message": {"content": "Text."}})) as mock_urlopen:
        llm.generate_chat_reply(
            [{"role": "user", "content": "Hej"}], context="", base_url="http://localhost:11500",
        )

    request = mock_urlopen.call_args[0][0]
    assert request.full_url == "http://localhost:11500/api/chat"
