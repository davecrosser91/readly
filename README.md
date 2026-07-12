# Lector

Sprachenlernen durch Lesen — ein lokaler AI-Reader nach dem Readlang-Prinzip,
aber KI-nativ: Die KI liest mit, erklärt Wörter im Kontext (Synonyme & Nuancen
statt bloßer Übersetzung), diskutiert über den Text und baut nebenbei ein
Spaced-Repetition-Vokabeldeck auf.

**Kein API-Key nötig.** Alle KI-Features laufen über die lokale Claude-CLI
(`claude -p`) und damit über die bestehende Claude-Subscription.

## Start

```bash
./setup.sh               # prüft Dependencies, installiert den /lector-Skill,
                         # startet den Server → http://127.0.0.1:8123
./setup.sh --lan         # zusätzlich vom Handy/Tablet im WLAN erreichbar
./setup.sh --stop        # Server stoppen
```

Oder manuell: `python3 server.py` (stdlib-only, keine Dependencies).

Beim ersten Start werden zwei Beispielbücher importiert (Original-Kurzgeschichten,
Spanisch + Englisch). Eigene Bücher: EPUB oder TXT über die Bibliothek importieren.

## Features

- **Reader**: ablenkungsfreie Buch-Typografie, Kapitel-Navigation, Leseposition wird gemerkt
- **Wort anklicken** → Erklärung im Satzkontext: Bedeutung, Synonyme mit Nuancen, Beispielsatz — landet automatisch im Vokabeldeck
- **Passage markieren** → wird Teil des stehenden Gesprächskontexts
- **Chat-Sidebar**: diskutiert über die Seite; Kontext = markierte Wörter + markierte Passagen + aktuelle Seite + Zusammenfassungen der bisherigen Kapitel (werden im Hintergrund erzeugt und gecacht)
- **Vokabeln**: Quick-Add überall (Wort-Popover, Auswahl, manuell) — sofort
  gespeichert, Erklärung ergänzt asynchron ein Agent; Liste + Spaced-Repetition-Review
- **Wissen**: Grammatik-Konzepte, Ideen und spannende Inhalte als taggbare
  Notizen — angelegt von dir oder einem Agenten
- **Outbox/Inbox**: Markierungen („unklar") landen in einer Aktions-Queue;
  angedockte Agenten (`/lector`-Skill interaktiv, `lector-agent.py --engine
  claude|codex` headless) holen sie samt Kontext ab, fragen zurück, was zu tun
  ist, und schreiben Ergebnisse in Vokabeln/Wissen/Chat zurück

## Architektur

```
static/           Reader-UI (vanilla HTML/CSS/JS)
server.py         stdlib-HTTP-Server + SQLite + Claude-CLI-Bridge
data/lector.db    gesamter Zustand (gläsern, per sqlite3 abfragbar)
data/events.jsonl Live-Eventstrom (Seitenwechsel, Lookups, Marks, Chats)
.claude/skills/lector   Mitlese-Skill für Claude-Code-Sessions
```

Die App ist bewusst „gläsern": Eine Claude-Code-Session kann über
`GET /api/observe`, die SQLite-DB oder den Eventstrom jederzeit mitlesen und
über die `chat`-Tabelle in den Reader hineinschreiben — das ist der
Beobachter-/Lehrer-Modus (`/lector`-Skill).
