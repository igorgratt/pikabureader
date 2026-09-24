(function () {
  "use strict";

  const settings = Object.assign(
    { theme: "light", font: "sans", size: "18", width: "normal" },
    window.READER && window.READER.settings
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
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".bookmark-btn");
    if (!btn) return;
    post("/api/bookmark", { chapter_id: btn.dataset.chapter }).then((res) => {
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
  let progressTimer = null;
  let lastPct = -1;

  function saveProgress(force) {
    if (!storyPage) return;
    const cid = storyPage.dataset.chapter;
    const rect = readerEl.getBoundingClientRect();
    const total = readerEl.offsetHeight - window.innerHeight;
    let pct = 0;
    if (total > 0) pct = Math.round((-rect.top / total) * 100);
    else if (rect.bottom <= window.innerHeight) pct = 100;
    pct = Math.max(0, Math.min(100, pct));
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

  if (storyPage && readerEl) {
    window.addEventListener(
      "scroll",
      () => {
        clearTimeout(progressTimer);
        progressTimer = setTimeout(saveProgress, 400);
      },
      { passive: true }
    );
    saveProgress(true);
    window.addEventListener("beforeunload", () => saveProgress(true));
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
    return (
      '<div class="note' + (depth > 0 ? " note-nested" : "") + '" data-note="' + note.id + '">' +
      '<div class="note-head"><span class="avatar-note">Я</span><b class="note-author">Ты</b>' +
      '<span class="note-time">' + note.created_at + "</span></div>" +
      '<div class="note-text">' + esc(note.text) + "</div>" +
      '<div class="note-foot">' +
      '<div class="rating rating-sm" data-target="note" data-id="' + note.id + '">' +
      '<button class="rate-btn rate-up" data-delta="1">▲</button>' +
      '<span class="rating-num">' + note.rating + "</span>" +
      '<button class="rate-btn rate-down" data-delta="-1">▼</button></div>' +
      '<button class="note-reply" data-parent="' + note.id + '" type="button">Ответить</button>' +
      '<button class="note-delete" data-note="' + note.id + '" type="button">Удалить</button>' +
      "</div><div class='note-children'></div></div>"
    );
  }

  function removeEmpty() {
    const empty = tree && tree.querySelector(".notes-empty");
    if (empty) empty.remove();
  }

  if (noteForm) {
    noteForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const ta = document.getElementById("note-text");
      const text = ta.value.trim();
      if (!text) return;
      post("/api/note", {
        chapter_id: noteForm.dataset.chapter,
        parent_id: replyTo,
        text,
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
})();
