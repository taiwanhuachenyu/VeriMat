# Development evidence source audit

Status: **draft / not independently checked**. This audit records the primary-study sources used
to exercise the V2 evaluation stack. It does not certify the benchmark labels and is not a hidden
test-set audit.

## Source inventory

| Snapshot | Primary article | Evidence location | Publication date | License |
|---|---|---|---|---|
| `e-llzo-current-bound` | [Wang et al., Nature Communications 11, 5201](https://www.nature.com/articles/s41467-020-19004-4), DOI `10.1038/s41467-020-19004-4` | Results: stability and kinetics of the Li/LLZO interface | 2020-10-15 | CC BY 4.0 |
| `e-ncm-single-surface` | [Zhang et al., Nature Communications 11, 3050](https://www.nature.com/articles/s41467-020-16824-2), DOI `10.1038/s41467-020-16824-2` | Results: surface analysis of pristine NCM electrodes | 2020-06-16 | CC BY 4.0 |
| `e-llzo-defects` | [Sastre et al., Communications Materials 2, 76](https://www.nature.com/articles/s43246-021-00177-4), DOI `10.1038/s43246-021-00177-4` | Abstract | 2021-07-14 | CC BY 4.0 |
| `e-nmc-cracking` | [Liu et al., Nature Communications 12, 6024](https://www.nature.com/articles/s41467-021-26290-z), DOI `10.1038/s41467-021-26290-z` | Introduction: surface protection and mechanical degradation | 2021-10-15 | CC BY 4.0 |
| `e-sulfide-moisture` | [Liu et al., Nature Communications 16, 213](https://www.nature.com/articles/s41467-024-55634-8), DOI `10.1038/s41467-024-55634-8` | Introduction: moisture sensitivity of sulfide solid electrolytes | 2025-01-02 | CC BY 4.0 |
| `e-halide-oxidation` | [Song et al., Nature Communications 15, 1481](https://www.nature.com/articles/s41467-024-45864-1), DOI `10.1038/s41467-024-45864-1` | Abstract | 2024-02-17 | CC BY 4.0 |
| `e-perovskite-traps` | [Ahn et al., Nature Communications 7, 13422](https://www.nature.com/articles/ncomms13422), DOI `10.1038/ncomms13422` | Abstract | 2016-11-10 | CC BY 4.0 |
| `e-sodium-sulfide-air` | [Hayashi et al., Nature Communications 10, 5266](https://www.nature.com/articles/s41467-019-13178-2), DOI `10.1038/s41467-019-13178-2` | Results: exposure to the atmosphere | 2019-11-20 | CC BY 4.0 |
| `e-perovskite-controlled-moisture` | [Liu et al., Nature Communications 13, 4891](https://www.nature.com/articles/s41467-022-32482-y), DOI `10.1038/s41467-022-32482-y` | Abstract | 2022-08-19 | CC BY 4.0 |
| `e-halide-cycle` | [Song et al., Nature Communications 15, 1481](https://www.nature.com/articles/s41467-024-45864-1), DOI `10.1038/s41467-024-45864-1` | Abstract | 2024-02-17 | CC BY 4.0 |

## Mechanical checks already enforced

- Every capsule is 1–120 words and its exact UTF-8 bytes match `content_sha256`.
- URL, DOI, section locator, publication date, retrieval timestamp, attribution, normalization and
  SPDX license are mandatory in the evidence snapshot schema.
- Every challenge evidence record must match its snapshot on hash, URL, DOI, section, date and
  license; orphan snapshots are rejected.
- Evidence marked `available_by_cutoff` must predate the challenge cutoff.
- The two halide items share one leakage group and cannot be split across development and test.

## Human checks still required before freezing

Two reviewers who did not author the items should independently verify: (1) capsule faithfulness to
the cited location, (2) whether the relation label follows from the passage, (3) whether the expected
terminal decision is neither too broad nor too narrow, and (4) whether a materially stronger
pre-cutoff source changes the gold set. Disagreements must be adjudicated and recorded; only then may
`construction_provenance.status` change from `draft` to `frozen`.

