"""Tests for the LLM judge: verdict parsing, providers, proposal integration and the endpoint."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from daisy.judge import (
    MAX_FILE_CHARS,
    SYSTEM_PROMPT,
    HeuristicProvider,
    JudgeConfigError,
    JudgeError,
    JudgeVerdict,
    OllamaProvider,
    OpenAICompatibleProvider,
    Verdict,
    build_user_prompt,
    parse_verdict,
    select_judge_provider,
)
from daisy.main import create_app
from daisy.proposals import ProposalError, ProposalManager, ProposalStatus

TOKEN = "test-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CLEAN = "def add(a: int, b: int) -> int:\n    return a + b\n"
APPROVED_JSON = {
    "verdict": "APPROVED",
    "confidence": 0.9,
    "risks": [],
    "summary": "No concern found.",
}
requires_tools = pytest.mark.skipif(
    importlib.util.find_spec("ruff") is None or importlib.util.find_spec("mypy") is None,
    reason="ruff and mypy are dev dependencies",
)


def make_verdict(verdict: Verdict = "APPROVED", provider: str = "fake") -> JudgeVerdict:
    return JudgeVerdict(
        verdict=verdict,
        confidence=0.8,
        risks=(),
        summary="stub verdict",
        provider=provider,
    )


class FakeJudge:
    """Returns a fixed verdict and counts how often it was asked."""

    name = "fake"

    def __init__(self, verdict: Verdict = "APPROVED") -> None:
        self.verdict = verdict
        self.calls = 0

    def audit(self, changes: dict[str, str]) -> JudgeVerdict:
        self.calls += 1
        return make_verdict(self.verdict)


def failing_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    return httpx.MockTransport(handler)


# Verdict parsing


def test_parse_accepts_valid_verdict() -> None:
    verdict = parse_verdict(json.dumps(APPROVED_JSON), "groq")
    assert verdict.verdict == "APPROVED"
    assert verdict.confidence == 0.9
    assert verdict.risks == ()
    assert verdict.summary == "No concern found."
    assert verdict.provider == "groq"


def test_parse_accepts_fenced_json() -> None:
    fenced = "```json\n" + json.dumps(APPROVED_JSON) + "\n```"
    assert parse_verdict(fenced, "ollama").verdict == "APPROVED"


@pytest.mark.parametrize("verdict", ["MAYBE", "approved", None])
def test_parse_rejects_unknown_verdict(verdict: object) -> None:
    payload = {**APPROVED_JSON, "verdict": verdict}
    with pytest.raises(JudgeError):
        parse_verdict(json.dumps(payload), "x")


@pytest.mark.parametrize("confidence", [1.5, -0.1, True, "0.5"])
def test_parse_rejects_bad_confidence(confidence: object) -> None:
    payload = {**APPROVED_JSON, "confidence": confidence}
    with pytest.raises(JudgeError):
        parse_verdict(json.dumps(payload), "x")


def test_parse_rejects_risks_that_are_not_strings() -> None:
    payload = {**APPROVED_JSON, "risks": "shell access"}
    with pytest.raises(JudgeError):
        parse_verdict(json.dumps(payload), "x")
    with pytest.raises(JudgeError):
        parse_verdict(json.dumps({**APPROVED_JSON, "risks": [1]}), "x")


def test_parse_rejects_empty_summary() -> None:
    with pytest.raises(JudgeError):
        parse_verdict(json.dumps({**APPROVED_JSON, "summary": "  "}), "x")


def test_parse_rejects_non_json_and_non_object() -> None:
    with pytest.raises(JudgeError):
        parse_verdict("not json", "x")
    with pytest.raises(JudgeError):
        parse_verdict("[1, 2]", "x")


# Heuristic provider


def test_heuristic_approves_clean_code() -> None:
    verdict = HeuristicProvider().audit({"pkg/a.py": CLEAN})
    assert verdict.verdict == "APPROVED"
    assert verdict.risks == ()
    assert verdict.provider == "heuristic"


def test_heuristic_summary_states_its_limits() -> None:
    verdict = HeuristicProvider().audit({"pkg/a.py": CLEAN})
    assert "not assessed" in verdict.summary


def test_heuristic_rejects_eval() -> None:
    verdict = HeuristicProvider().audit({"a.py": "x = eval(payload)\n"})
    assert verdict.verdict == "REJECTED"
    assert any("security" in risk and "a.py" in risk for risk in verdict.risks)


def test_heuristic_rejects_path_traversal() -> None:
    source = 'open("../secret.txt").read()\n'
    verdict = HeuristicProvider().audit({"a.py": source})
    assert verdict.verdict == "REJECTED"
    assert any("path traversal" in risk for risk in verdict.risks)


def test_heuristic_rejects_recursive_delete() -> None:
    verdict = HeuristicProvider().audit({"a.py": "import shutil\nshutil.rmtree('data')\n"})
    assert verdict.verdict == "REJECTED"


def test_heuristic_warns_on_subprocess() -> None:
    source = "import subprocess\nsubprocess.run(['ls'])\n"
    verdict = HeuristicProvider().audit({"a.py": source})
    assert verdict.verdict == "WARNING"


# Prompt


def test_prompt_marks_content_untrusted_and_includes_files() -> None:
    prompt = build_user_prompt({"pkg/a.py": CLEAN})
    assert 'path="pkg/a.py"' in prompt
    assert CLEAN in prompt
    assert "untrusted" in prompt
    assert "path traversal" in SYSTEM_PROMPT


def test_prompt_truncates_large_files() -> None:
    prompt = build_user_prompt({"big.py": "x" * (MAX_FILE_CHARS + 500)})
    assert "(truncated)" in prompt
    assert prompt.count("x") == MAX_FILE_CHARS


# Providers over HTTP


def test_openai_compatible_provider_sends_key_and_parses_verdict() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        content = json.dumps({**APPROVED_JSON, "verdict": "REJECTED"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    provider = OpenAICompatibleProvider(
        "groq",
        "https://example.test/v1",
        "k-123",
        "model-x",
        transport=httpx.MockTransport(handler),
    )
    verdict = provider.audit({"a.py": CLEAN})
    assert verdict.verdict == "REJECTED"
    assert verdict.provider == "groq"
    assert seen["auth"] == "Bearer k-123"
    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["body"]["model"] == "model-x"  # type: ignore[index]


def test_ollama_provider_requests_json_and_parses_verdict() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"response": json.dumps(APPROVED_JSON)})

    provider = OllamaProvider("qwen", "http://127.0.0.1:1/api/generate", httpx.MockTransport(handler))
    verdict = provider.audit({"a.py": CLEAN})
    assert verdict.verdict == "APPROVED"
    assert verdict.provider == "ollama"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["format"] == "json"
    assert body["stream"] is False


def test_http_error_becomes_warning_not_approval() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    provider = OpenAICompatibleProvider(
        "mistral", "https://example.test", "k", "m", httpx.MockTransport(handler)
    )
    verdict = provider.audit({"a.py": CLEAN})
    assert verdict.verdict == "WARNING"
    assert verdict.confidence == 0.0
    assert "HTTP 500" in verdict.risks[0]


def test_transport_error_becomes_warning() -> None:
    provider = OllamaProvider("qwen", "http://127.0.0.1:1/api/generate", failing_transport())
    verdict = provider.audit({"a.py": CLEAN})
    assert verdict.verdict == "WARNING"
    assert any("judge unavailable" in risk for risk in verdict.risks)


def test_invalid_model_output_becomes_warning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": "sure, looks fine!"})

    provider = OllamaProvider("qwen", "http://127.0.0.1:1/api/generate", httpx.MockTransport(handler))
    assert provider.audit({"a.py": CLEAN}).verdict == "WARNING"


def test_unexpected_reply_shape_becomes_warning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    provider = OpenAICompatibleProvider(
        "gemini", "https://example.test", "k", "m", httpx.MockTransport(handler)
    )
    assert provider.audit({"a.py": CLEAN}).verdict == "WARNING"


# Provider selection


def test_select_defaults_to_heuristic() -> None:
    assert isinstance(select_judge_provider({}), HeuristicProvider)


def test_select_cloud_provider_requires_its_key() -> None:
    with pytest.raises(JudgeConfigError):
        select_judge_provider({"DAISY_JUDGE_PROVIDER": "groq"})


def test_select_cloud_provider_with_key() -> None:
    provider = select_judge_provider({"DAISY_JUDGE_PROVIDER": "groq", "GROQ_API_KEY": "k"})
    assert provider.name == "groq"


def test_select_ollama_needs_no_key() -> None:
    assert select_judge_provider({"DAISY_JUDGE_PROVIDER": "ollama"}).name == "ollama"


def test_select_rejects_unknown_provider() -> None:
    with pytest.raises(JudgeConfigError):
        select_judge_provider({"DAISY_JUDGE_PROVIDER": "nope"})


# Proposal integration


@requires_tools
def test_propose_stores_judge_verdict(tmp_path: Path) -> None:
    judge = FakeJudge("WARNING")
    manager = ProposalManager(tmp_path, judge=judge)
    proposal = manager.propose({"pkg/clean.py": CLEAN})
    assert proposal.judge_verdict is not None
    assert proposal.judge_verdict.verdict == "WARNING"
    assert judge.calls == 1


@requires_tools
def test_propose_without_judge_leaves_verdict_empty(tmp_path: Path) -> None:
    proposal = ProposalManager(tmp_path).propose({"pkg/clean.py": CLEAN})
    assert proposal.judge_verdict is None


@requires_tools
def test_judge_outage_does_not_block_proposal(tmp_path: Path) -> None:
    judge = OllamaProvider("qwen", "http://127.0.0.1:1/api/generate", failing_transport())
    proposal = ProposalManager(tmp_path, judge=judge).propose({"pkg/clean.py": CLEAN})
    assert proposal.status is ProposalStatus.PENDING
    assert proposal.judge_verdict is not None
    assert proposal.judge_verdict.verdict == "WARNING"


@requires_tools
def test_rejected_verdict_does_not_block_human_approval(tmp_path: Path) -> None:
    manager = ProposalManager(tmp_path, judge=FakeJudge("REJECTED"))
    proposal = manager.propose({"pkg/clean.py": CLEAN})
    approved = manager.approve(proposal.proposal_id, "Jean Decian")
    assert approved.status is ProposalStatus.APPROVED


@requires_tools
def test_audit_reruns_judge_and_stores_result(tmp_path: Path) -> None:
    judge = FakeJudge("APPROVED")
    manager = ProposalManager(tmp_path, judge=judge)
    proposal = manager.propose({"pkg/clean.py": CLEAN})
    judge.verdict = "REJECTED"
    audited = manager.audit(proposal.proposal_id)
    assert audited.judge_verdict is not None
    assert audited.judge_verdict.verdict == "REJECTED"
    assert judge.calls == 2


def test_audit_without_judge_is_an_error(tmp_path: Path) -> None:
    manager = ProposalManager(tmp_path)
    with pytest.raises(ProposalError):
        manager.audit("0" * 32)


# Sidecar endpoint


@requires_tools
def test_propose_response_includes_judge_verdict(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            TOKEN,
            db_path=tmp_path / "daisy.db",
            workspace=tmp_path,
            judge=HeuristicProvider(),
        )
    )
    body = client.post(
        "/proposals/propose", json={"changes": {"pkg/a.py": CLEAN}}, headers=AUTH
    ).json()
    assert body["judge_verdict"]["provider"] == "heuristic"
    assert body["judge_verdict"]["verdict"] == "APPROVED"


@requires_tools
def test_judge_endpoint_returns_structured_verdict(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            TOKEN,
            db_path=tmp_path / "daisy.db",
            workspace=tmp_path,
            judge=FakeJudge("WARNING"),
        )
    )
    proposal_id = client.post(
        "/proposals/propose", json={"changes": {"pkg/a.py": CLEAN}}, headers=AUTH
    ).json()["proposal_id"]
    response = client.post(f"/proposals/{proposal_id}/judge", headers=AUTH)
    assert response.status_code == 200
    verdict = response.json()["judge_verdict"]
    assert verdict["verdict"] == "WARNING"
    assert set(verdict) == {"verdict", "confidence", "risks", "summary", "provider"}


def test_judge_endpoint_unknown_id_is_404(tmp_path: Path) -> None:
    client = TestClient(
        create_app(TOKEN, db_path=tmp_path / "daisy.db", workspace=tmp_path, judge=FakeJudge())
    )
    assert client.post("/proposals/missing/judge", headers=AUTH).status_code == 404


def test_judge_endpoint_requires_bearer_token(tmp_path: Path) -> None:
    client = TestClient(
        create_app(TOKEN, db_path=tmp_path / "daisy.db", workspace=tmp_path, judge=FakeJudge())
    )
    assert client.post("/proposals/anything/judge").status_code == 401
