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
async function loadLibrary() {
  const books = await api.get("/api/books");
  const grid = $("#bookGrid");
  grid.innerHTML = "";
  books.forEach((b) => {
    const card = document.createElement("div");
    card.className = "book-card";
    card.innerHTML = `
      <h3>${b.title}</h3>
      <div class="author">${b.author || "&nbsp;"}</div>
      <div class="meta">
        <span>${LANG_NAMES[b.language] || b.language}</span>
        <span>${b.n_chapters} Kapitel</span>
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

  await Promise.all([loadChapter(state.chapterIdx, state.paraIdx), loadMarks(), loadChat(), loadVocab()]);
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
  el.style.left = Math.min(rect.left, window.innerWidth - 350) + "px";
  el.style.top =
    (rect.bottom + 440 < window.innerHeight ? rect.bottom + 8 : Math.max(10, rect.top - 440)) + "px";
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
  state.marks = await api.get(`/api/marks?book_id=${state.book.book.id}`);
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
    div.innerHTML = `${m.kind === "passage" ? "„" + m.text + "“" : "<strong>" + m.text + "</strong>"}<button class="ghost del">×</button>`;
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
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => switchTab(t.dataset.tab)));

/* ==================================================== Vokabeln */
async function loadVocab() {
  const rows = await api.get("/api/vocab");
  $("#vocabCount").textContent = rows.length + " Einträge";
  const list = $("#vocabList");
  list.innerHTML = rows.length ? "" : '<p class="muted">Noch keine Vokabeln. Klicke ein Wort im Text an und lass es dir erklären.</p>';
  rows.forEach((v) => {
    const d = document.createElement("details");
    d.className = "vocab-item";
    d.innerHTML = `<summary>${v.word}</summary><div class="expl">${md(v.explanation || "")}<br><em class="muted">${v.sentence || ""}</em></div>`;
    list.appendChild(d);
  });
}

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
