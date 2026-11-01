# AI security

The AI in TeamPulse reads text written by people who aren't trusted (anyone who can open an issue on a public repo)
and writes text that people may act on. The design rule is: **the AI can say things, it can't do things**, and what
it says is checked before anyone sees it.

## The trust boundary

```
 team text ──► LLM.chat ──────────────────────────► AI provider
               1. keys removed                       │
               2. emails / phones removed            │
               3. injected instructions marked       ▼
                                                 answer ──► guardrails.apply ──► page
                                                            (links, contacts, keys, leaked instructions,
                                                             ungrounded references, report rules)
```

- **No write tools.** The agents only read. Nothing the AI returns can assign, approve, complete or delete. Drafts
  from the AI planner are saved as drafts and need a person to apply them, through the normal permission checks.
- **Permissions never depend on the AI.** Who can do what is decided by `auth.authorize`, in code.
- **One entrance, one exit.** Every request goes through `LLM.chat`; every answer through `guardrails.apply`.
  A new agent gets both automatically.

## Guardrail catalogue

| Proposed guardrail | How it's handled in TeamPulse | On failure |
|---|---|---|
| `prompt_injection` | `injection.py`: deterministic patterns (including Hinglish); a notice is added to the system message; counted and listed on the Security page | Degrade: the answer still runs, with the notice |
| `secret_leak` (input) | Keys redacted before sending | Fail closed: key never leaves |
| `secret_leak` (output) | Output guardrail hides key-shaped text | Fail closed |
| `pii_leak` | Emails and phones redacted before sending; contact details in answers removed unless they're in the evidence | Fail closed |
| `system_prompt_leak` | Output check for the instructions' text | Fail closed: answer replaced |
| `fabricated_link` / `fabricated_reference` | Links and item references must come from the evidence | Removed from the answer |
| `ungrounded_claim` | Blockers must cite sources; ungrounded answers are flagged | Flagged in the answer |
| `judgemental_report` / `report_tampering` | Contribution rules check (no ranking or judging people); approvals store an evidence digest that lapses on change | Report blocked / approval lapses |
| `unauthorized_action` | No write tools; MCP tools are read-only or go through permission checks | Not reachable |
| `cross_agent_escalation` | Agents share one permission layer; the supervisor can't call anything a user couldn't | Not reachable |
| `cross_tenant_leak` | One team per install; separate database and search index per install | Not applicable (tested: two installs share nothing) |
| `cost_abuse` | Per-person hourly limit, token cap, cache | Request refused |
| `model_supply_chain` | Models are API endpoints or Ollama models the team pulls; the embedding model is named exactly (`TEAMPULSE_EMBEDDER`); Python packages pinned in `requirements.lock` and audited | Accepted risk, documented |
| `abstention` | When retrieval finds nothing, the answer says so without calling the AI at all | Answer abstains |

## What we don't claim

Pattern-based injection detection can be bypassed with new wording. It's there to make attempts visible to a
leader, not as the thing that keeps the team safe. The structural controls above are.

## Tests

- `tests/test_guardrails.py`: a scripted AI breaks one rule at a time through the real agents.
- `tests/test_llm_safety.py`: limits, the injection notice, disguised links and images.
- `tests/test_security.py`: secret and PII redaction, injection detection, isolation and listing.
- `python -m teampulse.eval.langsmith_eval`: quality evaluation against a labelled dataset.
