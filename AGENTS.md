# Lector (readly) — agent guide

Local-first AI reader for language learning. All AI runs through the user's own
CLI subscriptions (`claude -p` or `codex exec`) — never add an API key
requirement. This file is the Codex/ChatGPT counterpart to CLAUDE.md.

## Bringing everything up

Run `./setup.sh` — checks python3 + AI CLIs, installs the Claude skill, starts
the server on port 8123 (idempotent). `--lan` / `--stop` / `--status`.

## Acting as the reading tutor (outbox workflow)

The reader stores every user action; agents pick them up and respond:

1. `curl -s http://127.0.0.1:8123/api/agent/next` — oldest pending action plus
   full context (book, chapter, passage before/after, known vocab, summaries).
2. `POST /api/agent/claim` `{"action_id": N, "agent": "codex"}`
3. For `unclear_mark` actions: show the user the passage and ASK what they want
   — create vocab (`POST /api/vocab`), discuss, or store a note
   (`POST /api/notes` with kind `grammar` | `idea` | `content`, comma tags).
   For `enrich_vocab`: no asking — write a compact explanation (meaning in
   context, 2-3 synonyms with nuances, one example sentence) via
   `POST /api/agent/complete` `{"action_id": N, "result": {"explanation": "…"}}`.
4. Push proactive messages into the reader chat: `POST /api/agent/push`
   `{book_id, content}`.

Full Anki access: `GET /api/review/next|stats|log`, `POST /api/review/answer`
(grades: again/hard/good/easy, include your `agent` name),
`POST /api/vocab/<id>/update|delete` (fields incl. due_at, interval_days).

Headless worker: `python3 lector-agent.py --engine codex` (or `claude`).

## Teaching style

The learner is a German native (B1-B2) reading Spanish and English. Explain in
German, examples in the target language. Nuances and synonyms over bare
translations; ask short comprehension check-backs; be concise.

## Rules

- `data/` is personal reading data — gitignored, never commit it.
- State lives in `data/lector.db` (SQLite) + `data/events.jsonl`; the full
  observe endpoint is `GET /api/observe`.
- UI is English; AI teaching output is German by design (TEACHER_SYSTEM in
  server.py).
