const _MENTION_MEMBERS = (function() {
  try {
    const el = document.getElementById('mention-members-data');
    if (el) return JSON.parse(el.textContent);
  } catch(e) {}
  return null;
})();

function createMentionController() {
  let _active = false, _idx = 0, _results = [], _query = '', _ta = null, _container = null, _onInsert = null;

  async function _search(q) {
    const lower = q.toLowerCase();
    if (_MENTION_MEMBERS && _MENTION_MEMBERS.length) {
      return _MENTION_MEMBERS.filter(m => {
        const hay = [
          m.username        || '',
          m.display_name    || '',
          m.global_name     || '',
          m.nick            || '',
        ].join(' ').toLowerCase();
        return hay.includes(lower);
      }).slice(0, 8);
    }
    try {
      const url = q ? `/api/members/search?q=${encodeURIComponent(q)}` : `/api/members/search?q=a`;
      const res  = await fetch(url);
      const data = await res.json();
      return Array.isArray(data) ? data : (data.results || []);
    } catch { return []; }
  }

  function _render() {
    let el = _container.querySelector('.mention-dropdown');
    if (!_results.length) { if (el) el.remove(); return; }
    if (!el) {
      el = document.createElement('div');
      el.className = 'mention-dropdown';
      _container.appendChild(el);
    }
    el.innerHTML = _results.map((m, i) => `
      <div class="mention-item ${i === _idx ? 'active' : ''}" data-i="${i}">
        <img src="${m.avatar_url || 'https://cdn.discordapp.com/embed/avatars/0.png'}"
             onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'" referrerpolicy="no-referrer"/>
        <div>
          <div class="m-name">${_escapeHtml(m.display_name || m.username)}</div>
          <div class="m-uname">@${_escapeHtml(m.username)}</div>
        </div>
        ${m.is_admin ? '<span class="m-badge admin">Admin</span>' : m.is_manager ? '<span class="m-badge mgr">Manager</span>' : ''}
      </div>`).join('');
    el.querySelectorAll('.mention-item').forEach(item => {
      item.addEventListener('mousedown', e => { e.preventDefault(); _insert(_results[+item.dataset.i]); });
    });
  }

  function _insert(m) {
    if (!_ta) return;
    const val   = _ta.value;
    const pos   = _ta.selectionStart;
    const before = val.slice(0, pos);
    const atPos  = before.lastIndexOf('@');
    const after  = val.slice(pos);
    _ta.value = before.slice(0, atPos) + '@' + m.username + ' ' + after;
    const newPos = atPos + m.username.length + 2;
    _ta.selectionStart = _ta.selectionEnd = newPos;
    _ta.focus();
    if (_onInsert) _onInsert();
    _close();
  }

  function _close() {
    _active = false; _results = []; _query = '';
    const el = _container?.querySelector('.mention-dropdown');
    if (el) el.remove();
  }

  let _debounce;
  function attach(ta, container, onInsert) {
    _ta = ta; _container = container; _onInsert = onInsert;
    ta.addEventListener('input', () => {
      const val    = ta.value;
      const pos    = ta.selectionStart;
      const before = val.slice(0, pos);
      const m      = before.match(/@([\w.]*)$/);
      if (!m) { _close(); return; }
      _query  = m[1];
      _active = true;
      clearTimeout(_debounce);
      const delay = _query.length === 0 ? 0 : 180;
      _debounce = setTimeout(async () => {
        _results = await _search(_query);
        _idx = 0;
        _render();
      }, delay);
    });
    ta.addEventListener('keydown', e => {
      if (!_active || !_results.length) return;
      if (e.key === 'ArrowDown')  { e.preventDefault(); _idx = (_idx + 1) % _results.length; _render(); }
      if (e.key === 'ArrowUp')    { e.preventDefault(); _idx = (_idx - 1 + _results.length) % _results.length; _render(); }
      if (e.key === 'Enter' || e.key === 'Tab') {
        if (_active && _results.length) { e.preventDefault(); e.stopPropagation(); _insert(_results[_idx]); }
      }
      if (e.key === 'Escape') _close();
    });
    ta.addEventListener('blur', () => setTimeout(_close, 160));
  }

  return { attach };
}

function _requestNotifPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

const _interactionEvents = ['click', 'keydown', 'touchstart'];
function _onFirstInteraction() {
  _requestNotifPermission();
  _interactionEvents.forEach(e => document.removeEventListener(e, _onFirstInteraction));
}
_interactionEvents.forEach(e => document.addEventListener(e, _onFirstInteraction));

function notifyMention(mentionedBy, todoTitle, todoId) {
  toast(`🔔 ${mentionedBy} mentioned you in TODO #${todoId}`, 'info');
  if ('Notification' in window && Notification.permission === 'granted') {
    try {
      const todoUrl = `${window.location.origin}/todo/${todoId}`;
      const n = new Notification(`${mentionedBy}`, {
        body: `${todoTitle}`,
        icon: '/favicon.ico',
        tag: `anymex-mention-${todoId}`,
        requireInteraction: false,
      });
      n.onclick = () => {
        window.focus();
        window.location.href = todoUrl;
        n.close();
      };
      setTimeout(() => n.close(), 10000);
    } catch(e) {}
  }
}

(function startMentionPoller() {
  let _lastCheck = Date.now();
  async function _poll() {
    if (!document.body.dataset.uid) return;
    try {
      const r = await fetch(`/api/notifications/mentions?since=${_lastCheck}`);
      if (!r.ok) return;
      const data = await r.json();
      _lastCheck = Date.now();
      (data.mentions || []).forEach(m => notifyMention(m.mentioned_by, m.todo_title, m.todo_id));
    } catch(e) {}
  }
  setInterval(_poll, 60000);
})();
