(function () {
  "use strict";

  const stored = (window.READER && window.READER.settings) || {};
  const sysDark =
    window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const settings = Object.assign(
    { theme: sysDark ? "dark" : "light", font: "sans", size: "18", width: "normal" },
    stored
  );

  // ------------------------------------------------ settings
  function applySettings() {
    document.documentElement.dataset.theme = settings.theme;
    const reader = document.getElementById("reader");
    if (!reader) return;
    reader.dataset.width = settings.width;
    reader.style.setProperty("--size-read", settings.size + "px");
    reader.style.setProperty(
      "--font-read",
      settings.font === "serif"
        ? 'Georgia, "Times New Roman", serif'
        : 'system-ui, "Segoe UI", Roboto, Arial, sans-serif'
    );
    document.querySelectorAll("[data-set]").forEach((el) => {
      el.value = settings[el.dataset.set] || el.value;
    });
  }

  function saveSettings(patch) {
    Object.assign(settings, patch);
    applySettings();
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    }).catch(() => {});
  }

  window.addEventListener("DOMContentLoaded", applySettings);

  const themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", () =>
      saveSettings({ theme: settings.theme === "dark" ? "light" : "dark" })
    );
  }

  document.addEventListener("change", (e) => {
    const el = e.target;
    if (el.dataset && el.dataset.set) {
      saveSettings({ [el.dataset.set]: el.value });
    }
  });

  // ------------------------------------------------ api helper
  function post(url, data) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }).then((r) => r.json());
  }

  // ------------------------------------------------ ratings
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".rate-btn");
    if (!btn) return;
    const box = btn.closest(".rating");
    if (!box) return;
    const delta = parseInt(btn.dataset.delta, 10);
    const numEl = box.querySelector(".rating-num");
    const before = parseInt(numEl.textContent.replace("K", "000"), 10) || 0;
    post("/api/rate", { target: box.dataset.target, id: box.dataset.id, delta })
      .then((res) => {
        if (!res.ok) return;
        numEl.textContent = res.rating;
        numEl.classList.toggle("is-pos", res.rating > 0);
        numEl.classList.toggle("is-neg", res.rating < 0);
        box.querySelectorAll(".rate-btn").forEach((b) => b.classList.remove("is-active"));
        if (delta > 0 && res.rating > before) btn.classList.add("is-active");
        if (delta < 0 && res.rating < before) btn.classList.add("is-active");
      })
      .catch(() => {});
  });

  // ------------------------------------------------ bookmarks
  // N6: цитата — ближайший к верху видимый абзац (чтобы в закладке было видно,
  // на что она ведёт)
  function currentQuote() {
    const box = document.getElementById("reader");
    if (!box) return "";
    const els = box.querySelectorAll("p, h1, h2, h3, li, blockquote, pre");
    let best = null;
    let bestDist = Infinity;
    for (const el of els) {
      const r = el.getBoundingClientRect();
      if (r.height < 8) continue;
      const dist = Math.abs(r.top - 110);
      if (r.bottom > 0 && dist < bestDist) {
        bestDist = dist;
        best = el;
      }
    }
    if (!best) return "";
    return best.textContent.replace(/\s+/g, " ").trim().slice(0, 300);
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".bookmark-btn");
    if (!btn) return;
    post("/api/bookmark", { chapter_id: btn.dataset.chapter, quote: currentQuote() }).then((res) => {
      if (!res.ok) return;
      btn.classList.toggle("is-active", res.bookmark);
      btn.textContent = res.bookmark ? "🔖 в закладках" : "🔖 Сохранить";
    });
  });

  // ------------------------------------------------ share
  document.addEventListener("click", (e) => {
    const del = e.target.closest(".book-delete");
    if (del) {
      if (!confirm("Удалить книгу со всеми главами и заметками?")) return;
      post("/api/book/delete", { book_id: del.dataset.book }).then((res) => {
        if (res.ok) location.href = "/";
      });
      return;
    }
    const btn = e.target.closest(".share-btn");
    if (!btn) return;
    const url = btn.dataset.url || location.href;
    const done = () => {
      const old = btn.textContent;
      btn.textContent = "✓ Ссылка скопирована";
      setTimeout(() => (btn.textContent = old), 1500);
    };
    if (navigator.clipboard) navigator.clipboard.writeText(url).then(done, done);
    else done();
  });

  // ------------------------------------------------ progress
  const readerEl = document.getElementById("reader");
  const storyPage = document.querySelector(".story-page");
  const progressBar = document.getElementById("read-progress-fill");
  const nextFab = document.getElementById("next-fab");
  let progressTimer = null;
  let lastPct = -1;

  function calcPct() {
    const rect = readerEl.getBoundingClientRect();
    const total = readerEl.offsetHeight - window.innerHeight;
    let pct = 0;
    if (total > 0) pct = Math.round((-rect.top / total) * 100);
    else if (rect.bottom <= window.innerHeight) pct = 100;
    return Math.max(0, Math.min(100, pct));
  }

  function updateBar(pct) {
    if (progressBar) progressBar.style.width = pct + "%";
  }

  function updateFab() {
    if (!nextFab) return;
    const doc = document.documentElement;
    const nearEnd =
      window.scrollY + window.innerHeight > doc.scrollHeight - window.innerHeight * 1.3;
    nextFab.classList.toggle("is-visible", window.scrollY > 80 && nearEnd);
  }

  function saveProgress(force) {
    if (!storyPage) return;
    const cid = storyPage.dataset.chapter;
    const pct = calcPct();
    if (pct === 0 && !force) return;
    if (pct === 0 && force && lastPct === -1) return; // opened at top: keep saved progress
    if (!force && Math.abs(pct - lastPct) < 5) return;
    lastPct = pct;
    const done = pct >= 95;
    post("/api/progress", { chapter_id: cid, pct, done }).then(() => {
      const label = document.getElementById("pct-label");
      if (label) label.textContent = pct + "%";
      if (done) {
        const markBtn = document.getElementById("mark-done");
        if (markBtn) markBtn.classList.add("is-active");
      }
    });
  }

  function restorePosition() {
    if (location.hash) return; // переход по якорю (сноска) — не мешаем
    const saved = parseInt(storyPage.dataset.readPct || "0", 10);
    if (!(saved > 2 && saved < 95)) return;
    const rect = readerEl.getBoundingClientRect();
    const absTop = rect.top + window.scrollY;
    const total = readerEl.offsetHeight - window.innerHeight;
    if (total <= 0) return;
    window.scrollTo(0, Math.round(absTop + (total * saved) / 100));
  }

  // ------------------------------------------------ подсветка в тексте
  // используется для ?find= (N2) и перехода по цитате заметки (R1)
  function highlightInReader(needle) {
    if (!readerEl || !needle) return false;
    const lower = needle.toLowerCase();
    const walker = document.createTreeWalker(readerEl, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const idx = node.nodeValue.toLowerCase().indexOf(lower);
      if (idx === -1) continue;
      try {
        const range = document.createRange();
        range.setStart(node, idx);
        range.setEnd(node, idx + needle.length);
        const mark = document.createElement("mark");
        mark.className = "find-mark";
        range.surroundContents(mark);
        mark.scrollIntoView({ block: "center" });
        setTimeout(() => {
          const parent = mark.parentNode;
          if (!parent) return;
          while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
          parent.removeChild(mark);
          if (parent.normalize) parent.normalize();
        }, 4000);
      } catch (e) {
        /* range за пределами узла — пропускаем */
      }
      return true;
    }
    return false;
  }

  (function highlightFind() {
    const find = new URLSearchParams(location.search).get("find");
    if (find) highlightInReader(find);
  })();

  // ------------------------------------------------ M2: свайп между главами
  if (storyPage) {
    let x0 = null;
    let y0 = null;
    storyPage.addEventListener(
      "touchstart",
      (e) => {
        if (e.touches.length !== 1) return;
        x0 = e.touches[0].clientX;
        y0 = e.touches[0].clientY;
      },
      { passive: true }
    );
    storyPage.addEventListener(
      "touchend",
      (e) => {
        if (x0 === null) return;
        const t = e.changedTouches[0];
        const dx = t.clientX - x0;
        const dy = t.clientY - y0;
        x0 = null;
        if (Math.abs(dx) < 70 || Math.abs(dy) > 60) return;
        const target = storyPage.dataset[dx < 0 ? "nxt" : "prev"];
        if (target) location.href = target;
      },
      { passive: true }
    );
  }

  // ------------------------------------------------ M10: шапка и fullscreen
  const topbar = document.querySelector(".topbar");
  let lastY = window.scrollY;
  function updateTopbar() {
    if (!topbar) return;
    const y = window.scrollY;
    if (y > lastY + 5 && y > 140) topbar.classList.add("is-hidden");
    else if (y < lastY - 5 || y <= 140) topbar.classList.remove("is-hidden");
    lastY = y;
  }

  const fsBtn = document.getElementById("fs-toggle");
  if (fsBtn) {
    fsBtn.addEventListener("click", () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else if (document.documentElement.requestFullscreen)
        document.documentElement.requestFullscreen().catch(() => {});
    });
  }

  if (storyPage && readerEl) {
    restorePosition();
    window.addEventListener(
      "scroll",
      () => {
        updateBar(calcPct());
        updateFab();
        updateTopbar();
        clearTimeout(progressTimer);
        progressTimer = setTimeout(saveProgress, 400);
      },
      { passive: true }
    );
    updateBar(calcPct());
    updateFab();
    saveProgress(true);
    window.addEventListener("beforeunload", () => saveProgress(true));
  } else {
    window.addEventListener("scroll", updateTopbar, { passive: true });
  }

  const markBtn = document.getElementById("mark-done");
  if (markBtn) {
    markBtn.addEventListener("click", () => {
      post("/api/progress", { chapter_id: markBtn.dataset.chapter, pct: 100, done: true }).then(() => {
        markBtn.classList.add("is-active");
        markBtn.textContent = "✓ Прочитано";
      });
    });
  }

  // ------------------------------------------------ notes
  const noteForm = document.getElementById("note-form");
  const tree = document.getElementById("notes-tree");
  let replyTo = null;

  function noteNodeHtml(note, depth) {
    const esc = (s) =>
      s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const quote = note.quote
      ? '<button class="note-quote" type="button" data-anchor="' +
        esc(note.quote).replace(/"/g, "&quot;") +
        '" title="Перейти к цитате в тексте">«' + esc(note.quote) + "»</button>"
      : "";
    const edited = note.edited_at ? " · изменено" : "";
    return (
      '<div class="note' + (depth > 0 ? " note-nested" : "") + '" data-note="' + note.id + '">' +
      '<div class="note-head"><span class="avatar-note">Я</span><b class="note-author">Ты</b>' +
      '<span class="note-time">' + note.created_at + edited + "</span></div>" +
      quote +
      '<div class="note-text">' + esc(note.text) + "</div>" +
      '<div class="note-foot">' +
      '<div class="rating rating-sm" data-target="note" data-id="' + note.id + '">' +
      '<button class="rate-btn rate-up" data-delta="1">▲</button>' +
      '<span class="rating-num">' + note.rating + "</span>" +
      '<button class="rate-btn rate-down" data-delta="-1">▼</button></div>' +
      '<button class="note-reply" data-parent="' + note.id + '" type="button">Ответить</button>' +
      '<button class="note-edit" data-note="' + note.id + '" type="button">Править</button>' +
      '<button class="note-delete" data-note="' + note.id + '" type="button">Удалить</button>' +
      "</div><div class='note-children'></div></div>"
    );
  }

  function removeEmpty() {
    const empty = tree && tree.querySelector(".notes-empty");
    if (empty) empty.remove();
  }

  if (noteForm) {
    const quoteField = document.getElementById("note-quote");
    noteForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const ta = document.getElementById("note-text");
      const text = ta.value.trim();
      if (!text) return;
      post("/api/note", {
        chapter_id: noteForm.dataset.chapter,
        parent_id: replyTo,
        text,
        quote: quoteField ? quoteField.value : "",
      }).then((res) => {
        if (!res.ok) return;
        removeEmpty();
        const html = noteNodeHtml(res.note, replyTo ? 1 : 0);
        if (replyTo) {
          const parent = tree.querySelector('[data-note="' + replyTo + '"]');
          const holder = parent && parent.querySelector(".note-children");
          if (holder) holder.insertAdjacentHTML("beforeend", html);
          else tree.insertAdjacentHTML("beforeend", html);
        } else {
          tree.insertAdjacentHTML("beforeend", html);
        }
        ta.value = "";
        if (quoteField) quoteField.value = "";
        const chip = document.getElementById("quote-chip");
        if (chip) chip.hidden = true;
        replyTo = null;
        const hint = noteForm.querySelector(".note-hint");
        hint.textContent = "Заметки видны только тебе";
      });
    });
  }

  document.addEventListener("click", (e) => {
    const reply = e.target.closest(".note-reply");
    if (reply) {
      replyTo = parseInt(reply.dataset.parent, 10);
      const ta = document.getElementById("note-text");
      ta.focus();
      const hint = noteForm.querySelector(".note-hint");
      hint.textContent = "Ответ на заметку — отправь форму";
      ta.placeholder = "Ответ…";
      return;
    }
    const del = e.target.closest(".note-delete");
    if (del) {
      post("/api/note/delete", { id: del.dataset.note }).then((res) => {
        if (res.ok) {
          const node = document.querySelector('[data-note="' + del.dataset.note + '"]');
          if (node) {
            const kids = node.querySelector(".note-children");
            const parent = node.parentElement;
            if (kids) while (kids.firstChild) parent.insertBefore(kids.firstChild, node);
            node.remove();
          }
        }
      });
    }
  });

  // ------------------------------------------------ R1: заметка к выделению
  let selPop = null;
  let pendingQuote = "";

  function hideSelPop() {
    if (selPop) selPop.hidden = true;
  }

  function ensureSelPop() {
    if (selPop) return selPop;
    selPop = document.createElement("div");
    selPop.className = "sel-pop";
    selPop.hidden = true;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "📝 Заметить";
    btn.addEventListener("mousedown", (e) => e.preventDefault()); // не терять выделение
    btn.addEventListener("click", () => {
      const hidden = document.getElementById("note-quote");
      const chip = document.getElementById("quote-chip");
      const ta = document.getElementById("note-text");
      if (hidden) hidden.value = pendingQuote;
      if (chip) {
        chip.hidden = false;
        const txt = document.getElementById("quote-chip-text");
        if (txt) txt.textContent = "«" + pendingQuote + "»";
      }
      hideSelPop();
      if (ta) {
        ta.focus();
        const notes = document.getElementById("notes");
        if (notes) notes.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
    selPop.appendChild(btn);
    document.body.appendChild(selPop);
    return selPop;
  }

  function onSelection() {
    if (!readerEl) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) {
      hideSelPop();
      return;
    }
    const node = sel.anchorNode;
    const inReader = node && readerEl.contains(node.nodeType === 1 ? node : node.parentNode);
    const text = sel.toString().replace(/\s+/g, " ").trim();
    if (!inReader || !text) {
      hideSelPop();
      return;
    }
    pendingQuote = text.slice(0, 300);
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    const pop = ensureSelPop();
    pop.hidden = false;
    const half = pop.offsetWidth / 2 || 50;
    const left = Math.max(half + 4, Math.min(rect.left + rect.width / 2, window.innerWidth - half - 4));
    pop.style.left = left + "px";
    pop.style.top = Math.max(8, rect.top - 46) + "px";
  }

  if (readerEl) {
    document.addEventListener("mouseup", () => setTimeout(onSelection, 10));
    document.addEventListener("touchend", () => setTimeout(onSelection, 120));
    document.addEventListener("mousedown", (e) => {
      if (selPop && !selPop.hidden && !selPop.contains(e.target)) hideSelPop();
    });
    document.addEventListener("touchstart", (e) => {
      if (selPop && !selPop.hidden && e.target && !selPop.contains(e.target)) hideSelPop();
    }, { passive: true });
  }

  const chipClear = document.getElementById("quote-chip-clear");
  if (chipClear) {
    chipClear.addEventListener("click", () => {
      const hidden = document.getElementById("note-quote");
      const chip = document.getElementById("quote-chip");
      if (hidden) hidden.value = "";
      if (chip) chip.hidden = true;
    });
  }

  // R1: клик по цитате заметки → прыжок к месту в тексте
  document.addEventListener("click", (e) => {
    const q = e.target.closest(".note-quote");
    if (!q) return;
    highlightInReader(q.dataset.anchor || q.textContent.replace(/^«|»$/g, ""));
  });

  // ------------------------------------------------ R2: правка заметки
  document.addEventListener("click", (e) => {
    const edit = e.target.closest(".note-edit");
    if (!edit) return;
    const node = edit.closest(".note");
    if (!node) return;
    const textEl = node.querySelector(".note-text");
    if (!textEl || node.querySelector(".note-edit-area")) return;
    const original = textEl.textContent;
    const ta = document.createElement("textarea");
    ta.className = "note-edit-area";
    ta.rows = 3;
    ta.value = original;
    const save = document.createElement("button");
    save.className = "btn btn-accent note-edit-save";
    save.type = "button";
    save.textContent = "Сохранить";
    const cancel = document.createElement("button");
    cancel.className = "btn note-edit-cancel";
    cancel.type = "button";
    cancel.textContent = "Отмена";
    textEl.hidden = true;
    textEl.after(ta);
    textEl.after(cancel);
    textEl.after(save);
    ta.focus();
    const close = () => {
      ta.remove();
      save.remove();
      cancel.remove();
      textEl.hidden = false;
    };
    cancel.addEventListener("click", close);
    save.addEventListener("click", () => {
      const text = ta.value.trim();
      if (!text) return;
      post("/api/note/edit", { id: edit.dataset.note, text }).then((res) => {
        if (!res.ok) return;
        textEl.textContent = res.note.text;
        close();
        const time = node.querySelector(".note-time");
        if (time && res.note.edited_at && !time.textContent.includes("изменено")) {
          time.textContent += " · изменено";
        }
      });
    });
  });

  // ------------------------------------------------ N3: оглавление
  const tocToggle = document.getElementById("toc-toggle");
  const tocPop = document.getElementById("toc-pop");
  const tocBackdrop = document.getElementById("toc-backdrop");
  function openToc(open) {
    if (!tocPop) return;
    tocPop.hidden = !open;
    tocBackdrop.hidden = !open;
  }
  if (tocToggle) tocToggle.addEventListener("click", () => openToc(tocPop.hidden));
  const tocClose = document.getElementById("toc-close");
  if (tocClose) tocClose.addEventListener("click", () => openToc(false));
  if (tocBackdrop) tocBackdrop.addEventListener("click", () => openToc(false));

  // ------------------------------------------------ R5: горячие клавиши
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    const typing =
      t &&
      (t.tagName === "INPUT" ||
        t.tagName === "TEXTAREA" ||
        t.tagName === "SELECT" ||
        t.isContentEditable);
    if (e.key === "Escape") {
      openToc(false);
      hideSelPop();
      return;
    }
    // "/" (и "?" на русской раскладке) — фокус в поиск
    if ((e.key === "/" || e.key === "?") && !typing) {
      const input = document.querySelector(".search input");
      if (input) {
        e.preventDefault();
        input.focus();
        input.select();
      }
      return;
    }
    if (typing) return;
    if (window.getSelection && String(window.getSelection()).length > 0) return;
    if (storyPage) {
      if (e.key === "ArrowLeft") {
        const u = storyPage.dataset.prev;
        if (u) location.href = u;
      } else if (e.key === "ArrowRight") {
        const u = storyPage.dataset.nxt;
        if (u) location.href = u;
      } else if (e.key === "b" || e.key === "B" || e.key === "и" || e.key === "И") {
        const btn = document.querySelector(".bookmark-btn");
        if (btn) btn.click();
      }
    }
  });

  // ------------------------------------------------ R6: читать далее в ленте
  document.querySelectorAll(".read-more[data-chapter]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      if (a.dataset.loading) return;
      a.dataset.loading = "1";
      const body = a.parentElement;
      const href = a.getAttribute("href");
      post("/api/chapter", { chapter_id: a.dataset.chapter })
        .then((res) => {
          if (!res.ok) throw new Error(res.error || "error");
          body.innerHTML = res.html;
          const pageLink = document.createElement("a");
          pageLink.className = "read-more read-more-page";
          pageLink.href = href;
          pageLink.textContent = "Открыть страницу главы →";
          body.appendChild(pageLink);
        })
        .catch(() => {
          delete a.dataset.loading;
          a.textContent = "Читать далее";
        });
    });
  });
})();
