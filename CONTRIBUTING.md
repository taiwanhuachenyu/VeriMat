# Contributing

Contributions should preserve VeriMat's trust boundary:

1. model output is a proposal, never an authority;
2. factual claims require replayable evidence locators;
3. terminal-state transitions require executed evidence-bearing events;
4. benchmark gold must remain unavailable to the generator;
5. tests and examples must be offline by default;
6. scientific and engineering claims must state their evidence boundary.

Before submitting a change, run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

Only contribute content that you are authorized to distribute under the repository license.
