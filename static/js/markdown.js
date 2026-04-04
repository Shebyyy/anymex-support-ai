if (typeof marked !== 'undefined') {
  marked.setOptions({ breaks: true, gfm: true, pedantic: false });
}

function _escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderMarkdown(text) {
  if (!text || typeof marked === 'undefined') return _escapeHtml(text || '');
  try {
    const decoded = text.replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
    const withMentions = decoded.replace(/@([\w.]+)/g, '<span class="mention">@$1</span>');
    return marked.parse(withMentions);
  } catch(e) { return _escapeHtml(text); }
}

function attachKbShortcuts(taId, wrapFn) {
  const ta = typeof taId === 'string' ? document.getElementById(taId) : taId;
  if (!ta) return;
  ta.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey) {
      if (e.key === 'b') { e.preventDefault(); wrapFn('**','**'); }
      if (e.key === 'i') { e.preventDefault(); wrapFn('_','_'); }
      if (e.key === '`') { e.preventDefault(); wrapFn('`','`'); }
    }
  });
}
