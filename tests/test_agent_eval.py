"""The per-agent evaluation runs offline and every agent meets its regression floor (CI gate)."""
import json

from teampulse.eval import agents
from teampulse.index import HashEmbedder


def test_every_agent_meets_its_offline_threshold(tmp_path):
    result = agents.run(offline=True, folder=tmp_path, embedder=HashEmbedder())
    below = [g for g in result["thresholds"] if not g["ok"]]
    assert not below, json.dumps(below, indent=1)
    assert set(result["results"]) == set(agents.AGENTS)
    rows = result["ragas_rows"]                                  # export for RAGAS is complete and well-formed
    assert len(rows) == 18 and all(r["user_input"] and r["reference"] and isinstance(r["retrieved_contexts"], list)
                                   for r in rows)
    assert "{date:" not in " ".join(r["reference"] for r in rows)   # relative dates resolved
    assert "## Agent evaluation (offline)" in agents.to_markdown(result)
