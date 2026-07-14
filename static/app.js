/* Lector frontend — vanilla JS, talks to the local server API. */

const $ = (sel) => document.querySelector(sel);
const api = {
  get: (url) => fetch(url).then((r) => r.json()),
  post: (url, body) =>
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(async (r) => {
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || r.statusText);
      return data;
    }),
};

/* ---------------- Theme */
const savedTheme = localStorage.getItem("lector-theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
function toggleTheme() {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("lector-theme", next);
}
document.addEventListener("DOMContentLoaded", () => {
  ["#themeToggle", "#themeToggle2"].forEach((s) => {
    const b = document.querySelector(s);
    if (b) b.onclick = toggleTheme;
  });
});

const state = {
  book: null,          // {book, chapters, position}
  chapterIdx: 0,
  paraIdx: 0,
  marks: [],
  currentWord: null,   // {word, sentence, el}
};

const LANG_NAMES = { es: "Spanish", en: "English" };

/* ---------------- mini markdown (bold, italic, code, lists, breaks) */
function md(text) {
  const esc = text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/^- (.+)$/gm, "•&nbsp;$1")
    .replace(/\n/g, "<br>");
}

/* ==================================================== Bibliothek */
function bookHue(title) {
  let h = 0;
  for (const c of title) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

async function loadLibrary() {
  const [books, vocab, notes, due] = await Promise.all([
    api.get("/api/books"),
    api.get("/api/vocab"),
    api.get("/api/notes"),
    api.get("/api/review/next"),
  ]);

  $("#trainInfo").textContent = due.length
    ? `${due.length}${due.length === 20 ? "+" : ""} ${due.length === 1 ? "card" : "cards"} due · ${vocab.length} total`
    : `Nothing due · ${vocab.length} cards total`;
  $("#trainStart").disabled = !due.length;

  $("#libStats").innerHTML = [
    [books.length, "Books"],
    [vocab.length, "Vocab"],
    [notes.length, "Knowledge"],
  ].map(([n, label]) => `<div class="stat"><div class="stat-n">${n}</div><div class="stat-l">${label}</div></div>`).join("");

  const grid = $("#bookGrid");
  grid.innerHTML = "";
  books.forEach((b) => {
    const started = b.chapter_idx != null && (b.chapter_idx > 0 || b.para_idx > 0);
    const pct = b.n_chapters ? Math.round(((b.chapter_idx || 0) / b.n_chapters) * 100) : 0;
    const card = document.createElement("div");
    card.className = "book-card";
    card.style.setProperty("--hue", bookHue(b.title));
    card.innerHTML = `
      <div class="cover"><span class="cover-title">${b.title}</span></div>
      <div class="card-body">
        <div class="author">${b.author || "&nbsp;"}</div>
        <div class="progress"><div class="progress-fill" style="width:${Math.max(pct, started ? 4 : 0)}%"></div></div>
        <div class="meta">
          <span>${LANG_NAMES[b.language] || b.language}</span>
          <span>${started ? `Chapter ${(b.chapter_idx || 0) + 1}/${b.n_chapters}` : b.n_chapters + " chapters"}</span>
        </div>
        <div class="cta">${started ? "Continue" : "Start reading"} →</div>
      </div>`;
    card.onclick = () => openBook(b.id);
    grid.appendChild(card);
  });
}

$("#importFile").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  $("#importStatus").textContent = "Importing…";
  const buf = await file.arrayBuffer();
  const b64 = btoa(String.fromCharCode(...new Uint8Array(buf)));
  try {
    await api.post("/api/import", {
      filename: file.name,
      data_b64: b64,
      language: $("#importLang").value,
    });
    $("#importStatus").textContent = "Imported ✓";
    loadLibrary();
  } catch (err) {
    $("#importStatus").textContent = "Error: " + err.message;
  }
});

/* ==================================================== Reader */
async function openBook(id) {
  state.book = await api.get(`/api/books/${id}`);
  state.chapterIdx = state.book.position ? state.book.position.chapter_idx : 0;
  state.paraIdx = state.book.position ? state.book.position.para_idx : 0;

  $("#library").classList.add("hidden");
  $("#reader").classList.remove("hidden");
  $("#bookTitle").textContent = state.book.book.title;
  $("#langBadge").textContent = state.book.book.language;

  const sel = $("#chapterSelect");
  sel.innerHTML = "";
  state.book.chapters.forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c.idx;
    opt.textContent = `${c.idx + 1}. ${c.title}`;
    sel.appendChild(opt);
  });

  await Promise.all([loadChapter(state.chapterIdx, state.paraIdx), loadMarks(), loadChat(), loadVocab(), loadNotes()]);
}

$("#backBtn").onclick = () => {
  $("#reader").classList.add("hidden");
  $("#library").classList.remove("hidden");
  loadLibrary();
};

async function loadChapter(idx, targetPara = 0) {
  const bookId = state.book.book.id;
  state.chapterIdx = idx;
  $("#chapterSelect").value = idx;
  const { paragraphs } = await api.get(`/api/books/${bookId}/chapters/${idx}`);
  const box = $("#pages");
  box.innerHTML = "";

  const title = document.createElement("h2");
  title.className = "chapter-title";
  title.textContent = state.book.chapters[idx].title;
  box.appendChild(title);

  paragraphs.forEach((para, i) => {
    const p = document.createElement("p");
    p.dataset.idx = i;
    p.innerHTML = tokenize(para);
    box.appendChild(p);
  });
  highlightMarkedWords();
  layoutPages(targetPara); // targetPara -1 = letzte Seite (Rückwärtsblättern über Kapitelgrenze)
  savePosition();
}

function tokenize(para) {
  // wrap words in spans, keep punctuation/whitespace as-is
  return para.replace(/[A-Za-zÀ-ÖØ-öø-ÿĀ-ž'’’-]+/g, (w) => `<span class="w">${w}</span>`);
}

$("#prevChapter").onclick = () => state.chapterIdx > 0 && loadChapter(state.chapterIdx - 1);
$("#nextChapter").onclick = () =>
  state.chapterIdx < state.book.chapters.length - 1 && loadChapter(state.chapterIdx + 1);
$("#chapterSelect").onchange = (e) => loadChapter(parseInt(e.target.value, 10));

/* ==================================================== Seiten (Buch-Layout)
   Der Kapiteltext fließt in CSS-Spalten von exakt einer Seitenbreite;
   Blättern = Verschieben des Spaltencontainers. Die Leseposition bleibt
   (chapter_idx, para_idx): gespeichert wird der erste Absatz der Seite. */
let posTimer = null;
const pager = { page: 0, count: 1, stride: 0, gap: 48, flipping: false };
const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function layoutPages(targetPara = null) {
  const box = $("#pages");
  const w = box.clientWidth;
  if (!w) return;
  box.style.columnWidth = w + "px";
  box.style.columnGap = pager.gap + "px";
  box.style.transform = "none";
  pager.stride = w + pager.gap;
  pager.count = Math.max(1, Math.round((box.scrollWidth + pager.gap) / pager.stride));
  const para = targetPara === null ? state.paraIdx : targetPara;
  showPage(para === -1 ? pager.count - 1 : pageOfPara(para));
}

function pageOfPara(paraIdx) {
  const el = document.querySelector(`#pages p[data-idx="${paraIdx}"]`);
  return el ? Math.min(pager.count - 1, Math.round(el.offsetLeft / pager.stride)) : 0;
}

function firstParaOfPage(page) {
  // erster Absatz, der auf der Seite beginnt; sonst der hineinragende davor
  let spanning = 0;
  for (const p of document.querySelectorAll("#pages p")) {
    const pg = Math.round(p.offsetLeft / pager.stride);
    if (pg === page) return parseInt(p.dataset.idx, 10);
    if (pg > page) break;
    spanning = parseInt(p.dataset.idx, 10);
  }
  return spanning;
}

function showPage(page) {
  pager.page = Math.max(0, Math.min(pager.count - 1, page));
  $("#pages").style.transform = `translateX(${-pager.page * pager.stride}px)`;
  $("#pageInfo").textContent = `${pager.page + 1} / ${pager.count}`;
  const lastChapter = state.book && state.chapterIdx === state.book.chapters.length - 1;
  $("#pagePrev").disabled = pager.page === 0 && state.chapterIdx === 0;
  $("#pageNext").disabled = pager.page === pager.count - 1 && lastChapter;
  state.paraIdx = firstParaOfPage(pager.page);
  clearTimeout(posTimer);
  posTimer = setTimeout(savePosition, 800);
}

function turnPage(dir) { // +1 vor, -1 zurück; über Kapitelgrenzen hinweg
  if (pager.flipping || !state.book) return;
  const target = pager.page + dir;
  if (target < 0) {
    if (state.chapterIdx > 0) loadChapter(state.chapterIdx - 1, -1);
    return;
  }
  if (target >= pager.count) {
    if (state.chapterIdx < state.book.chapters.length - 1) loadChapter(state.chapterIdx + 1, 0);
    return;
  }
  if (REDUCED_MOTION) return showPage(target);
  slideTo(target);
}

function slideTo(target) { // dezentes Gleiten statt 3D-Umschlag
  const box = $("#pages");
  const fromX = -pager.page * pager.stride;
  pager.flipping = true;
  showPage(target); // Endzustand setzen; die Animation überblendet dorthin
  const toX = -pager.page * pager.stride;
  const anim = box.animate(
    [{ transform: `translateX(${fromX}px)` }, { transform: `translateX(${toX}px)` }],
    { duration: 200, easing: "ease-out" });
  let done = false;
  const finish = () => { // idempotent — läuft auch, falls der Browser die Animation drosselt
    if (done) return;
    done = true;
    pager.flipping = false;
  };
  anim.onfinish = anim.oncancel = finish;
  setTimeout(finish, 400);
}

$("#pageNext").onclick = () => turnPage(1);
$("#pagePrev").onclick = () => turnPage(-1);

/* Swipe wie im Buch (Touch/Stift — die Maus wählt Text aus) */
let swipeStart = null;
$("#text").addEventListener("pointerdown", (e) => {
  if (e.pointerType === "mouse") return;
  swipeStart = { x: e.clientX, y: e.clientY };
});
$("#text").addEventListener("pointerup", (e) => {
  if (!swipeStart) return;
  const dx = e.clientX - swipeStart.x, dy = e.clientY - swipeStart.y;
  swipeStart = null;
  if (Math.abs(dx) > 48 && Math.abs(dx) > 1.6 * Math.abs(dy)) turnPage(dx < 0 ? 1 : -1);
});
$("#text").addEventListener("pointercancel", () => { swipeStart = null; });

document.addEventListener("keydown", (e) => {
  if ($("#reader").classList.contains("hidden")) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) return;
  if (e.key === "ArrowRight") turnPage(1);
  if (e.key === "ArrowLeft") turnPage(-1);
});

/* Neu paginieren, sobald sich die Seitenfläche ändert — deckt Fenster-Resize ab
   und heilt den Fall, dass beim ersten Layout die Breite noch 0 war. */
let resizeTimer = null;
new ResizeObserver(() => {
  if (!state.book || $("#reader").classList.contains("hidden")) return;
  const w = $("#pages").clientWidth;
  if (!w || pager.stride === w + pager.gap) return; // nichts Relevantes geändert
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => layoutPages(state.paraIdx), 120);
}).observe($("#pageClip"));
// Websfonts ändern den Umbruch — nach dem Laden einmal neu paginieren
if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(() => { if (state.book) layoutPages(state.paraIdx); });
}

function savePosition() {
  if (!state.book) return;
  api.post("/api/position", {
    book_id: state.book.book.id,
    chapter_idx: state.chapterIdx,
    para_idx: state.paraIdx,
  }).catch(() => {});
}

/* ==================================================== Wort-Popover */
const pop = $("#wordPop");

document.addEventListener("click", (e) => {
  if (e.target.classList && e.target.classList.contains("w")) {
    const word = e.target.textContent;
    const sentence = extractSentence(e.target);
    state.currentWord = { word, sentence, el: e.target };
    $("#popWord").textContent = word;
    $("#popBody").innerHTML = "";
    positionPopup(pop, e.target.getBoundingClientRect());
    pop.classList.remove("hidden");
    e.stopPropagation();
  } else if (!pop.contains(e.target)) {
    pop.classList.add("hidden");
  }
});
$("#popClose").onclick = () => pop.classList.add("hidden");

function extractSentence(el) {
  const paraText = el.closest("p").textContent;
  const word = el.textContent;
  // find the sentence containing the word
  const sentences = paraText.match(/[^.!?…]+[.!?…]*/g) || [paraText];
  return (sentences.find((s) => s.includes(word)) || paraText).trim();
}

function positionPopup(el, rect) {
  const m = 12, w = 340;
  el.style.left = Math.min(Math.max(m, rect.left), window.innerWidth - w - m) + "px";
  const below = window.innerHeight - rect.bottom;
  if (below >= 320 || below >= rect.top) {
    el.style.top = rect.bottom + 8 + "px";
    el.style.maxHeight = Math.min(430, below - 24) + "px";
  } else {
    const h = Math.min(430, rect.top - 24);
    el.style.top = Math.max(m, rect.top - h - 8) + "px";
    el.style.maxHeight = h + "px";
  }
}

$("#popExplain").onclick = async () => {
  const { word, sentence } = state.currentWord;
  $("#popBody").innerHTML = '<em class="muted">Lector is thinking…</em>';
  try {
    const res = await api.post("/api/lookup", {
      book_id: state.book.book.id,
      word,
      sentence,
      chapter_idx: state.chapterIdx,
    });
    $("#popBody").innerHTML = md(res.explanation);
    loadVocab();
  } catch (err) {
    $("#popBody").innerHTML = '<em class="muted">Error: ' + err.message + "</em>";
  }
};

$("#popMark").onclick = async () => {
  const { word, el } = state.currentWord;
  await addMark("word", word);
  el.classList.add("marked");
  pop.classList.add("hidden");
};

$("#popQuick").onclick = async () => {
  const { word, sentence } = state.currentWord;
  await api.post("/api/vocab", { book_id: state.book.book.id, word, sentence, chapter_idx: state.chapterIdx });
  $("#popBody").innerHTML = '<em class="muted">Saved ✓ — the next agent will add an explanation.</em>';
  loadVocab();
};

/* ==================================================== Auswahl-Toolbar */
const selTool = $("#selTool");
let selectionText = "";

document.addEventListener("mouseup", (e) => {
  if (selTool.contains(e.target) || pop.contains(e.target)) return;
  setTimeout(() => {
    const sel = window.getSelection();
    const text = sel.toString().trim();
    if (text.length > 2 && text.includes(" ") && $("#text").contains(sel.anchorNode)) {
      selectionText = text;
      const rect = sel.getRangeAt(0).getBoundingClientRect();
      selTool.style.left = Math.min(rect.left + rect.width / 2 - 70, window.innerWidth - 180) + "px";
      selTool.style.top = Math.max(8, rect.top - 44) + "px";
      selTool.classList.remove("hidden");
    } else {
      selTool.classList.add("hidden");
    }
  }, 10);
});

$("#selMark").onclick = async () => {
  await addMark("passage", selectionText);
  selTool.classList.add("hidden");
  window.getSelection().removeAllRanges();
};

$("#selVocab").onclick = async () => {
  await api.post("/api/vocab", {
    book_id: state.book.book.id,
    word: selectionText,
    sentence: selectionText,
    chapter_idx: state.chapterIdx,
  });
  selTool.classList.add("hidden");
  window.getSelection().removeAllRanges();
  loadVocab();
};

$("#selAsk").onclick = async () => {
  await addMark("passage", selectionText);
  selTool.classList.add("hidden");
  window.getSelection().removeAllRanges();
  switchTab("chat");
  $("#chatInput").focus();
};

/* ==================================================== Marks / Kontext */
async function addMark(kind, text) {
  await api.post("/api/marks", {
    book_id: state.book.book.id,
    chapter_idx: state.chapterIdx,
    kind,
    text,
  });
  await loadMarks();
}

async function loadMarks() {
  const bookId = state.book.book.id;
  const [marks, pending] = await Promise.all([
    api.get(`/api/marks?book_id=${bookId}`),
    api.get(`/api/actions?book_id=${bookId}&status=pending`),
  ]);
  state.marks = marks;
  state.pendingMarkIds = new Set(
    pending.filter((a) => a.type === "unclear_mark")
      .map((a) => JSON.parse(a.payload || "{}").mark_id)
  );
  renderChips();
  renderMarkList();
  highlightMarkedWords();
}

async function deleteMark(id) {
  await api.post(`/api/marks/${id}/delete`, {});
  await loadMarks();
}

function renderChips() {
  const box = $("#contextChips");
  box.innerHTML = "";
  state.marks.forEach((m) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `<span class="t">${m.kind === "passage" ? "„" + m.text + "“" : m.text}</span><span class="x">×</span>`;
    chip.querySelector(".x").onclick = () => deleteMark(m.id);
    box.appendChild(chip);
  });
}

function renderMarkList() {
  const list = $("#markList");
  list.innerHTML = state.marks.length ? "" : '<p class="muted">Nothing marked yet. Click a word or select a passage.</p>';
  state.marks.forEach((m) => {
    const div = document.createElement("div");
    div.className = "mark-item " + m.kind;
    const open = state.pendingMarkIds && state.pendingMarkIds.has(m.id);
    div.innerHTML = `<span class="badge ${open ? "open" : "done"}">${open ? "open" : "discussed"}</span>
      ${m.kind === "passage" ? "„" + m.text + "“" : "<strong>" + m.text + "</strong>"}
      <button class="ghost del">×</button>`;
    div.querySelector(".del").onclick = () => deleteMark(m.id);
    list.appendChild(div);
  });
}

function highlightMarkedWords() {
  const words = new Set(state.marks.filter((m) => m.kind === "word").map((m) => m.text.toLowerCase()));
  document.querySelectorAll("#text .w").forEach((el) => {
    el.classList.toggle("marked", words.has(el.textContent.toLowerCase()));
  });
}

/* ==================================================== Chat */
async function loadChat() {
  const msgs = await api.get(`/api/chat?book_id=${state.book.book.id}`);
  const log = $("#chatLog");
  log.innerHTML = msgs.length ? "" : '<div class="chat-empty">Frag mich etwas zur Seite, zu einem Wort<br>oder zur Geschichte.</div>';
  msgs.forEach((m) => appendMsg(m.role, m.content));
  log.scrollTop = log.scrollHeight;
}

function appendMsg(role, content, thinking = false) {
  const log = $("#chatLog");
  const empty = log.querySelector(".chat-empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = "msg " + role + (thinking ? " thinking" : "");
  div.innerHTML = thinking ? content : md(content);
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

$("#chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chatInput");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  appendMsg("user", message);
  const thinking = appendMsg("assistant", "Lector is reading the page", true);
  $("#chatSend").disabled = true;
  try {
    const res = await api.post("/api/chat", {
      book_id: state.book.book.id,
      chapter_idx: state.chapterIdx,
      para_idx: state.paraIdx,
      message,
    });
    thinking.remove();
    appendMsg("assistant", res.answer);
  } catch (err) {
    thinking.remove();
    appendMsg("assistant", "Something went wrong: " + err.message);
  } finally {
    $("#chatSend").disabled = false;
  }
});

$("#chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chatForm").requestSubmit();
  }
});

/* ==================================================== Tabs */
function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "vocab") loadVocab();
  if (name === "notes") loadNotes();
  if (name === "context") loadMarks();
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => switchTab(t.dataset.tab)));

/* ==================================================== Sprung zur Stelle */
async function jumpToSource(bookId, text, hintChapter) {
  if (!bookId || !text) return;
  if (!state.book || state.book.book.id !== bookId) await openBook(bookId);
  const hint = hintChapter == null ? "" : hintChapter;
  const res = await api.get(
    `/api/locate?book_id=${bookId}&text=${encodeURIComponent(text.slice(0, 80))}&chapter_idx=${hint}`
  );
  if (!res.found) return;
  await loadChapter(res.chapter_idx, res.para_idx); // paginiert direkt zur Zielseite
  const p = document.querySelector(`#pages p[data-idx="${res.para_idx}"]`);
  if (p) {
    p.classList.add("flash");
    setTimeout(() => p.classList.remove("flash"), 2200);
  }
}

function chapterLabel(idx) {
  return idx == null ? "" : `Ch. ${idx + 1}`;
}

/* ==================================================== Wissen (Notizen) */
const KIND_LABELS = { grammar: "Grammar", idea: "Ideas", content: "Passages" };
let activeTag = null;

async function loadNotes() {
  const notes = await api.get(`/api/notes?book_id=${state.book.book.id}`);
  const tags = [...new Set(notes.flatMap((n) => (n.tags || "").split(",").map((t) => t.trim()).filter(Boolean)))];
  const tagBox = $("#noteTags");
  tagBox.innerHTML = "";
  tags.forEach((t) => {
    const el = document.createElement("button");
    el.className = "tag" + (activeTag === t ? " active" : "");
    el.textContent = "#" + t;
    el.onclick = () => { activeTag = activeTag === t ? null : t; loadNotes(); };
    tagBox.appendChild(el);
  });

  const list = $("#noteList");
  list.innerHTML = "";
  const filtered = activeTag
    ? notes.filter((n) => (n.tags || "").split(",").map((x) => x.trim()).includes(activeTag))
    : notes;
  if (!filtered.length) {
    list.innerHTML = '<p class="muted">Nothing captured yet. Mark a passage as "Unclear" — the next agent will discuss it with you and store the takeaways here.</p>';
    return;
  }
  ["grammar", "idea", "content"].forEach((kind) => {
    const group = filtered.filter((n) => n.kind === kind);
    if (!group.length) return;
    const head = document.createElement("h4");
    head.className = "note-kind";
    head.textContent = KIND_LABELS[kind];
    list.appendChild(head);
    group.forEach((n) => {
      const div = document.createElement("div");
      div.className = "note-item " + n.kind;
      const tagHtml = (n.tags || "").split(",").map((t) => t.trim()).filter(Boolean)
        .map((t) => `<span class="tag small">#${t}</span>`).join(" ");
      const refText = n.source_text || "";
      div.innerHTML = `${md(n.content)}
        ${refText ? `<div class="note-src" title="Jump to source">„${refText}“</div>` : ""}
        <div class="note-foot">${tagHtml}
          ${refText ? `<button class="ghost jump">${chapterLabel(n.chapter_idx)} →</button>` : ""}
          <button class="ghost del">×</button></div>`;
      const jump = () => jumpToSource(n.book_id || state.book.book.id, refText, n.chapter_idx);
      const src = div.querySelector(".note-src");
      if (src) src.onclick = jump;
      const jbtn = div.querySelector(".jump");
      if (jbtn) jbtn.onclick = jump;
      div.querySelector(".del").onclick = async () => {
        await api.post(`/api/notes/${n.id}/delete`, {});
        loadNotes();
      };
      list.appendChild(div);
    });
  });
}

/* ==================================================== Vokabeln */
async function loadVocab() {
  const rows = await api.get("/api/vocab");
  $("#vocabCount").textContent = rows.length + " entries";
  const list = $("#vocabList");
  list.innerHTML = rows.length ? "" : '<p class="muted">No vocab yet. Click a word in the text and have it explained.</p>';
  rows.forEach((v) => {
    const d = document.createElement("details");
    d.className = "vocab-item";
    const expl = v.explanation
      ? md(v.explanation)
      : '<em class="muted">Explanation coming once an agent connects…</em>';
    const ref = v.sentence || v.word;
    d.innerHTML = `<summary>${v.word}${v.explanation ? "" : ' <span class="badge open">offen</span>'}</summary>
      <div class="expl">${expl}
        ${v.sentence ? `<div class="note-src" title="Jump to source">„${v.sentence}“</div>` : ""}
        ${v.book_id ? `<button class="ghost jump">${chapterLabel(v.chapter_idx)} →</button>` : ""}
      </div>`;
    const jbtn = d.querySelector(".jump");
    if (jbtn) jbtn.onclick = () => jumpToSource(v.book_id, ref, v.chapter_idx);
    const src = d.querySelector(".note-src");
    if (src) src.onclick = () => jumpToSource(v.book_id, ref, v.chapter_idx);
    list.appendChild(d);
  });
}

$("#vocabAdd").addEventListener("submit", async (e) => {
  e.preventDefault();
  const word = $("#vocabWord").value.trim();
  if (!word) return;
  $("#vocabWord").value = "";
  await api.post("/api/vocab", { book_id: state.book.book.id, word, sentence: "", chapter_idx: state.chapterIdx });
  loadVocab();
});

/* ==================================================== Anki-Trainer */
let trainQueue = [], trainIdx = 0, trainDone = 0;

function escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function fmtInterval(days) {
  if (days < 1) return Math.round(days * 24) + " h";
  return Math.round(days) + (Math.round(days) === 1 ? " day" : " days");
}

function predictIntervals(card) {
  const iv = card.interval_days || 0, first = !card.reps;
  return {
    again: "10 min",
    hard: fmtInterval(first ? 0.5 : Math.max(0.5, iv * 1.2)),
    good: fmtInterval(first ? 1 : Math.max(1, iv * 2.2)),
    easy: fmtInterval(first ? 3 : Math.max(2, iv * 3.2)),
  };
}

async function openTrainer() {
  trainQueue = await api.get("/api/review/next");
  trainIdx = 0;
  trainDone = 0;
  $("#trainer").classList.remove("hidden");
  renderTrainCard();
}

function closeTrainer() {
  $("#trainer").classList.add("hidden");
  if (!$("#library").classList.contains("hidden")) loadLibrary();
  if (state.book) loadVocab();
}

function renderTrainCard() {
  const box = $("#trainCard");
  $("#trainProgress").textContent = `${Math.min(trainIdx + 1, trainQueue.length)} / ${trainQueue.length}`;
  if (trainIdx >= trainQueue.length) {
    $("#trainProgress").textContent = "";
    box.innerHTML = `
      <div class="t-card t-done">
        <div class="t-check">✓</div>
        <div class="t-word">${trainDone} cards done</div>
        <p class="muted">The rest will come back when due.</p>
        <div class="t-actions"><button class="btn btn-primary" id="tClose">Done</button></div>
      </div>`;
    $("#tClose").onclick = closeTrainer;
    return;
  }
  const card = trainQueue[trainIdx];
  const cloze = card.sentence
    ? card.sentence.replace(new RegExp(escapeRegex(card.word), "i"), "____")
    : "";
  const iv = predictIntervals(card);
  box.innerHTML = `
    <div class="t-card">
      <div class="t-label">${card.word === card.sentence ? "Phrase" : "Word"} · ${LANG_NAMES[card.language] || card.language}</div>
      <div class="t-word">${card.word}</div>
      ${cloze && cloze !== card.word ? `<div class="t-sent">„${cloze}“</div>` : ""}
      <div class="t-actions"><button class="btn btn-primary" id="tReveal">Reveal</button></div>
      <div id="tBack" class="t-back hidden">
        <div class="expl">${md(card.explanation || "<em>No explanation yet — the next agent will add one.</em>")}</div>
        ${card.sentence ? `<div class="note-src">„${card.sentence}“</div>` : ""}
        <div class="grades">
          <button class="grade-btn g-again" data-g="again">Again<span class="grade-sub">${iv.again}</span></button>
          <button class="grade-btn g-hard" data-g="hard">Hard<span class="grade-sub">${iv.hard}</span></button>
          <button class="grade-btn g-good" data-g="good">Good<span class="grade-sub">${iv.good}</span></button>
          <button class="grade-btn g-easy" data-g="easy">Easy<span class="grade-sub">${iv.easy}</span></button>
        </div>
      </div>
    </div>`;
  $("#tReveal").onclick = () => {
    $("#tBack").classList.remove("hidden");
    $("#tReveal").classList.add("hidden");
  };
  const src = box.querySelector(".note-src");
  if (src && card.book_id) src.onclick = () => {
    closeTrainer();
    jumpToSource(card.book_id, card.sentence || card.word, card.chapter_idx);
  };
  box.querySelectorAll(".grade-btn").forEach((b) => {
    b.onclick = async () => {
      await api.post("/api/review/answer", { id: card.id, grade: b.dataset.g });
      trainDone += 1;
      trainIdx += 1;
      renderTrainCard();
    };
  });
}

$("#reviewBtn").onclick = openTrainer;
$("#trainStart").onclick = openTrainer;
$("#trainClose").onclick = closeTrainer;

/* ==================================================== Start */
loadLibrary();
