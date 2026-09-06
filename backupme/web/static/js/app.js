/* ═══════════════════════════════════════════════════════════
   BackupMe Dashboard — Core Application JS
   ═══════════════════════════════════════════════════════════ */

const App = (() => {
  const state = {
    pollers: [],
    theme: localStorage.getItem('bm-theme') || 'dark',
  };

  /* ── Theme Manager ───────────────────────────────────── */
  const ThemeManager = {
    init() {
      document.documentElement.setAttribute('data-theme', state.theme);
      this._updateIcon();
    },

    toggle() {
      state.theme = state.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', state.theme);
      localStorage.setItem('bm-theme', state.theme);
      this._updateIcon();
      window.dispatchEvent(new CustomEvent('themechange', { detail: state.theme }));
    },

    get current() {
      return state.theme;
    },

    _updateIcon() {
      const btn = document.getElementById('theme-toggle-btn');
      if (!btn) return;
      const isDark = state.theme === 'dark';
      btn.innerHTML = isDark
        ? '<i data-lucide="sun" style="width:18px;height:18px;"></i>'
        : '<i data-lucide="moon" style="width:18px;height:18px;"></i>';
      if (window.lucide) lucide.createIcons({ nodes: [btn] });
    }
  };

  /* ── Toast Notifications ─────────────────────────────── */
  const Toast = {
    show(message, type = 'info', duration = 4000) {
      const container = document.getElementById('toast-container');
      if (!container) return;

      const icons = {
        success: 'check-circle-2',
        error: 'x-circle',
        warning: 'alert-triangle',
        info: 'info',
      };

      const toast = document.createElement('div');
      toast.className = `toast toast-${type}`;
      toast.innerHTML = `
        <i data-lucide="${icons[type]}" class="toast-icon"></i>
        <span class="toast-message">${message}</span>
        <button class="toast-close" onclick="App.Toast.dismiss(this.parentElement)">
          <i data-lucide="x" style="width:14px;height:14px;"></i>
        </button>
      `;

      container.appendChild(toast);
      if (window.lucide) lucide.createIcons({ nodes: [toast] });

      if (duration > 0) {
        setTimeout(() => this.dismiss(toast), duration);
      }

      return toast;
    },

    success(msg, duration) { return this.show(msg, 'success', duration); },
    error(msg, duration) { return this.show(msg, 'error', duration); },
    warning(msg, duration) { return this.show(msg, 'warning', duration); },
    info(msg, duration) { return this.show(msg, 'info', duration); },

    dismiss(toast) {
      if (!toast || toast.classList.contains('removing')) return;
      toast.classList.add('removing');
      setTimeout(() => toast.remove(), 250);
    }
  };

  /* ── Modal System ────────────────────────────────────── */
  const Modal = {
    open(id) {
      const overlay = document.getElementById(id);
      if (!overlay) return;
      overlay.classList.add('visible');
      document.body.style.overflow = 'hidden';

      overlay._clickHandler = (e) => {
        if (e.target === overlay) this.close(id);
      };
      overlay.addEventListener('click', overlay._clickHandler);
    },

    close(id) {
      const overlay = document.getElementById(id);
      if (!overlay) return;
      overlay.classList.remove('visible');
      document.body.style.overflow = '';
      if (overlay._clickHandler) {
        overlay.removeEventListener('click', overlay._clickHandler);
      }
    },

    confirm(title, message, onConfirm, confirmText = 'Confirm', danger = false) {
      let existing = document.getElementById('confirm-modal');
      if (existing) existing.remove();

      const overlay = document.createElement('div');
      overlay.id = 'confirm-modal';
      overlay.className = 'modal-overlay';
      overlay.innerHTML = `
        <div class="modal" style="max-width: 420px;">
          <div class="modal-header">
            <h2>${escapeHtml(title)}</h2>
            <button class="modal-close" onclick="App.Modal.close('confirm-modal')">
              <i data-lucide="x" style="width:18px;height:18px;"></i>
            </button>
          </div>
          <p style="color: var(--text-secondary); font-size: 0.9rem;">${message}</p>
          <div class="modal-footer">
            <button class="btn btn-secondary" onclick="App.Modal.close('confirm-modal')">Cancel</button>
            <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" id="confirm-modal-btn">${escapeHtml(confirmText)}</button>
          </div>
        </div>
      `;

      document.body.appendChild(overlay);
      if (window.lucide) lucide.createIcons({ nodes: [overlay] });

      document.getElementById('confirm-modal-btn').addEventListener('click', () => {
        this.close('confirm-modal');
        if (onConfirm) onConfirm();
      });

      requestAnimationFrame(() => this.open('confirm-modal'));
    }
  };

  /* ── Polling System ──────────────────────────────────── */
  const Poller = {
    start(url, callback, intervalMs = 10000, options = {}) {
      const poller = {
        id: Symbol(),
        url,
        callback,
        interval: intervalMs,
        timer: null,
        active: true,
        ...options,
      };

      const execute = async () => {
        if (!poller.active) return;
        try {
          const resp = await fetch(url);
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          const data = await resp.json();
          if (poller.active) callback(data);
        } catch (err) {
          if (poller.active && options.onError) options.onError(err);
        }
      };

      execute();
      poller.timer = setInterval(execute, intervalMs);
      state.pollers.push(poller);
      return poller.id;
    },

    stop(id) {
      const idx = state.pollers.findIndex(p => p.id === id);
      if (idx !== -1) {
        state.pollers[idx].active = false;
        clearInterval(state.pollers[idx].timer);
        state.pollers.splice(idx, 1);
      }
    },

    stopAll() {
      state.pollers.forEach(p => {
        p.active = false;
        clearInterval(p.timer);
      });
      state.pollers = [];
    }
  };

  /* ── Format Utilities ────────────────────────────────── */
  function formatBytes(bytes, decimals = 1) {
    if (bytes === 0 || bytes == null) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    const i = Math.floor(Math.log(Math.abs(bytes)) / Math.log(k));
    const val = bytes / Math.pow(k, i);
    return val.toFixed(decimals) + ' ' + sizes[i];
  }

  function formatDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const now = new Date();
    const diff = (now - d) / 1000;

    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';

    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  }

  function formatTimestamp(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  }

  function formatNumber(n) {
    if (n == null) return '0';
    return Number(n).toLocaleString('en-US');
  }

  /* ── Clipboard ───────────────────────────────────────── */
  async function copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
      Toast.success('Copied to clipboard');
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;opacity:0;';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      Toast.success('Copied to clipboard');
    }
  }

  /* ── Debounce ────────────────────────────────────────── */
  function debounce(fn, ms) {
    let timer;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  /* ── Escape HTML ────────────────────────────────────── */
  function escapeHtml(str) {
    const el = document.createElement('span');
    el.textContent = str;
    return el.innerHTML;
  }

  /* ── Fetch API Wrapper ───────────────────────────────── */
  async function fetchAPI(url, options = {}) {
    try {
      const defaults = {
        headers: { 'Content-Type': 'application/json' },
      };
      const resp = await fetch(url, { ...defaults, ...options });
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.detail || data.message || `HTTP ${resp.status}`);
      }
      return data;
    } catch (err) {
      Toast.error(err.message || 'Request failed');
      throw err;
    }
  }

  /* ── Sidebar ─────────────────────────────────────────── */
  function toggleSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const overlay = document.querySelector('.sidebar-overlay');
    sidebar.classList.toggle('open');
    overlay.classList.toggle('visible');
  }

  function closeSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const overlay = document.querySelector('.sidebar-overlay');
    sidebar.classList.remove('open');
    overlay.classList.remove('visible');
  }

  /* ── Global Search ───────────────────────────────────── */
  function initGlobalSearch() {
    const input = document.getElementById('global-search');
    if (!input) return;

    document.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        input.focus();
      }
      if (e.key === 'Escape') {
        input.blur();
        input.value = '';
      }
    });

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && input.value.trim()) {
        window.location.href = '/files?q=' + encodeURIComponent(input.value.trim());
      }
    });
  }

  /* ── Accordion ───────────────────────────────────────── */
  function initAccordions() {
    document.querySelectorAll('.accordion-header').forEach(header => {
      header.addEventListener('click', () => {
        const item = header.closest('.accordion-item');
        item.classList.toggle('open');
      });
    });
  }

  /* ── Lucide Icons Init ───────────────────────────────── */
  function initIcons() {
    if (window.lucide) {
      lucide.createIcons();
    }
  }

  /* ── Page Init ───────────────────────────────────────── */
  function init() {
    ThemeManager.init();
    initIcons();
    initGlobalSearch();

    const sidebarOverlay = document.querySelector('.sidebar-overlay');
    if (sidebarOverlay) {
      sidebarOverlay.addEventListener('click', closeSidebar);
    }

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        const openModals = document.querySelectorAll('.modal-overlay.visible');
        openModals.forEach(m => Modal.close(m.id));
      }
    });
  }

  document.addEventListener('DOMContentLoaded', init);

  return {
    ThemeManager,
    Toast,
    Modal,
    Poller,
    formatBytes,
    formatDate,
    formatTimestamp,
    formatNumber,
    copyToClipboard,
    debounce,
    escapeHtml,
    fetchAPI,
    toggleSidebar,
    closeSidebar,
    initAccordions,
    initIcons,
  };
})();
