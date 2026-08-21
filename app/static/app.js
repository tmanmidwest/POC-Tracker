/* Small interaction helpers. No framework. */

// Auto-dismiss flash messages after 5s
document.querySelectorAll('.flash').forEach((el) => {
  setTimeout(() => {
    el.style.transition = 'opacity 0.3s, transform 0.3s';
    el.style.opacity = '0';
    el.style.transform = 'translateX(20px)';
    setTimeout(() => el.remove(), 300);
  }, 5000);
});

// Column picker dropdown toggle and persistence
function setupColumnPicker(pickerEl) {
  const storageKey = pickerEl.dataset.storageKey;
  const trigger = pickerEl.querySelector('.col-picker__trigger');
  const menu = pickerEl.querySelector('.col-picker__menu');
  const checkboxes = pickerEl.querySelectorAll('input[type="checkbox"]');

  // Restore saved state
  if (storageKey) {
    const saved = localStorage.getItem(storageKey);
    if (saved) {
      try {
        const hidden = JSON.parse(saved);
        checkboxes.forEach((cb) => {
          if (hidden.includes(cb.dataset.col)) cb.checked = false;
          applyColumnVisibility(cb.dataset.col, cb.checked);
        });
      } catch (e) { /* ignore parse errors */ }
    } else {
      // No saved state — apply current checkbox state
      checkboxes.forEach((cb) => applyColumnVisibility(cb.dataset.col, cb.checked));
    }
  }

  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    pickerEl.classList.toggle('is-open');
  });

  document.addEventListener('click', (e) => {
    if (!pickerEl.contains(e.target)) pickerEl.classList.remove('is-open');
  });

  checkboxes.forEach((cb) => {
    cb.addEventListener('change', () => {
      applyColumnVisibility(cb.dataset.col, cb.checked);
      if (storageKey) {
        const hidden = Array.from(checkboxes)
          .filter((c) => !c.checked)
          .map((c) => c.dataset.col);
        localStorage.setItem(storageKey, JSON.stringify(hidden));
      }
    });
  });
}

function applyColumnVisibility(colName, visible) {
  document.querySelectorAll(`[data-col="${colName}"]`).forEach((el) => {
    el.style.display = visible ? '' : 'none';
  });
}

document.querySelectorAll('.col-picker').forEach(setupColumnPicker);

// Client-side table filtering. A search box carries [data-filter-search="<tableId>"];
// each dropdown carries [data-filter-target="<tableId>"][data-filter-col="<attr>"] and
// matches against the row's data-<attr>. Region rows may list several values joined by
// "||"; a match means the selected value is one of them. Rows without a data-search
// attribute (e.g. the empty-state row) are ignored.
function applyTableFilters(tableId) {
  const table = document.getElementById(tableId);
  if (!table) return;
  const search = document.querySelector(`[data-filter-search="${tableId}"]`);
  const term = (search ? search.value : '').trim().toLowerCase();
  const selects = document.querySelectorAll(
    `[data-filter-target="${tableId}"][data-filter-col]`
  );
  const active = [];
  selects.forEach((s) => { if (s.value) active.push([s.dataset.filterCol, s.value]); });

  let visible = 0;
  table.querySelectorAll('tbody tr[data-search]').forEach((row) => {
    let show = true;
    if (term && !(row.dataset.search || '').toLowerCase().includes(term)) show = false;
    if (show) {
      for (const [col, val] of active) {
        const parts = (row.dataset[col] || '').split('||').map((p) => p.trim());
        if (!parts.includes(val)) { show = false; break; }
      }
    }
    row.style.display = show ? '' : 'none';
    if (show) visible += 1;
  });

  const empty = table.querySelector('.table-filter-empty');
  if (empty) empty.hidden = visible !== 0;
}

function setupTableFilters() {
  const tableIds = new Set();
  document.querySelectorAll('[data-filter-search], [data-filter-target]').forEach((el) => {
    tableIds.add(el.dataset.filterSearch || el.dataset.filterTarget);
  });
  tableIds.forEach((tableId) => {
    if (!tableId) return;
    const search = document.querySelector(`[data-filter-search="${tableId}"]`);
    if (search) search.addEventListener('input', () => applyTableFilters(tableId));
    document
      .querySelectorAll(`[data-filter-target="${tableId}"][data-filter-col]`)
      .forEach((s) => s.addEventListener('change', () => applyTableFilters(tableId)));
    const clear = document.querySelector(`[data-filter-clear="${tableId}"]`);
    if (clear) {
      clear.addEventListener('click', () => {
        if (search) search.value = '';
        document
          .querySelectorAll(`[data-filter-target="${tableId}"][data-filter-col]`)
          .forEach((s) => { s.value = ''; });
        applyTableFilters(tableId);
      });
    }
    applyTableFilters(tableId);
  });
}

setupTableFilters();

// Click-to-sort on table headers. Sortable <th> carry [data-sort-col]; a click sorts
// the tbody rows by that column, toggling asc/desc, and a second click on a new column
// starts fresh ascending. Sort values come from each row cell's [data-sort] (falling
// back to its text); empty values always sort last. Dates use ISO strings so a plain
// string compare orders them correctly. Filtered-out rows still reorder but stay hidden.
function setupTableSort(table) {
  const headers = Array.from(table.querySelectorAll('thead th[data-sort-col]'));
  if (!headers.length) return;
  const tbody = table.querySelector('tbody');
  if (!tbody) return;

  let curIdx = null;
  let dir = 1; // 1 = ascending, -1 = descending

  function cellValue(row, idx) {
    const cell = row.children[idx];
    if (!cell) return '';
    const raw = cell.dataset.sort != null ? cell.dataset.sort : cell.textContent;
    return raw.trim().toLowerCase();
  }

  function sortByIndex(idx, th) {
    dir = curIdx === idx ? -dir : 1;
    curIdx = idx;

    const rows = Array.from(tbody.querySelectorAll('tr[data-search]'));
    rows.sort((a, b) => {
      const va = cellValue(a, idx);
      const vb = cellValue(b, idx);
      if (va === vb) return 0;
      if (va === '') return 1; // empties last, regardless of direction
      if (vb === '') return -1;
      return va > vb ? dir : -dir;
    });
    rows.forEach((r) => tbody.appendChild(r));
    const empty = tbody.querySelector('.table-filter-empty');
    if (empty) tbody.appendChild(empty); // keep the no-results row at the bottom

    headers.forEach((h) => h.removeAttribute('data-sort-dir'));
    th.setAttribute('data-sort-dir', dir === 1 ? 'asc' : 'desc');
  }

  headers.forEach((th) => {
    const idx = Array.from(th.parentNode.children).indexOf(th);
    th.addEventListener('click', () => sortByIndex(idx, th));
    th.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        sortByIndex(idx, th);
      }
    });
  });
}

document.querySelectorAll('table').forEach(setupTableSort);

// Use-case status filter chips (project detail page). Multi-select toggle: each
// chip [data-uc-chip="<statusId>"] starts active; clicking toggles whether rows
// carrying that status (tr[data-uc-status]) show across every category table.
// Category cards ([data-uc-group]) with no visible rows are hidden. Rows with no
// status are always shown. "Clear" resets every chip to active. Inline status
// changes reload the page, so counts stay server-authoritative.
function setupUseCaseStatusFilter() {
  const bar = document.getElementById('uc-status-filter');
  if (!bar) return;
  const chips = Array.from(bar.querySelectorAll('[data-uc-chip]'));
  if (!chips.length) return;
  const clearBtn = document.getElementById('uc-status-clear');
  const summary = document.getElementById('uc-status-summary');
  const cards = Array.from(document.querySelectorAll('[data-uc-group]'));

  // Persist the toggled-off statuses per project so the filter survives reloads
  // (an inline status change reloads the whole page). Cleared filters remove the
  // key so a fresh visit starts with everything shown.
  const storeKey = `uc-status-filter:${bar.dataset.ucProject || ''}`;
  function loadOff() {
    try {
      const raw = window.localStorage.getItem(storeKey);
      return raw ? new Set(JSON.parse(raw)) : new Set();
    } catch (e) { return new Set(); }
  }
  function saveOff() {
    const off = chips.filter((c) => c.classList.contains('is-off')).map((c) => c.dataset.ucChip);
    try {
      if (off.length) window.localStorage.setItem(storeKey, JSON.stringify(off));
      else window.localStorage.removeItem(storeKey);
    } catch (e) { /* storage unavailable — filter just won't persist */ }
  }

  function apply() {
    const active = new Set();
    chips.forEach((c) => { if (!c.classList.contains('is-off')) active.add(c.dataset.ucChip); });
    const anyOff = chips.some((c) => c.classList.contains('is-off'));

    let shown = 0;
    let total = 0;
    cards.forEach((card) => {
      let cardVisible = 0;
      card.querySelectorAll('tr[data-uc-status]').forEach((row) => {
        total += 1;
        const st = row.dataset.ucStatus;
        const show = st === '' || active.has(st);
        row.style.display = show ? '' : 'none';
        if (show) { shown += 1; cardVisible += 1; }
      });
      card.style.display = cardVisible === 0 ? 'none' : '';
    });

    if (clearBtn) clearBtn.hidden = !anyOff;
    if (summary) summary.textContent = anyOff ? `Showing ${shown} of ${total}` : '';
  }

  chips.forEach((chip) => {
    if (chip.disabled) return;
    chip.addEventListener('click', () => {
      chip.classList.toggle('is-off');
      chip.setAttribute('aria-pressed', chip.classList.contains('is-off') ? 'false' : 'true');
      saveOff();
      apply();
    });
  });
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      chips.forEach((c) => { c.classList.remove('is-off'); c.setAttribute('aria-pressed', 'true'); });
      saveOff();
      apply();
    });
  }

  // Restore the saved selection before the first paint of the filter.
  const savedOff = loadOff();
  chips.forEach((c) => {
    if (savedOff.has(c.dataset.ucChip)) {
      c.classList.add('is-off');
      c.setAttribute('aria-pressed', 'false');
    }
  });
  apply();
}

setupUseCaseStatusFilter();

// Drag-to-reorder list. Container has [data-reorder] and [data-reorder-input]
// (the id of a hidden input); each row has [data-reorder-item] and [data-id].
// On drop, the hidden input is set to the comma-separated order of data-id.
function setupReorder(listEl) {
  const hidden = document.getElementById(listEl.dataset.reorderInput);
  const items = () => Array.from(listEl.querySelectorAll('[data-reorder-item]'));
  let dragging = null;

  function sync() {
    if (hidden) hidden.value = items().map((el) => el.dataset.id).join(',');
  }
  function afterElement(y) {
    return items()
      .filter((el) => el !== dragging)
      .reduce((closest, el) => {
        const box = el.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        return offset < 0 && offset > closest.offset ? { offset, el } : closest;
      }, { offset: -Infinity, el: null }).el;
  }

  items().forEach((item) => {
    item.setAttribute('draggable', 'true');
    item.addEventListener('dragstart', () => { dragging = item; item.classList.add('is-dragging'); });
    item.addEventListener('dragend', () => { item.classList.remove('is-dragging'); dragging = null; sync(); });
  });
  listEl.addEventListener('dragover', (e) => {
    e.preventDefault();
    if (!dragging) return;
    const after = afterElement(e.clientY);
    if (after == null) listEl.appendChild(dragging);
    else listEl.insertBefore(dragging, after);
  });
  sync();
}
document.querySelectorAll('[data-reorder]').forEach(setupReorder);

// Dark-mode toggle. Flips the theme instantly client-side, then persists the
// choice to the user's account. The server renders data-theme on <html>, so
// there's no flash on subsequent loads.
function toggleTheme(btn) {
  const root = document.documentElement;
  const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
  root.dataset.theme = next;
  const icon = btn && btn.querySelector('span');
  if (icon) icon.textContent = next === 'dark' ? '☀' : '☾';
  fetch('/ui/theme', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'theme=' + encodeURIComponent(next),
    credentials: 'same-origin',
  }).catch(() => { /* non-blocking; the visual change already applied */ });
}

// Desktop sidebar rail toggle. Flips the icon-rail state instantly, then
// persists it to the user's account (same pattern as the theme toggle) so it
// sticks across pages with no flash — the server renders .is-rail on load.
function toggleSidebar(btn) {
  const shell = document.getElementById('app-shell');
  if (!shell) return;
  const collapsed = shell.classList.toggle('is-rail');
  if (btn) {
    const label = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
    btn.setAttribute('aria-label', label);
    btn.setAttribute('title', label);
  }
  fetch('/ui/sidebar', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'collapsed=' + (collapsed ? '1' : '0'),
    credentials: 'same-origin',
  }).catch(() => { /* non-blocking; the visual change already applied */ });
}

// Modal: close on Escape, close on overlay click.
// Two kinds of `.modal-overlay` exist:
//  - dynamic (HTMX-injected): closing them means removing them from the DOM.
//  - persistent (authored in a template with `data-modal-persist`, toggled via
//    the `hidden` attribute — e.g. the report Save and Schedule modals): closing
//    means hiding them, so the element survives and can be reopened. Removing
//    them here would delete the markup and break the reopen button.
function closeModalOverlay(overlay) {
  if (!overlay) return;
  if ('modalPersist' in overlay.dataset) overlay.hidden = true;
  else overlay.remove();
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay').forEach(closeModalOverlay);
  }
});

document.addEventListener('click', (e) => {
  if (e.target.classList && e.target.classList.contains('modal-overlay')) {
    closeModalOverlay(e.target);
  }
  if (e.target.classList && e.target.classList.contains('modal__close')) {
    closeModalOverlay(e.target.closest('.modal-overlay'));
  }
});

// HTMX hook: after a successful form submit, close the modal and refresh the page
document.body.addEventListener('htmx:afterSwap', (e) => {
  if (e.detail.xhr.status >= 200 && e.detail.xhr.status < 300) {
    // If the response contains a meta refresh trigger, honor it
    const refreshHeader = e.detail.xhr.getResponseHeader('HX-Refresh');
    if (refreshHeader === 'true') {
      window.location.reload();
    }
  }
});

// Reset page: require typing "RESET" to enable the destructive button
const resetPhrase = document.querySelector('[data-reset-phrase]');
if (resetPhrase) {
  const phrase = resetPhrase.dataset.resetPhrase;
  const input = document.querySelector('#reset-confirm-input');
  const button = document.querySelector('#reset-submit');
  if (input && button) {
    button.disabled = true;
    input.addEventListener('input', () => {
      button.disabled = input.value.trim() !== phrase;
    });
  }
}

// Lightweight overlay modals (.ucmodal) for the project page. Toggled via the
// `hidden` attribute; close on backdrop click, [data-ucmodal-close], or Escape.
function pocOpen(id) {
  const el = document.getElementById(id);
  if (el) {
    el.hidden = false;
    document.body.classList.add('poc-modal-open');
  }
}
function pocClose(el) {
  if (!el) return;
  el.hidden = true;
  if (!document.querySelector('.ucmodal:not([hidden])')) {
    document.body.classList.remove('poc-modal-open');
  }
}
document.addEventListener('click', (e) => {
  if (e.target.classList && e.target.classList.contains('ucmodal')) {
    pocClose(e.target); // clicked the backdrop, not the box
    return;
  }
  const closer = e.target.closest && e.target.closest('[data-ucmodal-close]');
  if (closer) pocClose(closer.closest('.ucmodal'));
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.ucmodal:not([hidden])').forEach(pocClose);
  }
});

// Global search dropdown: clear/hide the suggestions when clicking outside the
// topbar search, and on Escape. The dropdown content itself is filled by HTMX.
(function () {
  const search = document.querySelector('.topbar__search');
  if (!search) return;
  const panel = search.querySelector('#search-suggest');
  const input = search.querySelector('input[name="q"]');
  const clear = () => { if (panel) panel.innerHTML = ''; };
  document.addEventListener('click', (e) => {
    if (!search.contains(e.target)) clear();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { clear(); if (input) input.blur(); }
  });
})();

// Mobile/tablet off-canvas sidebar drawer. The hamburger toggle and dim overlay
// only appear under the responsive breakpoint (see app.css); on desktop the
// sidebar is a static grid column and this code stays dormant.
(function () {
  const toggle = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  if (!toggle || !sidebar || !overlay) return;

  function open() {
    sidebar.classList.add('is-open');
    overlay.hidden = false;
    // next frame so the opacity transition runs
    requestAnimationFrame(() => overlay.classList.add('is-visible'));
    toggle.setAttribute('aria-expanded', 'true');
  }

  function close() {
    sidebar.classList.remove('is-open');
    overlay.classList.remove('is-visible');
    toggle.setAttribute('aria-expanded', 'false');
    setTimeout(() => { overlay.hidden = true; }, 200);
  }

  toggle.addEventListener('click', () => {
    sidebar.classList.contains('is-open') ? close() : open();
  });
  overlay.addEventListener('click', close);
  // Close after tapping a nav link so the drawer doesn't linger over the new page
  sidebar.querySelectorAll('.sidebar__link').forEach((link) => {
    link.addEventListener('click', close);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sidebar.classList.contains('is-open')) close();
  });
})();

// Click a .code-block (config examples) to select its whole contents for easy copy.
document.addEventListener('click', (e) => {
  const block = e.target.closest('.code-block');
  if (!block) return;
  const range = document.createRange();
  range.selectNodeContents(block);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
});

// Keep scroll position across the full-page reload that inline auto-submit forms
// trigger — e.g. changing a use case's status from the project page. Such forms
// opt in with [data-keep-scroll]; we stash the scroll offset (keyed to the path)
// just before the reload and restore it once on the next load, so the user stays
// put instead of being thrown back to the top. Runs last so it restores after any
// on-load layout shifts (e.g. the use-case status filter hiding rows).
//
// These forms auto-submit via `this.form.submit()`, and the .submit() *method*
// does NOT fire a 'submit' event — so we also capture 'change' (which the select
// fires first, before its inline onchange runs the submit). Capture phase keeps us
// ahead of the inline handler. 'submit' is kept too for any plain-button forms.
(function () {
  const KEY = 'keep-scroll';
  function save(target) {
    const form = target && target.closest ? target.closest('[data-keep-scroll]') : null;
    if (!form) return;
    try {
      sessionStorage.setItem(KEY, JSON.stringify({ path: location.pathname, y: window.scrollY }));
    } catch (err) { /* storage unavailable — position just won't be kept */ }
  }
  document.addEventListener('change', (e) => save(e.target), true);
  document.addEventListener('submit', (e) => save(e.target), true);

  try {
    const raw = sessionStorage.getItem(KEY);
    if (raw) {
      sessionStorage.removeItem(KEY);
      const saved = JSON.parse(raw);
      if (saved && saved.path === location.pathname && typeof saved.y === 'number') {
        if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
        window.scrollTo(0, saved.y);
      }
    }
  } catch (err) { /* ignore malformed/absent state */ }
})();
