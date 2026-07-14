# Lector v0.2 — Agent Protocol (Zielbild)

Datum: 2026-07-12 · Status: **v0.2a umgesetzt** (Queue, Quick-Add, Wissen-Notizen,
Agent-API, lector-agent.py) · Revision nach Feedback:

- **Lokaler Chat bleibt** (direkte `claude -p`-Bridge im Server) — die Queue ist
  das Production-Modell für Remote/Tablet, kein Ersatz.
- **Markierung = „mir unklar"** ist das zentrale Primitiv. Der Agent holt sie mit
  Kontext ab (Absätze davor/danach, Kapitel, Buch) und **fragt David zurück**,
  was geschehen soll — er entscheidet nicht selbst.
- **Vier Wissensarten** als Ergebnis: Vokabeln (`vocab`), Grammatik-Konzepte,
  Ideen, spannende Inhalte (`notes.kind = grammar|idea|content`, frei taggbar).

## Leitidee

Die App ist **nur** gehostetes Frontend + Zustand. Keine KI-Aufrufe im Server.
Jede KI (Claude, Codex/ChatGPT, künftige) ist ein **Agent**, der an die aktuelle
Lese-Session andockt: Er holt gespeicherte Nutzer-Aktionen ab (Outbox), liefert
Ergebnisse zurück (Inbox), und die UI rendert sie — egal von wem sie kamen.

Motivation: modellunabhängig, subscription-basiert (kein API-Key), und Davids
Aktionen sind asynchron — nichts im Reader blockiert auf eine KI-Antwort.

## Komponenten

### 1. Session-Store (SQLite, erweitert)
Neue Tabelle `actions` (Outbox):

| Spalte | Bedeutung |
|---|---|
| id, book_id, created_at | wie üblich |
| type | `explain_word` \| `enrich_vocab` \| `chat_message` \| `quiz_me` \| `summarize_chapter` |
| payload | JSON (Wort+Satz, Nachricht, Kapitel, …) |
| status | `pending` → `claimed` → `done` / `failed` |
| agent | wer geclaimt hat ("claude-session", "lector-agent/codex", "chatgpt") |
| result | JSON-Ergebnis (redundant zu Zieltabellen, für Debugging) |

Ergebnisse fließen zusätzlich in die bestehenden Tabellen (vocab.explanation,
chat, summaries), damit die UI nichts Neues lernen muss.

### 2. Agent-API (neu in server.py)
- `GET /api/agent/next?types=…` — älteste pending Aktion **+ kompletter
  Session-Kontext** (Buch, Kapitel, Seite um Leseposition, aktive Marks,
  Kapitel-Summaries, Vokabel-Level). Ein Call = alles, was ein Agent braucht.
- `POST /api/agent/claim` `{action_id, agent}` — Claim mit Timeout (5 min,
  danach wieder pending).
- `POST /api/agent/complete` `{action_id, result}` — schreibt Ergebnis in
  Zieltabelle + markiert done.
- `POST /api/agent/push` `{book_id, kind, content}` — unaufgeforderte Beiträge
  (proaktive Notiz, Quizfrage) → erscheinen im Chat/Notizen.
- Auth: Header `X-Lector-Token` (Token in data/token.txt), Pflicht sobald
  HOST != 127.0.0.1 oder Tunnel aktiv.

### 3. UI-Änderungen
- **Vokabel-Quick-Add**: Wort-Tap → „+ Vokabel" (speichert sofort Wort+Satz,
  legt `enrich_vocab` an); Auswahl-Toolbar → „+ Vokabel" für Wendungen;
  manuelles Eingabefeld im Vokabeln-Tab. Karten zeigen „Erklärung folgt…"
  bis ein Agent ergänzt.
- **Chat** läuft über `chat_message`-Aktionen (Polling auf Antwort statt
  blockierendem Request). UI zeigt, *welcher* Agent geantwortet hat.
- Statusleiste: „Agent verbunden: ✓ (claude)" / „kein Agent — Aktionen werden
  gesammelt". Offline-Lesen sammelt Aktionen, Agent arbeitet sie später ab.

### 4. Agent-Adapter
1. **`lector-agent.py`** (ersetzt Inline-Bridge): lokaler Daemon, pollt
   `/api/agent/next`, beantwortet per Engine:
   - `--engine claude` → `claude -p` (Anthropic-Abo)
   - `--engine codex` → `codex exec` (ChatGPT-Abo)
   Prompt-Bau = heutige build_context-Logik, wandert vom Server in den Agenten.
2. **`/lector`-Skill** (interaktiv): Claude-Code-Session dockt an — liest Queue,
   diskutiert, pusht proaktiv. Wie heute, nur über die formale Agent-API.
3. **ChatGPT von außen** (v0.2c):
   - Tunnel: `cloudflared tunnel` oder `tailscale funnel` → HTTPS-URL
   - `GET /openapi.json` (statisch generiert) → **Custom GPT mit Actions**
     („Lector Tutor"), Token als API-Key-Auth hinterlegt
   - alternativ MCP: dünner `lector-mcp`-Server (gleiche Endpoints), lokal für
     Claude Code (stdio), remote für ChatGPT-Connector (SSE)

## Beispielflüsse

**Quick-Add:** Tap „maleta" → +Vokabel → sofort im Deck (ohne Erklärung) →
`enrich_vocab` pending → Agent ergänzt Synonyme/Nuancen → Karte vollständig.

**In-App-Chat:** Frage → `chat_message` pending → beliebiger angedockter Agent
claimt, antwortet mit Session-Kontext → Antwort erscheint in Sidebar.

**ChatGPT unterwegs:** Custom GPT „Lector Tutor" am Handy: „Quiz mich zu dem,
was ich heute gelesen habe" → Action holt Session + Vokabeln → Quiz in ChatGPT
→ neue Vokabeln via `POST /api/agent/push` zurück ins Deck.

## Sicherheit
- Token-Pflicht außerhalb localhost; Tunnel-URL nicht veröffentlichen.
- Abo-Konformität: jeder Agent läuft unter Davids eigenem Account (Claude-Abo,
  ChatGPT-Abo). Kein Multi-User-Betrieb — bewusstes Non-Goal.

## Etappen
- **v0.2a**: `actions`-Tabelle + Agent-API + Quick-Add-UI + `lector-agent.py`
  (claude-Engine); Inline-`ask_claude` aus den Request-Handlern entfernen.
- **v0.2b**: codex-Engine (`codex exec`) im Daemon; Agent-Anzeige in der UI.
- **v0.2c**: Token-Auth + OpenAPI-Spec + Tunnel-Anleitung + Custom GPT;
  optional `lector-mcp`.
  *Stand 2026-07-15: Server-Seite fertig (Token in `data/token`, Bearer/
  Cookie/?token=-Auth für alles außer direktem localhost, `/openapi.json`
  öffentlich, `setup.sh --funnel` für Tailscale). Custom GPT anlegen +
  optional `lector-mcp` stehen noch aus.*

## Non-Goals
Multi-User-Hosting, API-Key-Betrieb, Konto-Sharing, Mobile-App (Web reicht).
