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
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
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
HOST = os.environ.get("LECTOR_HOST", "127.0.0.1")  # "0.0.0.0" = im WLAN erreichbar
TOKEN_PATH = os.path.join(DATA_DIR, "token")
_TOKEN = None


def load_token():
    """API-Token für Remote-Zugriffe — beim ersten Start erzeugt, in data/."""
    global _TOKEN
    if _TOKEN is None:
        if os.path.isfile(TOKEN_PATH):
            with open(TOKEN_PATH, encoding="utf-8") as f:
                _TOKEN = f.read().strip()
        if not _TOKEN:
            _TOKEN = secrets.token_urlsafe(32)
            os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(_TOKEN + "\n")
            os.chmod(TOKEN_PATH, 0o600)
    return _TOKEN
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
CREATE TABLE IF NOT EXISTS review_log (
  id INTEGER PRIMARY KEY,
  vocab_id INTEGER NOT NULL,
  grade TEXT NOT NULL,              -- again | hard | good | easy
  interval_days REAL DEFAULT 0,     -- Intervall NACH der Antwort
  agent TEXT DEFAULT '',            -- '' = David in der UI
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY,
  book_id INTEGER,
  chapter_idx INTEGER DEFAULT 0,
  kind TEXT NOT NULL,               -- 'grammar' | 'idea' | 'content'
  content TEXT NOT NULL,
  source_text TEXT DEFAULT '',
  tags TEXT DEFAULT '',             -- kommagetrennt
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY,
  book_id INTEGER,
  type TEXT NOT NULL,               -- 'unclear_mark' | 'enrich_vocab'
  payload TEXT DEFAULT '{}',
  status TEXT DEFAULT 'pending',    -- pending | claimed | done | failed
  agent TEXT DEFAULT '',
  result TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now')),
  claimed_at TEXT
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


# guide types and filename stems that mark EPUB front/back matter, not reading text
FRONT_MATTER_GUIDE = {"cover", "title-page", "copyright-page", "toc"}
FRONT_MATTER_FILE = re.compile(
    r"(?:^|[_\-.])(cover|cubierta|portada|title|titulo|copyright|info|toc|nav|"
    r"indice|índice|sinopsis|synopsis)(?:[_\-.]|$)", re.IGNORECASE)


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

    def full_path(href):
        return posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href

    manifest = {}
    for item in opf.findall(".//o:manifest/o:item", ons):
        manifest[item.get("id")] = (item.get("href"), item.get("media-type", ""),
                                    item.get("properties", ""))

    skip_paths = {full_path(ref.get("href", "").split("#")[0])
                  for ref in opf.findall(".//o:guide/o:reference", ons)
                  if ref.get("type") in FRONT_MATTER_GUIDE}

    chapters = []
    for itemref in opf.findall(".//o:spine/o:itemref", ons):
        href, mtype, props = manifest.get(itemref.get("idref"), (None, "", ""))
        if not href or "html" not in mtype:
            continue
        path = full_path(href)
        stem = posixpath.splitext(posixpath.basename(path))[0]
        if path in skip_paths or "nav" in props.split() or FRONT_MATTER_FILE.search(stem):
            continue
        try:
            html = zf.read(path).decode("utf-8", errors="replace")
        except KeyError:
            continue
        content, heading = html_to_text(html)
        if len(content) < 200:  # skip covers, title pages
            continue
        if heading:  # "1DESTINO" → "1 DESTINO" (number glued to title by inline markup)
            heading = re.sub(r"^(\d+)(?=[^\s\d.,:)])", r"\1 ", heading)
        chapters.append((heading or "Chapter %d" % (len(chapters) + 1), content))
    return {"title": meta_title, "author": meta_author, "language": meta_lang}, chapters


def _clean_pdf_text(text):
    """pdftotext output → readable paragraphs."""
    text = text.replace("\r\n", "\n")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # de-hyphenate line breaks
    text = re.sub(r"^\s*\d+\s*$", "", text, flags=re.M)   # bare page numbers
    text = text.replace("\f", "\n\n")                      # page breaks → paragraph
    # chapter headings must survive as their own paragraph
    text = re.sub(r"(?mi)^[ \t]*((?:cap[ií]tulo|chapter|kapitel|parte|part)\s+\S{1,8}[ \t]*)$",
                  r"\n\1\n", text)
    # if the PDF has almost no blank lines, fall back to sentence-end paragraphing
    blanks = len(re.findall(r"\n\s*\n", text))
    if blanks < len(text) / 3000:
        text = re.sub(r"([.!?…»”\"])\n(?=[A-ZÁÉÍÓÚÜÑ¿¡«“])", r"\1\n\n", text)
    # join wrapped lines inside paragraphs
    paras = re.split(r"\n\s*\n", text)
    paras = [re.sub(r"\s*\n\s*", " ", p).strip() for p in paras]
    return "\n\n".join(p for p in paras if p)


def parse_pdf(raw, language="es"):
    """Extract text from a PDF via pdftotext; OCR fallback for scanned PDFs."""
    import shutil
    import tempfile
    if not shutil.which("pdftotext"):
        raise ValueError("pdftotext not found — install poppler (brew install poppler).")
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp:
        pdf_path = os.path.join(tmp, "book.pdf")
        with open(pdf_path, "wb") as f:
            f.write(raw)
        proc = subprocess.run(["pdftotext", pdf_path, "-"],
                              capture_output=True, text=True, timeout=300)
        text = proc.stdout or ""
        if len(text.strip()) < 500:
            # likely a scanned PDF without a text layer → OCR (slow, one-time)
            if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
                raise ValueError("PDF has no text layer and no OCR tools found "
                                 "(brew install poppler tesseract).")
            lang = {"es": "spa", "en": "eng", "de": "deu"}.get(language, "eng")
            subprocess.run(["pdftoppm", "-r", "200", "-gray", pdf_path,
                            os.path.join(tmp, "pg")], check=True, timeout=1800)
            pages = sorted(f for f in os.listdir(tmp) if f.startswith("pg"))
            parts = []
            for i, page in enumerate(pages):
                ocr = subprocess.run(
                    ["tesseract", os.path.join(tmp, page), "-", "-l", lang],
                    capture_output=True, text=True, timeout=120)
                parts.append(ocr.stdout)
                print("[ocr] page %d/%d" % (i + 1, len(pages)), file=sys.stderr)
            text = "\n\f\n".join(parts)
    return _clean_pdf_text(text)


CHAPTER_RE = re.compile(
    r"^(cap[ií]tulo|chapter|kapitel|parte|part)\s+([0-9ivxlc]+|[a-záéíóúü]+)\b.{0,60}$",
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
            chapters.append((current_title or "Chapter %d" % (len(chapters) + 1),
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
                    out.append(("Part %d" % (len(out) + 1), "\n\n".join(chunk)))
                    chunk, count = [], 0
            if chunk:
                out.append(("Part %d" % (len(out) + 1), "\n\n".join(chunk)))
            chapters = out
    return chapters


def import_book(filename, raw, title=None, author=None, language=None):
    if filename.lower().endswith(".epub"):
        meta, chapters = parse_epub(raw)
        title = title or meta["title"] or filename
        author = author or meta["author"]
        language = language or meta["language"] or "es"
    elif filename.lower().endswith(".pdf"):
        text = parse_pdf(raw, language=language or "es")
        chapters = parse_txt(text)
        title = title or os.path.splitext(os.path.basename(filename))[0]
        author = author or ""
        language = language or "es"
    else:
        text = raw.decode("utf-8", errors="replace")
        chapters = parse_txt(text)
        title = title or os.path.splitext(os.path.basename(filename))[0]
        author = author or ""
        language = language or "es"
    if not chapters:
        raise ValueError("No chapters found — file empty or unknown format.")
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
            "INSERT INTO vocab (book_id, word, explanation, sentence, language, chapter_idx) "
            "VALUES (?,?,?,?,?,?)",
            (book_id, word, explanation, sentence, book["language"],
             body.get("chapter_idx")))
        vocab_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    log_event(book_id, "lookup", {"word": word, "sentence": sentence})
    return {"explanation": explanation, "vocab_id": vocab_id}


def api_review_answer(body):
    grade = body["grade"]  # 'again' | 'hard' | 'good' | 'easy' (Anki-Stufen)
    with db() as conn:
        card = conn.execute("SELECT * FROM vocab WHERE id=?", (body["id"],)).fetchone()
        if not card:
            return {"ok": False}
        iv = card["interval_days"] or 0
        first = not card["reps"]
        if grade == "again":
            interval, due = 0, "datetime('now', '+10 minutes')"
        else:
            if grade == "hard":
                interval = 0.5 if first else max(0.5, iv * 1.2)
            elif grade == "easy":
                interval = 3.0 if first else max(2.0, iv * 3.2)
            else:  # good
                interval = 1.0 if first else max(1.0, iv * 2.2)
            due = "datetime('now', '+%d hours')" % int(interval * 24)
        conn.execute(
            "UPDATE vocab SET reps=reps+1, interval_days=?, due_at=%s WHERE id=?" % due,
            (interval, body["id"]))
        conn.execute(
            "INSERT INTO review_log (vocab_id, grade, interval_days, agent) VALUES (?,?,?,?)",
            (body["id"], grade, interval, body.get("agent", "")))
    return {"ok": True}


VOCAB_FIELDS = {"word", "sentence", "explanation", "language",
                "due_at", "interval_days", "reps", "chapter_idx"}


def api_vocab_update(vocab_id, body):
    """Voller Schreibzugriff für Agenten: Inhalt UND SRS-Zustand."""
    fields = {k: v for k, v in body.items() if k in VOCAB_FIELDS}
    if not fields:
        return {"ok": False, "error": "keine gültigen Felder"}
    sets = ", ".join("%s=?" % k for k in fields)
    with db() as conn:
        cur = conn.execute("UPDATE vocab SET %s WHERE id=?" % sets,
                           (*fields.values(), vocab_id))
    log_event(None, "vocab_updated", {"id": vocab_id, "fields": list(fields)})
    return {"ok": cur.rowcount == 1}


def api_review_stats():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) n FROM vocab").fetchone()["n"]
        due = conn.execute(
            "SELECT COUNT(*) n FROM vocab WHERE due_at <= datetime('now')").fetchone()["n"]
        learned = conn.execute("SELECT COUNT(*) n FROM vocab WHERE reps > 0").fetchone()["n"]
        mature = conn.execute(
            "SELECT COUNT(*) n FROM vocab WHERE interval_days >= 7").fetchone()["n"]
        last7 = [dict(r) for r in conn.execute(
            "SELECT date(created_at) day, COUNT(*) reviews, "
            "SUM(CASE WHEN grade='again' THEN 1 ELSE 0 END) lapses "
            "FROM review_log WHERE created_at > datetime('now','-7 days') "
            "GROUP BY day ORDER BY day")]
        hardest = [dict(r) for r in conn.execute(
            "SELECT v.id, v.word, COUNT(*) lapses FROM review_log l "
            "JOIN vocab v ON v.id=l.vocab_id WHERE l.grade='again' "
            "GROUP BY v.id ORDER BY lapses DESC LIMIT 10")]
    return {"total": total, "due": due, "learned": learned, "mature": mature,
            "last_7_days": last7, "hardest_words": hardest}


def api_quick_vocab(body):
    """Sofort speichern, Erklärung später durch einen Agenten (enrich_vocab)."""
    book_id = body.get("book_id")
    word = body["word"].strip()
    sentence = body.get("sentence", "").strip()
    language = body.get("language", "")
    if book_id and not language:
        with db() as conn:
            b = conn.execute("SELECT language FROM books WHERE id=?", (book_id,)).fetchone()
            language = b["language"] if b else "es"
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO vocab (book_id, word, explanation, sentence, language, chapter_idx) "
            "VALUES (?,?,?,?,?,?)",
            (book_id, word, "", sentence, language or "es", body.get("chapter_idx")))
        vocab_id = cur.lastrowid
        conn.execute(
            "INSERT INTO actions (book_id, type, payload) VALUES (?,?,?)",
            (book_id, "enrich_vocab",
             json.dumps({"vocab_id": vocab_id, "word": word, "sentence": sentence},
                        ensure_ascii=False)))
    log_event(book_id, "vocab_quick_add", {"word": word})
    return {"vocab_id": vocab_id}


def mark_context(book_id, chapter_idx, text, span=3):
    """Absätze vor/nach der Fundstelle einer Markierung."""
    paras = get_paragraphs(book_id, chapter_idx)
    key = text[:60].lower()
    idx = next((i for i, p in enumerate(paras) if key in p.lower()), None)
    if idx is None:
        with db() as conn:
            pos = conn.execute("SELECT para_idx FROM positions WHERE book_id=?",
                               (book_id,)).fetchone()
        idx = min(pos["para_idx"] if pos else 0, max(0, len(paras) - 1))
    return {
        "before": paras[max(0, idx - span):idx],
        "containing": paras[idx] if paras else "",
        "after": paras[idx + 1:idx + 1 + span],
        "para_idx": idx,
    }


def api_agent_next(q):
    """Älteste offene Aktion + alles, was ein Agent an Kontext braucht."""
    with db() as conn:
        conn.execute(  # verwaiste Claims wieder freigeben
            "UPDATE actions SET status='pending' WHERE status='claimed' "
            "AND claimed_at < datetime('now','-5 minutes')")
        sql = "SELECT * FROM actions WHERE status='pending'"
        args = []
        if q.get("types"):
            types = q["types"].split(",")
            sql += " AND type IN (%s)" % ",".join("?" * len(types))
            args += types
        row = conn.execute(sql + " ORDER BY id LIMIT 1", args).fetchone()
    if not row:
        return {"action": None}
    action = dict(row)
    payload = json.loads(action["payload"] or "{}")
    action["payload"] = payload
    book_id = action["book_id"]
    context = {}
    if book_id:
        with db() as conn:
            book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
            summaries = [dict(r) for r in conn.execute(
                "SELECT chapter_idx, content FROM summaries WHERE book_id=? ORDER BY chapter_idx",
                (book_id,))]
            vocab = [r["word"] for r in conn.execute(
                "SELECT word FROM vocab WHERE book_id=? ORDER BY id DESC LIMIT 40", (book_id,))]
        chapter_idx = payload.get("chapter_idx", 0)
        with db() as conn:
            ch = conn.execute("SELECT title FROM chapters WHERE book_id=? AND idx=?",
                              (book_id, chapter_idx)).fetchone()
        context = {
            "book": dict(book) if book else None,
            "chapter_idx": chapter_idx,
            "chapter_title": ch["title"] if ch else "",
            "chapter_summaries": summaries,
            "known_vocab": vocab,
        }
        text = payload.get("text") or payload.get("sentence") or ""
        if text:
            context["passage"] = mark_context(book_id, chapter_idx, text)
    return {"action": action, "context": context}


def api_agent_complete(body):
    action_id = body["action_id"]
    result = body.get("result", {})
    with db() as conn:
        row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "unknown action"}
        payload = json.loads(row["payload"] or "{}")
        if row["type"] == "enrich_vocab" and result.get("explanation"):
            conn.execute("UPDATE vocab SET explanation=? WHERE id=?",
                         (result["explanation"], payload.get("vocab_id")))
        conn.execute(
            "UPDATE actions SET status=?, result=?, agent=COALESCE(NULLIF(?,''),agent) "
            "WHERE id=?",
            (body.get("status", "done"), json.dumps(result, ensure_ascii=False),
             body.get("agent", ""), action_id))
    log_event(row["book_id"], "action_completed",
              {"action_id": action_id, "type": row["type"]})
    return {"ok": True}


def api_locate(q):
    """Textstelle im Buch finden → (chapter_idx, para_idx) für Sprung-Referenzen."""
    book_id = int(q.get("book_id", 0))
    key = (q.get("text") or "").strip()[:60].lower()
    if not key:
        return {"found": False}
    hint = q.get("chapter_idx")
    with db() as conn:
        chapters = conn.execute(
            "SELECT idx, content FROM chapters WHERE book_id=? ORDER BY idx",
            (book_id,)).fetchall()
    if hint not in (None, ""):
        chapters = sorted(chapters, key=lambda c: 0 if c["idx"] == int(hint) else 1)
    for ch in chapters:
        for i, p in enumerate(ch["content"].split("\n\n")):
            if key in p.lower():
                return {"found": True, "chapter_idx": ch["idx"], "para_idx": i}
    return {"found": False}


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


# ---------------------------------------------------------------- openapi (v0.2c)

def openapi_spec(base_url):
    """Agent-facing API als OpenAPI 3.1 — für Custom-GPT-Actions & Co."""
    def op(summary, params=None, body=None, tag="lector"):
        o = {"summary": summary, "tags": [tag],
             "responses": {"200": {"description": "OK"}}}
        if params:
            o["parameters"] = [
                {"name": n, "in": "query", "required": req,
                 "schema": {"type": t}} for n, t, req in params]
        if body:
            o["requestBody"] = {"required": True, "content": {"application/json": {
                "schema": {"type": "object", "properties": {
                    k: {"type": t} for k, t in body.items()},
                    "additionalProperties": True}}}}
        return o

    return {
        "openapi": "3.1.0",
        "info": {"title": "Lector", "version": "0.2c",
                 "description": "Local-first AI reader — agent API. Auth: "
                                "Bearer-Token (data/token) für Remote-Zugriffe."},
        "servers": [{"url": base_url}],
        "security": [{"bearerAuth": []}],
        "components": {"securitySchemes": {"bearerAuth": {
            "type": "http", "scheme": "bearer"}}},
        "paths": {
            "/api/books": {"get": op("Alle Bücher mit Leseposition")},
            "/api/books/{id}": {"get": {
                "summary": "Buch mit Kapitelliste und Position", "tags": ["lector"],
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "OK"}}}},
            "/api/books/{id}/chapters/{idx}": {"get": {
                "summary": "Absätze eines Kapitels", "tags": ["lector"],
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "integer"}},
                    {"name": "idx", "in": "path", "required": True,
                     "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "OK"}}}},
            "/api/agent/next": {"get": op(
                "Älteste offene Aktion + kompletter Session-Kontext (Buch, "
                "Leseposition, Marks, Summaries, Vokabeln)",
                params=[("types", "string", False)])},
            "/api/agent/claim": {"post": op(
                "Aktion claimen (5-min-Timeout)",
                body={"action_id": "integer", "agent": "string"})},
            "/api/agent/complete": {"post": op(
                "Aktion abschließen; result wird verarbeitet",
                body={"action_id": "integer", "result": "object"})},
            "/api/agent/push": {"post": op(
                "Proaktive Nachricht in den Buch-Chat pushen",
                body={"book_id": "integer", "content": "string"})},
            "/api/vocab": {
                "get": op("Vokabeln (neueste zuerst, max 500)"),
                "post": op("Vokabel schnell anlegen",
                           body={"book_id": "integer", "word": "string",
                                 "sentence": "string", "chapter_idx": "integer"})},
            "/api/notes": {"get": op("Wissens-Notizen",
                                     params=[("book_id", "string", False)])},
            "/api/review/next": {"get": op("Fällige Anki-Karten (max 20)")},
            "/api/review/stats": {"get": op("Anki-Statistik")},
            "/api/locate": {"get": op(
                "Textstelle im Buch finden",
                params=[("book_id", "integer", True), ("text", "string", True),
                        ("chapter_idx", "string", False)])},
        },
    }


# ---------------------------------------------------------------- http server

LOCAL_ADDRS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    # -------------------------------------------------------- auth (v0.2c)
    def _authorized(self):
        """Localhost direkt = frei. Alles andere (LAN, Tunnel via
        X-Forwarded-For) braucht den Token: Bearer-Header, Cookie oder
        einmalig ?token=… (setzt dann ein Cookie für die UI)."""
        self._grant_cookie = False
        if (self.client_address[0] in LOCAL_ADDRS
                and "X-Forwarded-For" not in self.headers):
            return True
        token = load_token()
        if self.headers.get("Authorization", "") == "Bearer " + token:
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "lector_token" in cookie and cookie["lector_token"].value == token:
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("token", [""])[0] == token:
            self._grant_cookie = True
            return True
        return False

    def _send_cookie_if_granted(self):
        if getattr(self, "_grant_cookie", False):
            self.send_header("Set-Cookie",
                             "lector_token=%s; Path=/; Max-Age=31536000; "
                             "SameSite=Lax" % load_token())

    def _json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._send_cookie_if_granted()
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
        self._send_cookie_if_granted()
        self.end_headers()
        self.wfile.write(raw)

    # ------------------------------------------------------------ GET
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/openapi.json":  # öffentlich: beschreibt nur die API-Form
            return self._json(openapi_spec(self._base_url()))
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        q = {}
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    q[k] = urllib.parse.unquote_plus(v)
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
            if path == "/api/review/stats":
                return self._json(api_review_stats())
            if path == "/api/review/log":
                with db() as conn:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT l.*, v.word FROM review_log l LEFT JOIN vocab v "
                        "ON v.id=l.vocab_id ORDER BY l.id DESC LIMIT 200")]
                return self._json(rows)
            if path == "/api/notes":
                with db() as conn:
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM notes WHERE (?='' OR book_id=?) ORDER BY id DESC",
                        (q.get("book_id", ""), q.get("book_id", "")))]
                return self._json(rows)
            if path == "/api/actions":
                with db() as conn:
                    sql = "SELECT * FROM actions WHERE 1=1"
                    args = []
                    if q.get("book_id"):
                        sql += " AND book_id=?"
                        args.append(q["book_id"])
                    if q.get("status"):
                        sql += " AND status=?"
                        args.append(q["status"])
                    rows = [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT 100", args)]
                return self._json(rows)
            if path == "/api/agent/next":
                return self._json(api_agent_next(q))
            if path == "/api/locate":
                return self._json(api_locate(q))
            if path == "/api/observe":
                return self._json(api_observe())
            if path.startswith("/api/"):
                return self.send_error(404)
            return self._static(path)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def _base_url(self):
        proto = self.headers.get("X-Forwarded-Proto", "http")
        host = self.headers.get("X-Forwarded-Host",
                                self.headers.get("Host", "localhost:%d" % PORT))
        return "%s://%s" % (proto, host)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
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
                    # jede Markierung = "mir unklar" → Outbox für andockende Agenten
                    conn.execute(
                        "INSERT INTO actions (book_id, type, payload) VALUES (?,?,?)",
                        (body["book_id"], "unclear_mark",
                         json.dumps({"mark_id": mark_id, "kind": body["kind"],
                                     "text": body["text"],
                                     "chapter_idx": body.get("chapter_idx", 0)},
                                    ensure_ascii=False)))
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
            if self.path == "/api/vocab":
                return self._json(api_quick_vocab(body))
            m = re.match(r"^/api/vocab/(\d+)/update$", self.path)
            if m:
                return self._json(api_vocab_update(int(m.group(1)), body))
            m = re.match(r"^/api/vocab/(\d+)/delete$", self.path)
            if m:
                with db() as conn:
                    conn.execute("DELETE FROM vocab WHERE id=?", (int(m.group(1)),))
                return self._json({"ok": True})
            if self.path == "/api/notes":
                with db() as conn:
                    cur = conn.execute(
                        "INSERT INTO notes (book_id, chapter_idx, kind, content, source_text, tags) "
                        "VALUES (?,?,?,?,?,?)",
                        (body.get("book_id"), body.get("chapter_idx", 0), body["kind"],
                         body["content"], body.get("source_text", ""),
                         body.get("tags", "")))
                    note_id = cur.lastrowid
                log_event(body.get("book_id"), "note_added",
                          {"kind": body["kind"], "tags": body.get("tags", "")})
                return self._json({"id": note_id})
            m = re.match(r"^/api/notes/(\d+)/delete$", self.path)
            if m:
                with db() as conn:
                    conn.execute("DELETE FROM notes WHERE id=?", (int(m.group(1)),))
                return self._json({"ok": True})
            if self.path == "/api/agent/claim":
                with db() as conn:
                    cur = conn.execute(
                        "UPDATE actions SET status='claimed', agent=?, claimed_at=datetime('now') "
                        "WHERE id=? AND status='pending'",
                        (body.get("agent", "unknown"), body["action_id"]))
                return self._json({"ok": cur.rowcount == 1})
            if self.path == "/api/agent/complete":
                return self._json(api_agent_complete(body))
            if self.path == "/api/agent/push":
                with db() as conn:
                    conn.execute(
                        "INSERT INTO chat (book_id, role, content) VALUES (?,?,?)",
                        (body["book_id"], "assistant", body["content"]))
                log_event(body["book_id"], "agent_push",
                          {"content": body["content"][:120]})
                return self._json({"ok": True})
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
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(vocab)")]
        if "chapter_idx" not in cols:
            conn.execute("ALTER TABLE vocab ADD COLUMN chapter_idx INTEGER")
    seed()
    threading.Thread(target=summarizer_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Lector running at http://%s:%d" % (HOST, PORT))
    print("API token (remote/LAN): %s  — Datei: data/token" % load_token())
    server.serve_forever()


if __name__ == "__main__":
    main()
