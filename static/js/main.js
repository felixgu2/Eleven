function showToast(message) {
  let toast = document.querySelector('.toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove('show'), 2200);
}

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
