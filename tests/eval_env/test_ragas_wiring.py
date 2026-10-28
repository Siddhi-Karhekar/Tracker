"""Runs in the RAGAS environment only (requirements-eval.txt): checks that ragas_score drives the real RAGAS
metrics end to end. The judge is a stand-in that returns well-formed replies, so the scores themselves mean
nothing here; a real judge needs network access and a key."""
import json
import typing
from pathlib import Path

from pydantic import BaseModel
from ragas.llms.base import InstructorBaseRagasLLM

from teampulse.eval import ragas_score


def _fill(tp):
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin in (list, typing.List):
        return [_fill(args[0])]
    if origin is typing.Literal:
        return args[0]
    if origin is typing.Union:
        return _fill(next(a for a in args if a is not type(None)))
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return tp(**{name: _fill(f.annotation) for name, f in tp.model_fields.items()})
    return {str: "The team decided to use Postgres.", int: 1, float: 1.0, bool: True}.get(tp, "x")


class StandInJudge(InstructorBaseRagasLLM):
    def __init__(self):
        self.calls = 0

    def generate(self, prompt, response_model):
        self.calls += 1
        return _fill(response_model)

    async def agenerate(self, prompt, response_model):
        return self.generate(prompt, response_model)


def test_scores_every_metric_for_every_row(tmp_path):
    rows = [{"id": "q10", "agent": "supervisor", "user_input": "Which database did we decide to use?",
             "response": "Postgres instead of Firebase.", "retrieved_contexts": ["final decision: Postgres use karenge"],
             "reference": "Postgres instead of Firebase."},
            {"id": "q09", "agent": "retrieval", "user_input": "Was the menu API merged?", "response": "Yes.",
             "retrieved_contexts": [], "reference": "Yes, PR #15 was merged."}]
    judge = StandInJudge()
    scored, summary = ragas_score.run(rows, judge, min_interval=0)
    assert judge.calls >= 8
    for r in scored:
        assert not r.get("errors"), r.get("errors")
        assert all(isinstance(r[m], float) and 0 <= r[m] <= 1 for m in ragas_score.METRICS)
    assert set(summary) == {"supervisor", "retrieval"}
    md = ragas_score.to_markdown(summary, scored, "stand-in")
    assert "| supervisor | 1 |" in md
