# Lector (readly)

Local-first AI reader for language learning. All AI runs through the user's own
Claude CLI (`claude -p`, subscription) — never add an API key requirement.

## Bringing everything up

Run `./setup.sh` — it checks python3 + claude CLI, symlinks the `/lector` skill
into `~/.claude/skills/`, and starts the server on port 8123 (idempotent).
Variants: `--lan` (reachable from phone/tablet), `--stop`, `--status`.

## Architecture in one breath

`server.py` (stdlib-only Python, no deps) serves `static/` (vanilla JS reader)
and owns all state in `data/lector.db` (SQLite) + `data/events.jsonl`. User
marks ("unclear") and quick-added vocab become actions in an outbox; agents
fetch them with context via `GET /api/agent/next`, ask the user what to do,
answer via `/api/agent/complete|push`, and write knowledge to `notes` /
`vocab`. `lector-agent.py` is the headless worker (engines: claude, codex).
The `/lector` skill (.claude/skills/lector/SKILL.md) defines the interactive
teacher workflow and documents every endpoint, including full Anki access.

## Rules

- `data/` is personal reading data — gitignored, never commit it.
- UI language is English; the AI teaching output is German by design (native
  language of the learner) — see TEACHER_SYSTEM in server.py.
- Design system: sharp edges (radius 0), 1px hairlines, token-based theming
  (dark default, `[data-theme="light"]`), Instrument Sans + Newsreader.
- Design docs live in `docs/plans/`; keep them updated when architecture shifts.
