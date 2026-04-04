const QUICK_EMOJIS = ['👍','❤️','🔥','✅','😂','😮','🎉','👀'];

const EMOJI_CATEGORIES = [
  { label: '⚡ Quick', emojis: ['👍','❤️','🔥','✅','😂','😮','🎉','👀','💯','🙏','👏','🤝'] },
  { label: '😀 Faces', emojis: ['😀','😁','😂','🤣','😃','😄','😅','😆','😊','😇','🙂','🙃','😉','😌','😍','🥰','😘','😗','😙','😚','😋','😛','😜','🤪','😝','🤑','🤗','🤭','🤫','🤔','🤐','🤨','😐','😑','😶','😏','😒','🙄','😬','🤥','😌','😔','😪','🤤','😴','😷','🤒','🤕','🤢','🤮','🤧','🥵','🥶','🥴','😵','🤯','🤠','🥸','😎','🤓','🧐','😕','😟','🙁','☹️','😮','😯','😲','😳','🥺','😦','😧','😨','😰','😥','😢','😭','😱','😖','😣','😞','😓','😩','😫','🥱','😤','😡','😠','🤬','😈','👿'] },
  { label: '👍 Hands', emojis: ['👍','👎','👌','🤌','🤏','✌️','🤞','🤟','🤘','🤙','👈','👉','👆','🖕','👇','☝️','👋','🤚','🖐️','✋','🖖','👏','🙌','🤲','🤜','🤛','✊','👊','🤝','🙏','✍️','💪','🦾','🦿','🦵','🦶','👂','🦻','👃','🫀','🫁','🧠','🦷','🦴','👀','👁','👅','👄','💋'] },
  { label: '❤️ Hearts', emojis: ['❤️','🧡','💛','💚','💙','💜','🖤','🤍','🤎','❤️‍🔥','❤️‍🩹','💔','💕','💞','💓','💗','💖','💘','💝','💟','♥️','💌','💋'] },
  { label: '🎉 Celebrate', emojis: ['🎉','🎊','🥳','🎈','🎁','🎀','🥂','🍾','🎆','🎇','✨','⭐','🌟','💫','🏆','🥇','🥈','🥉','🏅','🎖','🎗','🎫','🎟','🎪','🎭','🎨','🎬','🎤','🎧','🎼','🎹','🎸','🎺','🎻','🥁','🎷'] },
  { label: '🔥 Hype', emojis: ['🔥','💥','⚡','🌊','🌪','🌈','☀️','⭐','🌙','❄️','💨','🌸','🌺','🍀','🌿','🦋','🐉','🦄','👑','💎','🚀','💡','🔑','⚔️','🛡','🎯','💯','✅','❌','⚠️','🆙','🆒','🆕','🔝','💢','💬','💭','🗯'] },
  { label: '🐶 Animals', emojis: ['🐶','🐱','🐭','🐹','🐰','🦊','🐻','🐼','🐨','🐯','🦁','🐮','🐷','🐸','🐵','🙈','🙉','🙊','🐔','🐧','🐦','🦆','🦅','🦉','🦇','🐺','🐗','🦝','🦙','🦘','🦡','🦨','🦦','🦥','🐿','🦔','🐾','🐲','🐉','🌵','🎄','🌲','🌳','🐬','🐋','🦈','🐙','🦑','🦐','🦞','🦀','🦞'] },
  { label: '🍕 Food', emojis: ['🍕','🍔','🌮','🌯','🥙','🧆','🍳','🥞','🧇','🥓','🍖','🍗','🥩','🍠','🥚','🧀','🥗','🥘','🍲','🍜','🍝','🍛','🍣','🍱','🥟','🦪','🍤','🍙','🍚','🍘','🍥','🥮','🍡','🧁','🍰','🎂','🍮','🍭','🍬','🍫','🍿','🍩','🍪','🌰','🥜','🍯','🥤','🧃','☕','🍵','🧋','🍺','🍻','🥂','🍷'] },
];

function renderReactions(reactions, commentId, currentUid, onToggle) {
  if (!reactions || !Object.keys(reactions).length) return '';
  const chips = Object.entries(reactions).map(([emoji, users]) => {
    const mine = users.includes(currentUid);
    const count = users.length;
    return `<span class="reaction-chip ${mine?'mine':''}" 
      title="${users.length} reaction${users.length!==1?'s':''}"
      onclick="(${onToggle.toString()})('${commentId}','${emoji}')"
    >${emoji} <span class="r-count">${count}</span></span>`;
  }).join('');
  return chips;
}

let _discordEmojisCache = null;
async function _loadDiscordEmojis() {
  if (_discordEmojisCache !== null) return _discordEmojisCache;

  try {
    const stored = sessionStorage.getItem('anymex_guild_emojis');
    if (stored) {
      const parsed = JSON.parse(stored);
      if (parsed._ts && (Date.now() - parsed._ts) < 3600000) {
        _discordEmojisCache = parsed.emojis || [];
        return _discordEmojisCache;
      }
    }
  } catch(e) {}

  try {
    const res  = await fetch('/api/emojis');
    const data = await res.json();
    _discordEmojisCache = data.emojis || [];
    try {
      sessionStorage.setItem('anymex_guild_emojis', JSON.stringify({
        emojis: _discordEmojisCache,
        _ts: Date.now(),
      }));
    } catch(e) {}
  } catch(e) {
    _discordEmojisCache = [];
  }
  return _discordEmojisCache;
}
_loadDiscordEmojis();

function openEmojiPicker(anchorEl, commentId, onPick) {
  document.querySelectorAll('.emoji-picker-popup').forEach(p => p.remove());

  const picker = document.createElement('div');
  picker.className = 'emoji-picker-popup emoji-picker-full';

  const searchWrap = document.createElement('div');
  searchWrap.className = 'ep-search-wrap';
  const searchInput = document.createElement('input');
  searchInput.placeholder = '🔍 Search emojis…';
  searchInput.className = 'ep-search';
  searchWrap.appendChild(searchInput);
  picker.appendChild(searchWrap);

  const tabs = document.createElement('div');
  tabs.className = 'ep-tabs';
  picker.appendChild(tabs);

  const grid = document.createElement('div');
  grid.className = 'ep-grid';
  picker.appendChild(grid);

  let _allCategories = [...EMOJI_CATEGORIES];
  let _activeCatIdx  = 0;

  function _renderTabBar() {
    tabs.innerHTML = '';
    _allCategories.forEach((cat, ci) => {
      const tab = document.createElement('button');
      tab.className = 'ep-tab' + (ci === _activeCatIdx ? ' active' : '');
      tab.title = cat.label;
      if (cat.serverEmojis) {
        tab.innerHTML = '<span style="font-size:.7rem;font-weight:700">Server</span>';
      } else {
        tab.textContent = cat.emojis[0];
      }
      tab.onclick = (e) => {
        e.stopPropagation();
        _activeCatIdx = ci;
        _renderTabBar();
        _renderCategory(ci);
      };
      tabs.appendChild(tab);
    });
  }

  function _renderCategory(ci) {
    const cat = _allCategories[ci];
    grid.innerHTML = '';
    if (cat.serverEmojis) {
      cat.serverEmojis.forEach(e => {
        const span = document.createElement('span');
        span.className = 'ep-emoji ep-discord-emoji';
        span.title     = `:${e.name}:`;
        span.innerHTML = `<img src="${e.url}" alt=":${e.name}:" style="width:22px;height:22px;object-fit:contain;display:block">`;
        span.onclick   = (ev) => { ev.stopPropagation(); onPick(commentId, e.animated ? `<a:${e.name}:${e.id}>` : `<:${e.name}:${e.id}>`); picker.remove(); };
        grid.appendChild(span);
      });
      if (!cat.serverEmojis.length) {
        grid.innerHTML = '<div style="padding:16px;font-size:.8rem;color:var(--muted);text-align:center">No server emojis found</div>';
      }
    } else {
      cat.emojis.forEach(emoji => {
        const span = document.createElement('span');
        span.className   = 'ep-emoji';
        span.textContent = emoji;
        span.title       = emoji;
        span.onclick     = (e) => { e.stopPropagation(); onPick(commentId, emoji); picker.remove(); };
        grid.appendChild(span);
      });
    }
  }

  searchInput.addEventListener('input', () => {
    const q = searchInput.value.toLowerCase().trim();
    if (!q) { _renderCategory(_activeCatIdx); return; }
    grid.innerHTML = '';
    const allUnicode = _allCategories.filter(c => !c.serverEmojis).flatMap(c => c.emojis);
    [...new Set(allUnicode)].forEach(emoji => {
      const span = document.createElement('span');
      span.className   = 'ep-emoji';
      span.textContent = emoji;
      span.onclick     = (e) => { e.stopPropagation(); onPick(commentId, emoji); picker.remove(); };
      grid.appendChild(span);
    });
    const serverCat = _allCategories.find(c => c.serverEmojis);
    if (serverCat) {
      serverCat.serverEmojis.filter(e => e.name.toLowerCase().includes(q)).forEach(e => {
        const span = document.createElement('span');
        span.className = 'ep-emoji ep-discord-emoji';
        span.title     = `:${e.name}:`;
        span.innerHTML = `<img src="${e.url}" alt=":${e.name}:" style="width:22px;height:22px;object-fit:contain;display:block">`;
        span.onclick   = (ev) => { ev.stopPropagation(); onPick(commentId, e.animated ? `<a:${e.name}:${e.id}>` : `<:${e.name}:${e.id}>`); picker.remove(); };
        grid.appendChild(span);
      });
    }
  });

  const discordEmojis = _discordEmojisCache || [];
  if (discordEmojis.length > 0) {
    _allCategories = [
      { label: '🖥 Server', serverEmojis: discordEmojis },
      ...EMOJI_CATEGORIES,
    ];
    _activeCatIdx = 0;
  }
  _renderTabBar();
  _renderCategory(_activeCatIdx);

  if (_discordEmojisCache === null) {
    _loadDiscordEmojis().then(emojis => {
      if (!emojis.length) return;
      _allCategories = [
        { label: '🖥 Server', serverEmojis: emojis },
        ...EMOJI_CATEGORIES,
      ];
      _renderTabBar();
      _renderCategory(_activeCatIdx);
    });
  }

  document.body.appendChild(picker);
  const rect = anchorEl.getBoundingClientRect();
  const ph   = picker.offsetHeight || 300;
  const spaceBelow = window.innerHeight - rect.bottom;
  const top  = spaceBelow > ph + 10 ? rect.bottom + 6 : rect.top - ph - 6;
  let left   = rect.left;
  if (left + 300 > window.innerWidth) left = window.innerWidth - 310;
  picker.style.cssText += `;position:fixed;top:${Math.max(8,top)}px;left:${Math.max(8,left)}px`;

  searchInput.focus();
  setTimeout(() => document.addEventListener('click', (e) => { if (!picker.contains(e.target)) picker.remove(); }, { once: true }), 10);
}
