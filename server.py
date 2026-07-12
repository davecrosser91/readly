#!/usr/bin/env python3
"""Lector — AI-assisted reading for language learning.

Stdlib-only local server: SQLite state, EPUB/TXT import, and a Claude-CLI
bridge so all AI features run over the user's Claude subscription (no API key).
Everything the app knows lives in data/lector.db and data/events.jsonl so an
observing Claude Code session can read along at any time.
"""

import base64
import json
import os
import posixpath
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
STATIC_DIR = os.path.join(ROOT, "static")
BOOKS_DIR = os.path.join(ROOT, "books")
DB_PATH = os.path.join(DATA_DIR, "lector.db")
EVENTS_PATH = os.path.join(DATA_DIR, "events.jsonl")
PORT = int(os.environ.get("LECTOR_PORT", "8123"))
CLAUDE_BIN = os.environ.get("LECTOR_CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("LECTOR_MODEL", "")  # empty = CLI default

PAGE_WINDOW = 6  # paragraphs before/after the current one that count as "page"

# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  author TEXT DEFAULT '',
  language TEXT DEFAULT 'es',
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS chapters (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL REFERENCES books(id),
  idx INTEGER NOT NULL,
  title TEXT DEFAULT '',
  content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
  book_id INTEGER PRIMARY KEY REFERENCES books(id),
  chapter_idx INTEGER DEFAULT 0,
  para_idx INTEGER DEFAULT 0,
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS vocab (
  id INTEGER PRIMARY KEY,
  book_id INTEGER,
  word TEXT NOT NULL,
  explanation TEXT DEFAULT '',
  sentence TEXT DEFAULT '',
  language TEXT DEFAULT 'es',
  reps INTEGER DEFAULT 0,
  interval_days REAL DEFAULT 0,
  due_at TEXT DEFAULT (datetime('now')),
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS marks (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL,
  chapter_idx INTEGER DEFAULT 0,
  kind TEXT NOT NULL,               -- 'word' | 'passage'
  text TEXT NOT NULL,
  active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL,
  role TEXT NOT NULL,               -- 'user' | 'assistant'
  content TEXT NOT NULL,
  context_json TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS summaries (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL,
  chapter_idx INTEGER NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(book_id, chapter_idx)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts TEXT DEFAULT (datetime('now')),
  book_id INTEGER,
  type TEXT NOT NULL,
  payload TEXT DEFAULT ''
);
"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def log_event(book_id, etype, payload):
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "book_id": book_id,
             "type": etype, "payload": payload}
    with db() as conn:
        conn.execute("INSERT INTO events (book_id, type, payload) VALUES (?,?,?)",
                     (book_id, etype, json.dumps(payload, ensure_ascii=False)))
    with open(EVENTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- claude bridge

claude_semaphore = threading.Semaphore(2)


def ask_claude(prompt, system=None, timeout=240):
    """Run a prompt through the Claude CLI (subscription, no API key)."""
    cmd = [CLAUDE_BIN, "-p", "--output-format", "text", "--strict-mcp-config"]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    if system:
        cmd += ["--append-system-prompt", system]
    with claude_semaphore:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=timeout, cwd=DATA_DIR,
        )
    if proc.returncode != 0:
        raise RuntimeError("claude CLI failed: " + proc.stderr.strip()[:500])
    return proc.stdout.strip()


TEACHER_SYSTEM = (
    "Du bist Lector, ein persönlicher Lese- und Sprachlehrer. David (Muttersprache "
    "Deutsch) verbessert sein Spanisch und Englisch durch Lesen. Antworte auf "
    "Deutsch, außer er bittet dich, in der Zielsprache zu bleiben. Didaktik: "
    "Erkläre Nuancen und Synonyme statt nur zu übersetzen, nutze Beispielsätze in "
    "der Zielsprache, stelle gelegentlich eine kurze Rückfrage, um Verständnis zu "
    "prüfen. Sei prägnant — das ist eine Sidebar neben einem Buch, keine Vorlesung. "
    "Kein Meta-Kommentar über deinen Kontext; nutze ihn einfach."
)


# ---------------------------------------------------------------- import: epub/txt

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "head"}
    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "br", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.heading = None
        self._skip_depth = 0
        self._in_heading = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3") and self.heading is None:
            self._in_heading = ""

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3") and self._in_heading is not None:
            text = self._in_heading.strip()
            if text and self.heading is None:
                self.heading = text
            self._in_heading = None

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.parts.append(data)
        if self._in_heading is not None:
            self._in_heading += data


def html_to_text(html):
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    text = "".join(p.parts)
    paras = [re.sub(r"\s+", " ", x).strip() for x in text.split("\n")]
    paras = [x for x in paras if x]
    return "\n\n".join(paras), p.heading


def parse_epub(raw):
    """Return list of (title, content) chapters from EPUB bytes."""
    zf = zipfile.ZipFile(BytesIO(raw))
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    opf_path = container.find(".//c:rootfile", ns).get("full-path")
    opf_dir = posixpath.dirname(opf_path)
    opf = ET.fromstring(zf.read(opf_path))
    ons = {"o": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}

    meta_title = opf.findtext(".//dc:title", default="", namespaces=ons)
    meta_author = opf.findtext(".//dc:creator", default="", namespaces=ons)
    meta_lang = (opf.findtext(".//dc:language", default="", namespaces=ons) or "")[:2]

    manifest = {}
    for item in opf.findall(".//o:manifest/o:item", ons):
        manifest[item.get("id")] = (item.get("href"), item.get("media-type", ""))

    chapters = []
    for itemref in opf.findall(".//o:spine/o:itemref", ons):
        href, mtype = manifest.get(itemref.get("idref"), (None, ""))
        if not href or "html" not in mtype:
            continue
        path = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
        try:
            html = zf.read(path).decode("utf-8", errors="replace")
        except KeyError:
            continue
        content, heading = html_to_text(html)
        if len(content) < 200:  # skip covers, title pages
            continue
        chapters.append((heading or "Kapitel %d" % (len(chapters) + 1), content))
    return {"title": meta_title, "author": meta_author, "language": meta_lang}, chapters


CHAPTER_RE = re.compile(
    r"^(cap[ií]tulo|chapter|kapitel|parte|part)\s+([0-9ivxlc]+|[a-záéíóúü]+)\b.*$",
    re.IGNORECASE,
)


def parse_txt(text):
    """Split plain text into chapters by heading lines, else by size."""
    lines = text.replace("\r\n", "\n").split("\n")
    chapters, current_title, current = [], None, []

    def flush():
        body = "\n".join(current).strip()
        paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", body)]
        paras = [p for p in paras if p]
        if paras:
            chapters.append((current_title or "Kapitel %d" % (len(chapters) + 1),
                             "\n\n".join(paras)))

    for line in lines:
        if CHAPTER_RE.match(line.strip()):
            flush()
            current_title, current = line.strip(), []
        else:
            current.append(line)
    flush()

    if len(chapters) <= 1 and chapters:
        # no headings found: chunk into ~2500-word chapters
        words = chapters[0][1].split(" ")
        if len(words) > 4000:
            chapters = []
            paras = chapters or []
            text_paras = re.split(r"\n\n", " ".join(words))
            chunk, count, out = [], 0, []
            for p in text_paras:
                chunk.append(p)
                count += len(p.split())
                if count >= 2500:
                    out.append(("Teil %d" % (len(out) + 1), "\n\n".join(chunk)))
                    chunk, count = [], 0
            if chunk:
                out.append(("Teil %d" % (len(out) + 1), "\n\n".join(chunk)))
            chapters = out
    return chapters


def import_book(filename, raw, title=None, author=None, language=None):
    if filename.lower().endswith(".epub"):
        meta, chapters = parse_epub(raw)
        title = title or meta["title"] or filename
        author = author or meta["author"]
        language = language or meta["language"] or "es"
    else:
        text = raw.decode("utf-8", errors="replace")
        chapters = parse_txt(text)
        title = title or os.path.splitext(os.path.basename(filename))[0]
        author = author or ""
        language = language or "es"
    if not chapters:
        raise ValueError("Keine Kapitel gefunden — Datei leer oder Format unbekannt.")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO books (title, author, language) VALUES (?,?,?)",
            (title, author, language))
        book_id = cur.lastrowid
        for idx, (ctitle, content) in enumerate(chapters):
            conn.execute(
                "INSERT INTO chapters (book_id, idx, title, content) VALUES (?,?,?,?)",
                (book_id, idx, ctitle, content))
        conn.execute("INSERT OR REPLACE INTO positions (book_id) VALUES (?)", (book_id,))
    log_event(book_id, "book_imported", {"title": title, "chapters": len(chapters)})
    return book_id


# ---------------------------------------------------------------- summaries

summary_queue = queue.Queue()


def summarizer_loop():
    while True:
        book_id, chapter_idx = summary_queue.get()
        try:
            with db() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM summaries WHERE book_id=? AND chapter_idx=?",
                    (book_id, chapter_idx)).fetchone()
                if exists:
                    continue
                row = conn.execute(
                    "SELECT c.title, c.content, b.language, b.title AS btitle FROM chapters c "
                    "JOIN books b ON b.id=c.book_id WHERE c.book_id=? AND c.idx=?",
                    (book_id, chapter_idx)).fetchone()
            if not row:
                continue
            content = row["content"]
            if len(content) > 24000:
                content = content[:12000] + "\n[...]\n" + content[-12000:]
            prompt = (
                "Fasse dieses Kapitel in 4-6 deutschen Sätzen zusammen (Handlung, "
                "Figuren, offene Fragen). Nur die Zusammenfassung, kein Vorspann.\n\n"
                "Buch: %s\nKapitel: %s\n\n%s" % (row["btitle"], row["title"], content))
            summary = ask_claude(prompt, timeout=300)
            with db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO summaries (book_id, chapter_idx, content) "
                    "VALUES (?,?,?)", (book_id, chapter_idx, summary))
            log_event(book_id, "summary_created", {"chapter_idx": chapter_idx})
        except Exception as e:
            print("[summarizer]", e, file=sys.stderr)
        finally:
            summary_queue.task_done()


def request_summaries_upto(book_id, chapter_idx):
    with db() as conn:
        have = {r["chapter_idx"] for r in conn.execute(
            "SELECT chapter_idx FROM summaries WHERE book_id=?", (book_id,))}
    for i in range(chapter_idx):
        if i not in have:
            summary_queue.put((book_id, i))


# ---------------------------------------------------------------- context assembly

def get_paragraphs(book_id, chapter_idx):
    with db() as conn:
        row = conn.execute(
            "SELECT content FROM chapters WHERE book_id=? AND idx=?",
            (book_id, chapter_idx)).fetchone()
    return row["content"].split("\n\n") if row else []


def build_context(book_id, chapter_idx, para_idx):
    """Marks + current page + chapter/book summaries — the standing chat context."""
    with db() as conn:
        book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        chapter = conn.execute(
            "SELECT title FROM chapters WHERE book_id=? AND idx=?",
            (book_id, chapter_idx)).fetchone()
        marks = conn.execute(
            "SELECT kind, text FROM marks WHERE book_id=? AND active=1 "
            "ORDER BY created_at DESC LIMIT 20", (book_id,)).fetchall()
        summaries = conn.execute(
            "SELECT chapter_idx, content FROM summaries WHERE book_id=? AND chapter_idx<? "
            "ORDER BY chapter_idx", (book_id, chapter_idx)).fetchall()

    paras = get_paragraphs(book_id, chapter_idx)
    lo = max(0, para_idx - PAGE_WINDOW)
    hi = min(len(paras), para_idx + PAGE_WINDOW + 1)
    page_text = "\n\n".join(paras[lo:hi])
    if len(page_text) > 9000:
        page_text = page_text[:9000] + " [...]"

    lang_name = {"es": "Spanisch", "en": "Englisch"}.get(book["language"], book["language"])
    parts = ["# Lesekontext",
             "Buch: %s — %s (Zielsprache: %s)" % (book["title"], book["author"] or "unbekannt", lang_name),
             "Aktuelles Kapitel: %s (Kapitel %d)" % (chapter["title"] if chapter else "?", chapter_idx + 1)]
    if summaries:
        parts.append("\n## Die Geschichte bisher (frühere Kapitel)")
        for s in summaries:
            parts.append("Kapitel %d: %s" % (s["chapter_idx"] + 1, s["content"]))
    parts.append("\n## Aktuelle Seite (um Davids Leseposition)\n" + page_text)
    word_marks = [m["text"] for m in marks if m["kind"] == "word"]
    passage_marks = [m["text"] for m in marks if m["kind"] == "passage"]
    if word_marks:
        parts.append("\n## Von David markierte Wörter\n" + ", ".join(word_marks))
    if passage_marks:
        parts.append("\n## Von David markierte Passagen")
        for p in passage_marks:
            parts.append("> " + p)
    return "\n".join(parts)


# ---------------------------------------------------------------- api handlers

def api_chat(body):
    book_id = body["book_id"]
    chapter_idx = int(body.get("chapter_idx", 0))
    para_idx = int(body.get("para_idx", 0))
    message = body["message"].strip()

    context = build_context(book_id, chapter_idx, para_idx)
    with db() as conn:
        history = conn.execute(
            "SELECT role, content FROM chat WHERE book_id=? ORDER BY id DESC LIMIT 8",
            (book_id,)).fetchall()
        conn.execute("INSERT INTO chat (book_id, role, content) VALUES (?,?,?)",
                     (book_id, "user", message))
    history = list(reversed(history))

    convo = ""
    if history:
        convo = "\n## Bisheriges Gespräch\n" + "\n".join(
            "%s: %s" % ("David" if h["role"] == "user" else "Lector", h["content"])
            for h in history)
    prompt = "%s\n%s\n\n## Davids Nachricht\n%s" % (context, convo, message)

    log_event(book_id, "chat_user", {"message": message})
    answer = ask_claude(prompt, system=TEACHER_SYSTEM)
    with db() as conn:
        conn.execute("INSERT INTO chat (book_id, role, content, context_json) VALUES (?,?,?,?)",
                     (book_id, "assistant", answer, json.dumps({"chapter_idx": chapter_idx})))
    log_event(book_id, "chat_assistant", {"message": answer[:200]})
    return {"answer": answer}


def api_lookup(body):
    book_id = body["book_id"]
    word = body["word"].strip()
    sentence = body.get("sentence", "").strip()
    with db() as conn:
        book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    lang_name = {"es": "Spanisch", "en": "Englisch"}.get(book["language"], book["language"])
    prompt = (
        "Erkläre kompakt für einen deutschen Lerner das %s-Wort \"%s\" in diesem Satz:\n"
        "\"%s\"\n\n"
        "Format (Markdown, max ~90 Wörter):\n"
        "**Bedeutung hier:** <deutsche Bedeutung im Satzkontext>\n"
        "**Synonyme & Nuancen:** <2-3 Synonyme in der Zielsprache, je 3-6 Wörter zur Abgrenzung>\n"
        "**Beispiel:** <ein neuer Beispielsatz in der Zielsprache>"
        % (lang_name, word, sentence))
    explanation = ask_claude(prompt)
    with db() as conn:
        conn.execute(
            "INSERT INTO vocab (book_id, word, explanation, sentence, language) "
            "VALUES (?,?,?,?,?)",
            (book_id, word, explanation, sentence, book["language"]))
        vocab_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    log_event(book_id, "lookup", {"word": word, "sentence": sentence})
    return {"explanation": explanation, "vocab_id": vocab_id}


def api_review_answer(body):
    grade = body["grade"]  # 'again' | 'good'
    with db() as conn:
        card = conn.execute("SELECT * FROM vocab WHERE id=?", (body["id"],)).fetchone()
        if not card:
            return {"ok": False}
        if grade == "good":
            interval = max(1.0, (card["interval_days"] or 0) * 2.2) if card["reps"] else 1.0
            due = "datetime('now', '+%d hours')" % int(interval * 24)
        else:
            interval = 0
            due = "datetime('now', '+10 minutes')"
        conn.execute(
            "UPDATE vocab SET reps=reps+1, interval_days=?, due_at=%s WHERE id=?" % due,
            (interval, body["id"]))
    return {"ok": True}


def api_observe():
    """Everything an observing Claude session needs in one call."""
    with db() as conn:
        books = [dict(r) for r in conn.execute(
            "SELECT b.*, p.chapter_idx, p.para_idx, p.updated_at AS last_read FROM books b "
            "LEFT JOIN positions p ON p.book_id=b.id ORDER BY p.updated_at DESC")]
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 50")]
        marks = [dict(r) for r in conn.execute(
            "SELECT * FROM marks WHERE active=1 ORDER BY id DESC LIMIT 50")]
        vocab = [dict(r) for r in conn.execute(
            "SELECT word, language, reps, created_at FROM vocab ORDER BY id DESC LIMIT 100")]
    current = books[0] if books else None
    page = None
    if current and current["chapter_idx"] is not None:
        paras = get_paragraphs(current["id"], current["chapter_idx"])
        pi = current["para_idx"] or 0
        page = "\n\n".join(paras[max(0, pi - 2):pi + 3])
    return {"books": books, "current_book": current, "current_page": page,
            "active_marks": marks, "recent_events": events, "recent_vocab": vocab}


# ---------------------------------------------------------------- http server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    def _json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        fpath = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not fpath.startswith(STATIC_DIR) or not os.path.isfile(fpath):
            self.send_error(404)
            return
        ctype = {"html": "text/html", "js": "application/javascript",
                 "css": "text/css", "svg": "image/svg+xml"}.get(
                     fpath.rsplit(".", 1)[-1], "application/octet-stream")
        with open(fpath, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    # ------------------------------------------------------------ GET
    def do_GET(self):
        path = self.path.split("?")[0]
        q = {}
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    q[k] = v
        try:
            if path == "/api/books":
                with db() as conn:
                    books = [dict(r) for r in conn.execute(
                        "SELECT b.*, p.chapter_idx, p.para_idx, "
                        "(SELECT COUNT(*) FROM chapters c WHERE c.book_id=b.id) AS n_chapters "
                        "FROM books b LEFT JOIN positions p ON p.book_id=b.id ORDER BY b.id")]
                return self._json(books)
            m = re.match(r"^/api/books/(\d+)$", path)
            if m:
                book_id = int(m.group(1))
                with db() as conn:
                    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
                    chapters = [dict(r) for r in conn.execute(
                        "SELECT idx, title FROM chapters WHERE book_id=? ORDER BY idx", (book_id,))]
                    pos = conn.execute("SELECT * FROM positions WHERE book_id=?", (book_id,)).fetchone()
                return self._json({"book": dict(book), "chapters": chapters,
                                   "position": dict(pos) if pos else None})
            m = re.match(r"^/api/books/(\d+)/chapters/(\d+)$", path)
            if m:
                paras = get_paragraphs(int(m.group(1)), int(m.group(2)))
                return self._json({"paragraphs": paras})
            if path == "/api/marks":
                with db() as conn:
                    marks = [dict(r) for r in conn.execute(
                        "SELECT * FROM marks WHERE book_id=? AND active=1 ORDER BY id",
                        (int(q.get("book_id", 0)),))]
                return self._json(marks)
            if path == "/api/chat":
                with db() as conn:
                    msgs = [dict(r) for r in conn.execute(
                        "SELECT id, role, content, created_at FROM chat WHERE book_id=? ORDER BY id",
                        (int(q.get("book_id", 0)),))]
                return self._json(msgs)
            if path == "/api/vocab":
                with db() as conn:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM vocab ORDER BY id DESC LIMIT 500")]
                return self._json(rows)
            if path == "/api/review/next":
                with db() as conn:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM vocab WHERE due_at <= datetime('now') "
                        "ORDER BY due_at LIMIT 20")]
                return self._json(rows)
            if path == "/api/observe":
                return self._json(api_observe())
            if path.startswith("/api/"):
                return self.send_error(404)
            return self._static(path)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/api/import":
                raw = base64.b64decode(body["data_b64"])
                book_id = import_book(body["filename"], raw,
                                      title=body.get("title"), author=body.get("author"),
                                      language=body.get("language"))
                return self._json({"book_id": book_id})
            if self.path == "/api/position":
                with db() as conn:
                    conn.execute(
                        "INSERT INTO positions (book_id, chapter_idx, para_idx, updated_at) "
                        "VALUES (?,?,?,datetime('now')) ON CONFLICT(book_id) DO UPDATE SET "
                        "chapter_idx=excluded.chapter_idx, para_idx=excluded.para_idx, "
                        "updated_at=excluded.updated_at",
                        (body["book_id"], body["chapter_idx"], body["para_idx"]))
                request_summaries_upto(body["book_id"], body["chapter_idx"])
                return self._json({"ok": True})
            if self.path == "/api/marks":
                with db() as conn:
                    cur = conn.execute(
                        "INSERT INTO marks (book_id, chapter_idx, kind, text) VALUES (?,?,?,?)",
                        (body["book_id"], body.get("chapter_idx", 0), body["kind"], body["text"]))
                    mark_id = cur.lastrowid
                log_event(body["book_id"], "mark_added",
                          {"kind": body["kind"], "text": body["text"][:120]})
                return self._json({"id": mark_id})
            m = re.match(r"^/api/marks/(\d+)/delete$", self.path)
            if m:
                with db() as conn:
                    conn.execute("UPDATE marks SET active=0 WHERE id=?", (int(m.group(1)),))
                return self._json({"ok": True})
            if self.path == "/api/chat":
                return self._json(api_chat(body))
            if self.path == "/api/lookup":
                return self._json(api_lookup(body))
            if self.path == "/api/review/answer":
                return self._json(api_review_answer(body))
            return self.send_error(404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)


# ---------------------------------------------------------------- seed & main

def seed():
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"]
    if n:
        return
    for fname, meta in [
        ("el_faro_del_sur.txt", {"title": "El faro del sur", "author": "Lector Sample",
                                 "language": "es"}),
        ("the_last_ferry.txt", {"title": "The Last Ferry", "author": "Lector Sample",
                                "language": "en"}),
    ]:
        path = os.path.join(BOOKS_DIR, fname)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                import_book(fname, f.read(), **meta)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
    seed()
    threading.Thread(target=summarizer_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Lector läuft auf http://127.0.0.1:%d" % PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
