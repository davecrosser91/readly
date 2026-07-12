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

const state = {
  book: null,          // {book, chapters, position}
  chapterIdx: 0,
  paraIdx: 0,
  marks: [],
  currentWord: null,   // {word, sentence, el}
};

const LANG_NAMES = { es: "Spanisch", en: "Englisch" };

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
  const [books, vocab, notes] = await Promise.all([
    api.get("/api/books"),
    api.get("/api/vocab"),
    api.get("/api/notes"),
  ]);

  $("#libStats").innerHTML = [
    [books.length, "Bücher"],
    [vocab.length, "Vokabeln"],
    [notes.length, "Wissen"],
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
          <span>${started ? `Kapitel ${(b.chapter_idx || 0) + 1}/${b.n_chapters}` : b.n_chapters + " Kapitel"}</span>
        </div>
        <div class="cta">${started ? "Weiterlesen" : "Anfangen"} →</div>
      </div>`;
    card.onclick = () => openBook(b.id);
    grid.appendChild(card);
  });
}

$("#importFile").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  $("#importStatus").textContent = "Importiere…";
  const buf = await file.arrayBuffer();
  const b64 = btoa(String.fromCharCode(...new Uint8Array(buf)));
  try {
    await api.post("/api/import", {
      filename: file.name,
      data_b64: b64,
      language: $("#importLang").value,
    });
    $("#importStatus").textContent = "Importiert ✓";
    loadLibrary();
  } catch (err) {
    $("#importStatus").textContent = "Fehler: " + err.message;
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

async function loadChapter(idx, scrollToPara = 0) {
  const bookId = state.book.book.id;
  state.chapterIdx = idx;
  $("#chapterSelect").value = idx;
  const { paragraphs } = await api.get(`/api/books/${bookId}/chapters/${idx}`);
  const art = $("#text");
  art.innerHTML = "";

  const title = document.createElement("h2");
  title.className = "chapter-title";
  title.textContent = state.book.chapters[idx].title;
  art.appendChild(title);

  paragraphs.forEach((para, i) => {
    const p = document.createElement("p");
    p.dataset.idx = i;
    p.innerHTML = tokenize(para);
    art.appendChild(p);
  });
  observeParagraphs();
  highlightMarkedWords();

  if (scrollToPara > 0) {
    const target = art.querySelector(`p[data-idx="${scrollToPara}"]`);
    if (target) target.scrollIntoView({ block: "center" });
  } else {
    art.scrollTop = 0;
  }
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

/* Leseposition: oberster sichtbarer Absatz */
let posTimer = null;
function observeParagraphs() {
  const obs = new IntersectionObserver(
    (entries) => {
      const visible = entries
        .filter((e) => e.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (visible.length) {
        state.paraIdx = parseInt(visible[0].target.dataset.idx, 10);
        clearTimeout(posTimer);
        posTimer = setTimeout(savePosition, 1500);
      }
    },
    { root: $("#text"), threshold: 0.1 }
  );
  document.querySelectorAll("#text p").forEach((p) => obs.observe(p));
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
  $("#popBody").innerHTML = '<em class="muted">Lector denkt nach…</em>';
  try {
    const res = await api.post("/api/lookup", {
      book_id: state.book.book.id,
      word,
      sentence,
    });
    $("#popBody").innerHTML = md(res.explanation);
    loadVocab();
  } catch (err) {
    $("#popBody").innerHTML = '<em class="muted">Fehler: ' + err.message + "</em>";
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
  await api.post("/api/vocab", { book_id: state.book.book.id, word, sentence });
  $("#popBody").innerHTML = '<em class="muted">Gespeichert ✓ — Erklärung ergänzt der nächste Agent.</em>';
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
  list.innerHTML = state.marks.length ? "" : '<p class="muted">Noch nichts markiert. Klicke ein Wort an oder wähle eine Passage aus.</p>';
  state.marks.forEach((m) => {
    const div = document.createElement("div");
    div.className = "mark-item " + m.kind;
    const open = state.pendingMarkIds && state.pendingMarkIds.has(m.id);
    div.innerHTML = `<span class="badge ${open ? "open" : "done"}">${open ? "offen" : "besprochen"}</span>
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
  const thinking = appendMsg("assistant", "Lector liest die Seite", true);
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
    appendMsg("assistant", "Da ist etwas schiefgegangen: " + err.message);
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

/* ==================================================== Wissen (Notizen) */
const KIND_LABELS = { grammar: "Grammatik", idea: "Ideen", content: "Inhalte" };
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
    list.innerHTML = '<p class="muted">Noch nichts festgehalten. Markiere eine Stelle als „Unklar" — der nächste Agent bespricht sie mit dir und legt hier Wissen ab.</p>';
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
      div.innerHTML = `${md(n.content)}
        ${n.source_text ? `<div class="note-src">„${n.source_text}“</div>` : ""}
        <div class="note-foot">${tagHtml}<button class="ghost del">×</button></div>`;
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
  $("#vocabCount").textContent = rows.length + " Einträge";
  const list = $("#vocabList");
  list.innerHTML = rows.length ? "" : '<p class="muted">Noch keine Vokabeln. Klicke ein Wort im Text an und lass es dir erklären.</p>';
  rows.forEach((v) => {
    const d = document.createElement("details");
    d.className = "vocab-item";
    const expl = v.explanation
      ? md(v.explanation)
      : '<em class="muted">Erklärung folgt, sobald ein Agent andockt…</em>';
    d.innerHTML = `<summary>${v.word}${v.explanation ? "" : ' <span class="badge open">offen</span>'}</summary>
      <div class="expl">${expl}<br><em class="muted">${v.sentence || ""}</em></div>`;
    list.appendChild(d);
  });
}

$("#vocabAdd").addEventListener("submit", async (e) => {
  e.preventDefault();
  const word = $("#vocabWord").value.trim();
  if (!word) return;
  $("#vocabWord").value = "";
  await api.post("/api/vocab", { book_id: state.book.book.id, word, sentence: "" });
  loadVocab();
});

$("#reviewBtn").onclick = startReview;

async function startReview() {
  const due = await api.get("/api/review/next");
  const area = $("#reviewArea");
  area.classList.remove("hidden");
  if (!due.length) {
    area.innerHTML = '<p class="muted">Nichts fällig. Lies weiter!</p>';
    return;
  }
  let i = 0;
  const show = () => {
    if (i >= due.length) {
      area.innerHTML = '<p class="muted">Fertig für heute ✓</p>';
      loadVocab();
      return;
    }
    const card = due[i];
    area.innerHTML = `
      <div class="review-card">
        <div class="rword">${card.word}</div>
        <div class="rsent">${card.sentence || ""}</div>
        <button class="btn" id="revShow">Aufdecken</button>
        <div id="revBack" class="hidden">
          <div class="expl" style="text-align:left; margin-top:.8rem">${md(card.explanation || "")}</div>
          <div class="review-actions">
            <button class="btn" id="revAgain">Nochmal</button>
            <button class="btn btn-primary" id="revGood">Gewusst</button>
          </div>
        </div>
      </div>`;
    $("#revShow").onclick = () => {
      $("#revBack").classList.remove("hidden");
      $("#revShow").classList.add("hidden");
    };
    const answer = async (grade) => {
      await api.post("/api/review/answer", { id: card.id, grade });
      i += 1;
      show();
    };
    $("#revAgain").onclick = () => answer("again");
    $("#revGood").onclick = () => answer("good");
  };
  show();
}

/* ==================================================== Start */
loadLibrary();
