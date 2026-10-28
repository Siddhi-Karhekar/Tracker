"""LangSmith integration: the dataset, the evaluators, and a real `langsmith.evaluate` run done locally
(no account, nothing uploaded)."""
import uuid

from langsmith.schemas import Example

from teampulse import tracing
from teampulse.eval import langsmith_eval as le
from teampulse.eval import qa
from teampulse.llm import LLM


def test_dataset_examples_carry_the_answer_key():
    ex = le.examples(qa.load_questions())
    assert len(ex) == 18 and len({e["metadata"]["id"] for e in ex}) == 18
    assert all(e["outputs"]["reference"] and e["outputs"]["specialists"] for e in ex)


def test_evaluators_score_good_and_bad_answers():
    ref = {"must_mention": [["postgres"]], "must_cite": [["discord:900006"]], "must_not": [], "specialists": ["retrieval"]}
    good = {"result": {"answer": "Postgres [1].", "cited": [1], "evidence": [{"event_id": "discord:900006"}],
                       "route": {"parts": [{"specialist": "retrieval"}]}, "guardrails": []}, "faithfulness": 1.0}
    bad = {"result": {"answer": "Firebase.", "cited": [], "evidence": [], "route": {"parts": [{"specialist": "memory"}]},
                      "guardrails": [{"agent": "retrieval", "guard": "invented_link", "action": "repair"}]}}
    assert [e(good, ref)["score"] for e in (le.correctness, le.citation_recall, le.routing)] == [1.0, 1.0, 1.0]
    assert [e(bad, ref)["score"] for e in (le.correctness, le.citation_recall, le.routing)] == [0.0, 0.0, 0.0]
    assert le.guardrails_clean(good)["score"] == 1.0 and le.guardrails_clean(bad)["score"] == 0.0


def test_a_real_langsmith_evaluate_run_works_locally(tmp_path, monkeypatch):
    """Regression: LangSmith uploaded evaluator and LangGraph traces (team chat text) even with uploads off."""
    import requests
    calls = []
    real_send = requests.Session.send

    def watch(self, request, **kw):
        if "smith.langchain" in request.url:
            calls.append(request.url)
        return real_send(self, request, **kw)
    monkeypatch.setattr(requests.Session, "send", watch)
    monkeypatch.setenv("TEAMPULSE_EMBEDDER", "hash")
    questions = qa.load_questions()[:3]
    data = [Example(id=uuid.uuid4(), dataset_id=uuid.uuid4(), inputs=e["inputs"], outputs=e["outputs"],
                    metadata=e["metadata"]) for e in le.examples(questions)]
    from langsmith import Client
    from langsmith.schemas import LangSmithInfo
    client = Client(api_key="local-only", info=LangSmithInfo())
    results = le.run_experiment(True, tmp_path, client=client, data=data, upload=False)   # nothing may leave
    scores = le.summary(results)
    assert set(scores) == {"correctness", "citation_recall", "routing", "guardrails_clean", "faithfulness"}
    assert all(0 <= v <= 1 for v in scores.values())
    client.flush()                                # push out anything LangSmith queued for upload
    assert not calls, calls


def test_tracing_is_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    assert not tracing.langsmith_enabled()
    sentinel = object()
    assert tracing.langsmith_wrap(sentinel) is sentinel
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert tracing.langsmith_enabled()
    import langsmith.wrappers
    wrapped = []
    monkeypatch.setattr(langsmith.wrappers, "wrap_openai", lambda c: wrapped.append(c) or c)
    LLM(providers=["groq"])._client("groq")
    assert len(wrapped) == 1 and type(wrapped[0]).__name__ == "OpenAI"   # every AI call goes into the trace
