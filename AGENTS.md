## Agent skills

The skills themselves are not committed: `.claude/skills/` and `.agents/` are git-ignored. `skills-lock.json` records each skill's source repository, path and content hash. The documents under `docs/agents/` stand on their own and work without the skills installed.

### Issue tracker

Issues and specs live as GitHub issues in `Igman/flighty_exporter`, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
