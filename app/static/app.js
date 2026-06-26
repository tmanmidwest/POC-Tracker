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

// Modal: close on Escape, close on overlay click
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay').forEach((el) => el.remove());
  }
});

document.addEventListener('click', (e) => {
  if (e.target.classList && e.target.classList.contains('modal-overlay')) {
    e.target.remove();
  }
  if (e.target.classList && e.target.classList.contains('modal__close')) {
    const overlay = e.target.closest('.modal-overlay');
    if (overlay) overlay.remove();
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
