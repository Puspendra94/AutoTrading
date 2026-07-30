"""The pattern brain's decision loop.

    features -> gate -> prompt -> DeepSeek -> validate -> size -> risk gate -> order

Division of labour, which is the whole safety model:

  * The GATE is deterministic and decides only WHETHER to spend an LLM call.
  * The LLM decides direction, entry range, stop and target — and nothing else. It never sees a
    quantity and cannot ask for one.
  * The VALIDATOR is deterministic and can only reject, never rewrite. A rejected decision is
    logged and skipped, so the audit trail records what the model actually said.
  * SIZING is deterministic and owns the money. Policy constants live in code, so the LLM
    cannot widen its own risk budget by asking.
"""
