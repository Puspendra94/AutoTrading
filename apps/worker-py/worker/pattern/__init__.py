"""The pattern/indicator trading brain.

Everything the pattern brain owns lives under this package — features, decision loop,
guardrails, sizing, exits and its own persistence. Nothing here imports from
`worker.strategy`, and nothing in `worker.strategy` imports from here: the two brains share
only the account-level layer (execution engine, risk gate, orders, positions, candles).

That one-way boundary is deliberate. It is what makes TRADING_BRAIN a real switch rather than
a suggestion — see worker/config.py.
"""
