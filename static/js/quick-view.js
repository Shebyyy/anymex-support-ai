const GV_STATUS_ICONS  = {todo:'○',in_progress:'◑',review_needed:'◇',blocked:'✕',done:'✓'};
const GV_STATUS_LABELS = {todo:'To Do',in_progress:'In Progress',review_needed:'Review Needed',blocked:'Blocked',done:'Done'};
const GV_STATUS_CLASSES= {todo:'status-todo',in_progress:'status-in_progress',review_needed:'status-review_needed',blocked:'status-blocked',done:'status-done'};
const GV_PRI_ICONS     = {low:'▽',medium:'◈',high:'▲'};
const GV_PRI_LABELS    = {low:'Low',medium:'Medium',high:'High'};
const GV_PRI_CLASSES   = {low:'pri-low',medium:'pri-medium',high:'pri-high'};

let _gvCurrentId = null;
const _gvCache   = {};

async function openQuickView(todoId) {
  _gvCurrentId = todoId;
  const modal = document.getElementById('gv-modal');
  modal.classList.add('open');

  if (!_gvCache[todoId]) {
    document.getElementById('gv-loading').style.display = 'block';
    document.getElementById('gv-comments-section').style.display = 'none';
    try {
      const res = await fetch(`/api/todo/${todoId}`);
      if (!res.ok) throw new Error('Not found');
      _gvCache[todoId] = await res.json();
    } catch(e) {
      document.getElementById('gv-loading').textContent = 'Failed to load TODO.';
      return;
    }
  }
  document.getElementById('gv-loading').style.display = 'none';
  _gvRender(_gvCache[todoId]);
  _gvLoadComments(todoId, _gvCache[todoId].status === 'done');
}

function _gvRender(t) {
  const status   = t.status || 'todo';
  const pri      = t.priority || 'medium';
  const isPublic = t.public !== false;

  document.getElementById('gv-title').textContent = `#${t.id} — ${t.title}`;

  document.getElementById('gv-status').innerHTML =
    `<span class="status-pill ${GV_STATUS_CLASSES[status]}">${GV_STATUS_ICONS[status]||'○'} ${GV_STATUS_LABELS[status]||status}</span>`;
  document.getElementById('gv-priority').innerHTML =
    `<span class="pri-icon ${GV_PRI_CLASSES[pri]}">${GV_PRI_ICONS[pri]||'◈'} ${GV_PRI_LABELS[pri]||pri}</span>`;
  document.getElementById('gv-public-badge').innerHTML =
    `<span style="font-size:.72rem;padding:3px 8px;border-radius:10px;background:${isPublic?'rgba(29,158,117,0.12)':'rgba(107,114,128,0.1)'};color:${isPublic?'var(--done)':'var(--muted)'}">${isPublic?'🌐 Public':'🔒 Private'}</span>`;

  const assignedEl       = document.getElementById('gv-assigned');
  const assignedDisplay  = t.assigned_to_name     || null;
  const assignedUsername = t.assigned_to_username || null;
  const assignedAvatar   = t.assigned_to_avatar   || 'https://cdn.discordapp.com/embed/avatars/0.png';
  if (t.assigned_to_id && (assignedDisplay || assignedUsername)) {
    assignedEl.innerHTML = `<a href="/member/${t.assigned_to_id}" class="chip">
      <img src="${assignedAvatar}" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'" style="width:22px;height:22px;border-radius:50%;flex-shrink:0" referrerpolicy="no-referrer"/>
      <span style="display:flex;flex-direction:column;gap:0">
        <span style="font-size:.78rem;font-weight:600;color:var(--text);line-height:1.3">${assignedDisplay || assignedUsername}</span>
        ${assignedUsername ? `<span style="font-size:.68rem;color:var(--muted);font-family:var(--mono);line-height:1.3">@${assignedUsername}</span>` : ''}
      </span>
    </a>`;
  } else {
    assignedEl.innerHTML = `<span style="color:var(--muted)">Unassigned</span>`;
  }

  const addedEl       = document.getElementById('gv-added');
  const addedDisplay  = t.added_by_display  || null;
  const addedUsername = t.added_by_username || null;
  const addedAvatar   = t.added_by_avatar   || 'https://cdn.discordapp.com/embed/avatars/0.png';
  if (t.added_by_id && (addedDisplay || addedUsername)) {
    addedEl.innerHTML = `<a href="/member/${t.added_by_id}" class="chip">
      <img src="${addedAvatar}" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'" style="width:22px;height:22px;border-radius:50%;flex-shrink:0" referrerpolicy="no-referrer"/>
      <span style="display:flex;flex-direction:column;gap:0">
        <span style="font-size:.78rem;font-weight:600;color:var(--text);line-height:1.3">${addedDisplay || addedUsername}</span>
        ${addedUsername ? `<span style="font-size:.68rem;color:var(--muted);font-family:var(--mono);line-height:1.3">@${addedUsername}</span>` : ''}
      </span>
    </a>`;
  } else if (t.added_by_id) {
    addedEl.innerHTML = `<span style="color:var(--muted)">Member</span>`;
  } else {
    addedEl.innerHTML = `<span style="color:var(--muted)">—</span>`;
  }

  if (t.due_date) {
    const urgency = t.due_urgency || 'normal';
    const icon = urgency==='overdue'?'⚠ ':urgency==='today'?'🕐 ':'📅 ';
    const label = t.due_label || t.due_date_fmt || (t.due_date||'').slice(0,10);
    document.getElementById('gv-due').innerHTML =
      `<span class="due-badge due-${urgency}">${icon}${label}</span>`;
  } else {
    document.getElementById('gv-due').innerHTML = `<span style="color:var(--muted);font-size:.82rem">No due date</span>`;
  }

  document.getElementById('gv-created').textContent = t.created_at_fmt || '—';

  document.getElementById('gv-tags').innerHTML = (t.tags||[]).map(tag=>`<span class="tag">${tag}</span>`).join('');

  const desc = t.ai_description || t.description || '';
  const dw = document.getElementById('gv-desc-wrap');
  dw.style.display = desc ? 'block' : 'none';
  if (desc) document.getElementById('gv-desc').textContent = desc;

  const extras = [];
  if ((t.watcher_count||0)>0) extras.push(`👁 ${t.watcher_count} watcher${t.watcher_count!==1?'s':''}`);
  if ((t.total_minutes||0)>0) extras.push(`⏱ ${(t.total_minutes/60).toFixed(1)}h logged`);
  if (t.recur) extras.push('🔁 recurring');
  document.getElementById('gv-extra').innerHTML = extras.join('<span style="color:var(--border);margin:0 6px">·</span>');

  const boardLink = document.getElementById('gv-board-link');
  const changelogLink = document.getElementById('gv-changelog-link');
  boardLink.href = `/board?highlight=${t.id}`;
  if (t.status === 'done') {
    boardLink.textContent = 'Open on Board →';
    if (changelogLink) changelogLink.style.display = 'inline-flex';
  } else {
    boardLink.textContent = 'Open on Board →';
    if (changelogLink) changelogLink.style.display = 'none';
  }

  const watchBtn = document.getElementById('gv-watch-btn');
  const wc = (t.watchers||[]).length;
  watchBtn.textContent = wc > 0 ? `👁 Watching (${wc})` : '👁 Watch';
  watchBtn.style.display = (t.status === 'done') ? 'none' : '';
}

function _gvRelTime(isoStr) {
  if (!isoStr) return '';
  try {
    const diff = Math.floor((Date.now() - new Date(isoStr)) / 1000);
    if (diff < 60)    return 'just now';
    if (diff < 3600)  return Math.floor(diff/60) + 'm ago';
    if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
    return new Date(isoStr).toLocaleDateString(undefined, {month:'short', day:'numeric'});
  } catch(e) { return ''; }
}
function _gvEsc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

let _gvReplyingTo = null;

function _gvRenderComment(c, todoId, isDone, depth) {
  const GV_UID   = document.body.dataset.uid   || '';
  const GV_LEVEL = document.body.dataset.level || 'public';
  const indentPx = Math.min(depth, 3) * 28;
  const indent = indentPx > 0 ? `margin-left:${indentPx}px;` : '';
  const avatarSize = depth === 0 ? '26px' : '20px';
  const isDeleted = c.deleted;

  if (isDeleted) {
    return `<div style="display:flex;gap:8px;align-items:flex-start;padding:6px 0;${indent}">
      <div style="width:${avatarSize};height:${avatarSize};border-radius:50%;flex-shrink:0;background:var(--surface2);border:1px solid var(--border)"></div>
      <span style="font-size:.8rem;color:var(--muted);font-style:italic;padding-top:2px">[comment deleted]</span>
    </div>`;
  }

  const canDel = (GV_UID === c.user_id || ['manager','admin','owner'].includes(GV_LEVEL)) && !isDone;
  const delBtn = canDel
    ? `<button data-cid="${c.id}" data-tid="${todoId}" onclick="gvDeleteComment(this.dataset.tid,this.dataset.cid)"
         title="Delete" class="gv-del-btn"
         style="background:none;border:none;cursor:pointer;color:var(--muted);font-size:.72rem;padding:0 2px;opacity:0;transition:opacity .15s">✕</button>`
    : '';
  const replyBtn = !isDone
    ? `<button onclick="gvStartReply('${c.id}','${_gvEsc(c.user_name)}')"
         class="gv-reply-btn"
         style="background:none;border:none;cursor:pointer;color:var(--muted);font-size:.71rem;padding:0 2px;opacity:0;transition:opacity .15s">↩ Reply</button>`
    : '';
  return `<div style="display:flex;gap:8px;align-items:flex-start;padding:8px 0;${indent}"
      onmouseenter="this.querySelectorAll('.gv-del-btn,.gv-reply-btn').forEach(b=>b.style.opacity=1)"
      onmouseleave="this.querySelectorAll('.gv-del-btn,.gv-reply-btn').forEach(b=>b.style.opacity=0)">
    <img src="${c.user_avatar||'https://cdn.discordapp.com/embed/avatars/0.png'}"
      style="width:${avatarSize};height:${avatarSize};border-radius:50%;flex-shrink:0;border:1px solid var(--border);margin-top:2px" referrerpolicy="no-referrer"/>
    <div style="flex:1;min-width:0">
      <div style="display:flex;align-items:baseline;gap:5px;margin-bottom:3px;flex-wrap:wrap">
        <span style="font-size:.78rem;font-weight:700;color:var(--text)">${_gvEsc(c.user_name)}</span>
        <span style="font-size:.7rem;color:var(--muted)">${_gvRelTime(c.ts)}</span>
        ${replyBtn}${delBtn}
      </div>
      <div style="font-size:.82rem;line-height:1.5;color:var(--text);white-space:pre-wrap;word-break:break-word">${_gvEsc(c.text)}</div>
    </div>
  </div>`;
}

async function _gvLoadComments(todoId, isDone) {
  if (!document.body.dataset.uid) return;
  const section   = document.getElementById('gv-comments-section');
  const list      = document.getElementById('gv-comments-list');
  const countEl   = document.getElementById('gv-comment-count');
  const inputWrap = document.getElementById('gv-comment-input-wrap');
  section.style.display = 'block';
  _gvReplyingTo = null;
  const replyBar = document.getElementById('gv-reply-bar');
  if (replyBar) replyBar.style.display = 'none';
  const ta = document.getElementById('gv-comment-input');
  if (ta) { ta.value = ''; ta.style.height = 'auto'; ta.placeholder = 'Write a comment…'; }
  if (inputWrap) { inputWrap.style.display = isDone ? 'none' : 'block'; }
  list.innerHTML = '<p style="font-size:.8rem;color:var(--muted)">Loading…</p>';
  try {
    const all = await (await fetch(`/api/todo/${todoId}/comments`)).json();
    const topLevel   = all.filter(c => !c.reply_to);
    const replyCount = all.length - topLevel.length;
    if (countEl) {
      const label = all.length
        ? `${topLevel.length} comment${topLevel.length!==1?'s':''}${replyCount?` · ${replyCount} repl${replyCount!==1?'ies':'y'}`:''}`
        : '';
      countEl.textContent = label;
    }
    if (!all.length) {
      list.innerHTML = `<div style="text-align:center;padding:16px 0;color:var(--muted);font-size:.82rem">
        <div style="font-size:1.3rem;margin-bottom:4px">💬</div>${isDone?'No comments.':'No comments yet.'}</div>`;
      return;
    }
    const childMap = {};
    all.forEach(c => { if (c.reply_to) { (childMap[c.reply_to] = childMap[c.reply_to]||[]).push(c); } });
    function _gvRenderThread(c, depth) {
      let h = _gvRenderComment(c, todoId, isDone, depth);
      (childMap[c.id]||[]).forEach(child => { h += _gvRenderThread(child, depth + 1); });
      return h;
    }
    let html = '';
    topLevel.forEach((c, i) => {
      html += `<div style="${i>0?'border-top:1px solid var(--border)':''}">`;
      html += _gvRenderThread(c, 0);
      html += '</div>';
    });
    list.innerHTML = html;
  } catch(e) { if(list) list.innerHTML = ''; }
}

function gvStartReply(commentId, userName) {
  _gvReplyingTo = { id: commentId, user_name: userName };
  const bar   = document.getElementById('gv-reply-bar');
  const label = document.getElementById('gv-reply-label');
  const outer = document.getElementById('gv-input-outer');
  if (bar)   bar.style.display = 'flex';
  if (label) label.textContent = `Replying to ${userName}`;
  if (outer) outer.style.borderColor = 'var(--accent)';
  const input = document.getElementById('gv-comment-input');
  if (input) { input.placeholder = `Reply to ${userName}…`; input.focus(); }
}

function gvCancelReply() {
  _gvReplyingTo = null;
  const bar   = document.getElementById('gv-reply-bar');
  const outer = document.getElementById('gv-input-outer');
  const input = document.getElementById('gv-comment-input');
  if (bar)   bar.style.display = 'none';
  if (outer) outer.style.borderColor = 'var(--border)';
  if (input) input.placeholder = 'Write a comment…';
}

function gvWrap(before, after) {
  const ta = document.getElementById('gv-comment-input');
  if (!ta) return;
  const s = ta.selectionStart, e = ta.selectionEnd;
  const sel = ta.value.slice(s, e) || 'text';
  ta.value = ta.value.slice(0, s) + before + sel + after + ta.value.slice(e);
  ta.focus();
  ta.selectionStart = s + before.length;
  ta.selectionEnd   = s + before.length + sel.length;
}

async function gvDeleteComment(todoId, commentId) {
  if (!confirm('Delete this comment? If it has replies, it will appear as [deleted].')) return;
  try {
    const res = await fetch(`/api/todo/${todoId}/comments/${commentId}`, {method:'DELETE'});
    if (!res.ok) throw new Error('Failed');
    _gvLoadComments(todoId, false);
  } catch(e) { alert('Could not delete comment.'); }
}

async function gvPostComment() {
  const input = document.getElementById('gv-comment-input');
  const text  = (input?.value||'').trim();
  if (!text || !_gvCurrentId) return;
  const body = { text };
  if (_gvReplyingTo) body.reply_to = _gvReplyingTo.id;
  try {
    const res = await fetch(`/api/todo/${_gvCurrentId}/comments`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    if (!res.ok) throw new Error('Failed');
    input.value = '';
    input.style.height = 'auto';
    _gvReplyingTo = null;
    const bar = document.getElementById('gv-reply-bar');
    if (bar) bar.style.display = 'none';
    const t = _gvCache[_gvCurrentId];
    _gvLoadComments(_gvCurrentId, t && t.status === 'done');
  } catch(e) { alert('Could not post comment.'); }
}

async function gvToggleWatch() {
  if (!_gvCurrentId) return;
  const btn = document.getElementById('gv-watch-btn');
  const isWatching = btn.textContent.includes('Watching');
  try {
    const res = await fetch(`/api/todo/${_gvCurrentId}/watch`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action: isWatching?'unwatch':'watch'})
    });
    const data = await res.json();
    const count = (data.watchers||[]).length;
    btn.textContent = count>0 ? `👁 Watching (${count})` : '👁 Watch';
  } catch(e) {}
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.getElementById('gv-modal').classList.remove('open');
  if (e.key === '/' && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
    e.preventDefault(); window.location = '/search';
  }
});

document.getElementById('gv-modal').addEventListener('keydown', e => {
  if (e.key !== 'Tab') return;
  const modal = document.getElementById('gv-modal');
  const focusable = [...modal.querySelectorAll('button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])')].filter(el => !el.disabled && el.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (e.shiftKey) { if (document.activeElement === first) { e.preventDefault(); last.focus(); } }
  else { if (document.activeElement === last) { e.preventDefault(); first.focus(); } }
});
