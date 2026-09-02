# Data, services, and licenses

## Data provenance

The confirmatory thermoelectric snapshot uses Sciverse for scientific search and full-text location.

| Item | Version/scope | Repository representation | Redistribution boundary |
|---|---|---|---|
| Discovery corpus | thermoelectric literature, 2000–2021 | source identifiers, locators, necessary evidence passages, and SHA-256 hashes | no bulk publisher PDFs |
| Validation corpus | 2022–2025 | query/audit records and accepted evidence needed to replay oracle decisions | no bulk publisher PDFs |
| Evaluation claims | `semifinal-v2-thermo`, frozen 2026-08-28 | derived claim, relation, prediction, and score artifacts | released with the code snapshot |

Access to the upstream Sciverse service requires the user's own token and is governed by Sciverse's terms. The repository does not include or redistribute access credentials.

## External models and services

- Formal structured model route: `zhipuai/glm-5.3-flash` through OpenCode 1.18.21.
- Formal discovery retrieval: Sciverse API.
- OQMD validation was not active in the formal v2 run because the endpoint was unavailable; affected checks are recorded as uncovered rather than imputed.

Service availability, pricing, and provider terms may change. Reproduction users are responsible for reviewing current terms before making network calls.

## Prompt and configuration disclosure

- Complete model prompts and response contracts: `docs/PROMPTS.md`
- Frozen experiment definition: `preregistration/semifinal_v2.json`
- Non-secret environment template: `config/verimat.env.example`
- Pinned Python dependencies: `requirements-runtime.lock` and `requirements-dev.lock`

## License boundary

VeriMat source code is MIT licensed. This license does not replace or broaden the rights attached to third-party papers, databases, APIs, model services, or generated provider outputs. No secret, private absolute path, local scratch directory, or bulk copyrighted paper collection belongs in a public release or submission archive.
