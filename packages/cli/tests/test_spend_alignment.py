"""Envelope, run result and notification use the server-recorded spend."""

import pytest

from designaround_cli.main import agent_outcome, charged_credits
from designaround_cli.notify import describe_outcome
from designaround_cli.output import CliExit


def test_charged_credits_keeps_a_real_zero():
    assert charged_credits({"spent": 0, "run_id": "run-free"}) == 0
    assert charged_credits({"spent": 26}) == 26


def test_gate_failed_error_carries_the_recorded_spend():
    with pytest.raises(CliExit) as caught:
        agent_outcome(2, {"type": "result", "status": "gate_failed", "spent": 26, "version": "v1", "reason": "photos do not match"}, what="CAD Agent")
    assert caught.value.kind == "gate_failed"
    assert caught.value.cost == {"charged": 26}
    with pytest.raises(CliExit) as unnamed:
        agent_outcome(2, {"type": "result", "status": "gate_failed", "spent": 4, "version": "v4"}, what="CAD Agent")
    assert unnamed.value.kind == "gate_failed"
    assert "photo check" in unnamed.value.message
    assert unnamed.value.cost == {"charged": 4}


def test_notification_uses_the_same_spend():
    _title, text = describe_outcome("model", "case-1", 2, {"status": "gate_failed", "reason": "photos do not match", "spent": 26})
    assert "26" in text
    _title, done = describe_outcome("model", "case-1", 0, {"status": "ok", "version": "v1", "spent": 26})
    assert "26" in done
