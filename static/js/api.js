function toast(msg, type='success') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${type === 'success' ? '✓' : '✕'}</span> ${msg}`;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || 'Request failed');
  return data;
}

function attachPasteImage(ta, previewContainer, onImageChange) {
  ta.addEventListener('paste', e => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (!item.type.startsWith('image/')) continue;
      e.preventDefault();
      const file = item.getAsFile();
      const reader = new FileReader();
      reader.onload = () => {
        onImageChange(reader.result, file.type);
        if (previewContainer) {
          previewContainer.innerHTML = `
            <div class="paste-img-preview">
              <img src="${reader.result}" alt="pasted"/>
              <button class="remove-img" onclick="this.closest('.paste-img-preview').remove();(${onImageChange.toString()})(null)" title="Remove">✕</button>
            </div>`;
        }
      };
      reader.readAsDataURL(file);
      break;
    }
  });
}

function markCommentsRead(todoId, latestTs) {
  try { localStorage.setItem(`anymex_read_${todoId}`, latestTs||Date.now()); } catch {}
}
function getLastRead(todoId) {
  try { return parseInt(localStorage.getItem(`anymex_read_${todoId}`)||'0', 10); } catch { return 0; }
}
function hasUnread(todoId, latestTs) {
  if (!latestTs) return false;
  return new Date(latestTs).getTime() > getLastRead(todoId);
}
