# GitHub development snapshot — 2026-10-08

Repository: https://github.com/jiamuOvO/LAPIS
Branch: codex/intake-v3-development

## Status

This branch preserves unfinished v3 intake development. It is not a release and
has not passed end-to-end acceptance. GitHub main retains the previous baseline.

Included: v3 request contracts, deterministic conflict checks, local source-backed
recommendation catalog, intake changes, active request validity tracking,
migration 005, and initial contract tests.

## Upload checks

- Python files parsed successfully with the ast module.
- Three tests in test_lapis_v3_contract.V3ContractTest passed.
- git diff --check passed.
- Bounded text secret-pattern scan found no matches.
- Local credentials, Conda environments, databases, backups, and artifacts are ignored.

No database migration, live LLM run, scientific computation, or full test suite
was performed for this upload.

## Remaining integration work

- lapis.py must propagate input_context for confirmations and selections.
- Existing intake tests still patch the removed propose_intake function.
- Graph tests and CLI require v3 contract adaptation.
- Mixed intents, stale selections, issue resolution, provenance, and recovery
  require further verification.
- Migration 005 requires backup and deployment validation; uploading SQL does
  not apply it to an existing database.

This snapshot is not evidence of real computation or scientific conclusions.
Merge to main only after integration and acceptance.