"""Literature survey: retrieve a subfield, extract its structure-property relations, state its gaps.

The task statement makes traceability the binding constraint rather than a nicety: every claim and
every gap has to reach a specific document in a named database, and cited text is checked against a
full-text index.  So the pipeline is built as a series of gates instead of a series of prompts.  A
model proposes; a deterministic check decides.  A relation whose quote is not literally in its
passage never becomes a record, and a gap that cites nothing never becomes a gap.
"""
