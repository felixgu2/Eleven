function showToast(message, iconName) {
  let toast = document.querySelector('.toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.innerHTML = '';
  if (iconName && ICONS[iconName]) {
    const iconSpan = document.createElement('span');
    iconSpan.innerHTML = ICONS[iconName]; // our own fixed icon set, never user data
    toast.appendChild(iconSpan);
  }
  const textSpan = document.createElement('span');
  textSpan.textContent = (iconName ? ' ' : '') + message; // message may include badge data - stays as safe text
  toast.appendChild(textSpan);
  toast.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove('show'), 2200);
}

// Matches the weather branch of the icon() macro in templates/_icons.html -
// needed here too because the dashboard's live weather refresh (after the
// browser grants GPS access) swaps the chip's content client-side, where
// Jinja macros aren't available.
const WEATHER_ICONS = {
  'sun': '<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4" y1="12" x2="2" y2="12"/><line x1="22" y1="12" x2="20" y2="12"/><line x1="5" y1="5" x2="6.5" y2="6.5"/><line x1="17.5" y1="17.5" x2="19" y2="19"/><line x1="19" y1="5" x2="17.5" y2="6.5"/><line x1="6.5" y1="17.5" x2="5" y2="19"/>',
  'cloud-sun': '<circle cx="7" cy="7" r="2.5"/><line x1="7" y1="2.3" x2="7" y2="1"/><line x1="3.5" y1="4.5" x2="2.6" y2="3.6"/><line x1="2.3" y1="7" x2="1" y2="7"/><path d="M9.5 19h6.5a4 4 0 0 0 .5-8 5.5 5.5 0 0 0-10.4 1.7A3.5 3.5 0 0 0 6.5 19h3z"/>',
  'cloud': '<path d="M7 18h10a4 4 0 0 0 0-8 5.5 5.5 0 0 0-10.5 1.5A3.5 3.5 0 0 0 7 18z"/>',
  'fog': '<path d="M6.5 11h9a3 3 0 1 0-2.8-4.1"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="5" y1="19" x2="19" y2="19"/>',
  'cloud-drizzle': '<path d="M7 14h10a4 4 0 0 0 0-8 5.5 5.5 0 0 0-10.4 1.5A3.5 3.5 0 0 0 7 14z"/><line x1="8" y1="18" x2="8" y2="20"/><line x1="12" y1="18" x2="12" y2="20"/><line x1="16" y1="18" x2="16" y2="20"/>',
  'cloud-rain': '<path d="M7 14h10a4 4 0 0 0 0-8 5.5 5.5 0 0 0-10.4 1.5A3.5 3.5 0 0 0 7 14z"/><line x1="8" y1="18" x2="7" y2="21"/><line x1="12" y1="18" x2="11" y2="21"/><line x1="16" y1="18" x2="15" y2="21"/>',
  'snowflake': '<line x1="12" y1="2" x2="12" y2="22"/><line x1="4" y1="7" x2="20" y2="17"/><line x1="4" y1="17" x2="20" y2="7"/>',
  'cloud-lightning': '<path d="M7 14h9a4 4 0 0 0 0-8 5.5 5.5 0 0 0-10.4 1.5A3.5 3.5 0 0 0 7 14z"/><path d="M13 14l-2 4h3l-2 4"/>',
};
function weatherIconSvg(name, cls) {
  const inner = WEATHER_ICONS[name] || WEATHER_ICONS['sun'];
  return `<svg class="icon ${cls || ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
}

// A few more icons needed client-side (matches templates/_icons.html) for
// content built dynamically in JS rather than server-rendered.
const ICONS = {
  'check-circle': '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/></svg>',
  'sparkles': '<svg class="icon icon-pop" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 2l1.2 4.8L18 8l-4.8 1.2L12 14l-1.2-4.8L6 8l4.8-1.2z"/><path d="M19 15l.6 2.4L22 18l-2.4.6L19 21l-.6-2.4L16 18l2.4-.6z"/></svg>',
  'target': '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/></svg>',
};

async function postJSON(url, data) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data || {}),
  });
  return res.json();
}

// Push the user's GPS position over a persistent WebSocket instead of
// opening a fresh HTTP request every time it changes. Pages that need
// this (dashboard, map) include the Socket.IO client script themselves
// before calling sendLocation(); other pages never pay for the connection.
let _locationSocket = null;
function sendLocation(lat, lon) {
  if (typeof io === 'undefined') return;
  if (!_locationSocket) _locationSocket = io();
  _locationSocket.emit('location_update', { lat, lon });
}

// Report the browser's real timezone once per session so the server can
// compute "today" / "this hour" correctly instead of using its own clock.
(function reportTimezone() {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (tz && sessionStorage.getItem('reported_tz') !== tz) {
      fetch('/timezone', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tz }),
      }).then(res => {
        if (res.ok) sessionStorage.setItem('reported_tz', tz);
      });
    }
  } catch (e) { /* Intl unsupported - server falls back to UTC */ }
})();
