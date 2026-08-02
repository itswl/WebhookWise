"""The dashboard: one page whose only job is answering "why".

Deliberately dependency-free (no build step, no CDN) so the container stays
self-contained and the page keeps working on an air-gapped network.
"""

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WebhookWise Lite</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --line:#272b35; --text:#e6e8ee; --muted:#9aa3b2;
          --ok:#31c48d; --warn:#f0a02f; --dim:#6b7280; --accent:#5b8def; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif; }
  header { padding:20px 24px; border-bottom:1px solid var(--line); display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
  h1 { font-size:17px; margin:0; font-weight:600; }
  .sub { color:var(--muted); font-size:13px; }
  main { padding:20px 24px; max-width:1100px; }
  .stats { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:22px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 14px; min-width:104px; }
  .stat b { display:block; font-size:20px; font-weight:600; }
  .stat span { color:var(--muted); font-size:12px; }
  .row { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:9px; }
  .head { display:flex; gap:10px; align-items:center; flex-wrap:wrap; cursor:pointer; }
  .tag { font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid var(--line); white-space:nowrap; }
  .fwd { color:var(--ok); border-color:rgba(49,196,141,.4); }
  .skip { color:var(--warn); border-color:rgba(240,160,47,.35); }
  .title { flex:1; min-width:220px; font-weight:500; }
  .meta { color:var(--muted); font-size:12px; white-space:nowrap; }
  .steps { margin-top:10px; padding-top:10px; border-top:1px dashed var(--line); display:none; }
  .steps.open { display:block; }
  .step { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; color:var(--muted); padding:2px 0; }
  .step b { color:var(--text); font-weight:500; }
  .empty { color:var(--muted); padding:40px 0; text-align:center; }
  code { background:#11141a; padding:1px 5px; border-radius:4px; }
  .controls { margin-left:auto; display:flex; align-items:center; gap:10px; font-size:12px; color:var(--muted); }
  .controls select, .controls button {
    background:var(--card); color:var(--text); border:1px solid var(--line);
    border-radius:7px; padding:4px 9px; font-size:12px; font-family:inherit; cursor:pointer;
  }
  .controls button:hover, .controls select:hover { border-color:var(--accent); }
  #updated { font-variant-numeric:tabular-nums; }
  #updated.stale { color:var(--warn); }
</style>
</head>
<body>
<header>
  <h1>WebhookWise Lite</h1>
  <span class="sub">every alert, and why it did or didn't reach you</span>
  <div class="controls">
    <span id="updated">—</span>
    <select id="interval" title="Auto-refresh interval">
      <option value="0">manual</option>
      <option value="10">10s</option>
      <option value="30">30s</option>
      <option value="120">2m</option>
      <option value="600">10m</option>
    </select>
    <button id="refresh" type="button">Refresh</button>
  </div>
</header>
<main>
  <div class="stats" id="stats"></div>
  <div id="list"><div class="empty">loading…</div></div>
</main>
<script>
const LABEL = {
  forwarded: 'forwarded', duplicate: 'duplicate', silenced: 'silenced',
  cooldown: 'cooldown', no_match: 'no rule matched'
};

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function stepLine(s) {
  const rest = Object.entries(s)
    .filter(([k, v]) => k !== 'step' && k !== 'result' && v !== null && v !== undefined)
    .map(([k, v]) => k + '=' + (typeof v === 'object' ? JSON.stringify(v) : v))
    .join('  ');
  return '<div class="step"><b>' + esc(s.step) + '</b> → ' + esc(s.result) + (rest ? '  ' + esc(rest) : '') + '</div>';
}

async function refresh() {
  let stats, data;
  try {
    [stats, data] = await Promise.all([
      fetch('api/stats').then(r => r.json()),
      fetch('api/decisions?limit=60').then(r => r.json())
    ]);
  } catch (e) {
    // Surface it: a silently failed refresh looks exactly like "nothing new",
    // which is the one thing this dashboard must never imply.
    const el = document.getElementById('updated');
    el.textContent = 'refresh failed';
    el.classList.add('stale');
    return;
  }
  lastUpdated = Date.now();
  stamp();

  const s = stats.last24h || {};
  const order = ['forwarded', 'duplicate', 'silenced', 'cooldown', 'no_match'];
  const keys = order.filter(k => k in s).concat(Object.keys(s).filter(k => !order.includes(k)));
  document.getElementById('stats').innerHTML = (keys.length ? keys : ['forwarded'])
    .map(k => '<div class="stat"><b>' + (s[k] || 0) + '</b><span>' + esc(LABEL[k] || k) + '</span></div>').join('')
    + '<div class="stat"><b>' + ((stats.outbox || {}).pending || 0) + '</b><span>outbox pending</span></div>'
    + '<div class="stat"><b>' + ((stats.outbox || {}).exhausted || 0) + '</b><span>outbox failed</span></div>';

  const rows = data.decisions || [];
  document.getElementById('list').innerHTML = rows.length ? rows.map((d, i) => {
    const fwd = d.outcome === 'forwarded';
    const label = fwd ? 'forwarded' : (LABEL[d.skip_code] || d.skip_code);
    return '<div class="row">'
      + '<div class="head" onclick="document.getElementById(\\'s' + i + '\\').classList.toggle(\\'open\\')">'
      + '<span class="tag ' + (fwd ? 'fwd' : 'skip') + '">' + esc(label) + '</span>'
      + '<span class="title">' + esc(d.title) + '</span>'
      + '<span class="meta">' + esc(d.source) + (d.importance ? ' · ' + esc(d.importance) : '')
      + (d.route === 'rule' ? ' · rule-triage' : d.route === 'ai' ? ' · ai' : '')
      + ' · ' + new Date(d.at * 1000).toLocaleTimeString() + '</span></div>'
      + '<div class="steps" id="s' + i + '">'
      + (d.summary ? '<div class="step"><b>summary</b> ' + esc(d.summary) + '</div>' : '')
      + (d.steps || []).map(stepLine).join('')
      + (d.matched && d.matched.length ? '<div class="step"><b>rules</b> ' + esc(d.matched.join(', ')) + '</div>' : '')
      + '</div></div>';
  }).join('') : '<div class="empty">No alerts yet. Send one:<br><br>'
      + '<code>curl -X POST localhost:8000/webhook/demo -H "content-type: application/json" '
      + '-d \\'{"title":"disk full","body":"/ is at 95%"}\\'</code></div>';
}

// Refresh scheduling. Default 30s rather than a tight poll: alerts arrive
// minutes apart, so a faster loop only burns requests. "manual" turns the
// timer off entirely — hence the always-visible last-updated stamp, without
// which a paused dashboard is indistinguishable from a quiet one.
const DEFAULT_INTERVAL = 30;
const STORAGE_KEY = 'wwlite.refreshInterval';
let timer = null;
let lastUpdated = null;

function stamp() {
  const el = document.getElementById('updated');
  if (!lastUpdated) { el.textContent = '—'; el.classList.remove('stale'); return; }
  const age = Math.round((Date.now() - lastUpdated) / 1000);
  el.textContent = age < 60 ? 'updated ' + age + 's ago'
                 : 'updated ' + Math.floor(age / 60) + 'm ago';
  // Only meaningful while a timer should be keeping things current.
  el.classList.toggle('stale', currentInterval() > 0 && age > currentInterval() * 2);
}

function currentInterval() {
  return parseInt(document.getElementById('interval').value, 10) || 0;
}

function schedule() {
  if (timer) { clearInterval(timer); timer = null; }
  const seconds = currentInterval();
  localStorage.setItem(STORAGE_KEY, String(seconds));
  // A hidden tab polling in the background helps nobody; visibilitychange
  // re-arms it and refreshes once so the view is current when you return.
  if (seconds > 0 && !document.hidden) timer = setInterval(refresh, seconds * 1000);
}

document.getElementById('interval').addEventListener('change', schedule);
document.getElementById('refresh').addEventListener('click', () => refresh());
document.addEventListener('visibilitychange', () => {
  schedule();
  if (!document.hidden) refresh();
});

const saved = localStorage.getItem(STORAGE_KEY);
document.getElementById('interval').value = saved === null ? String(DEFAULT_INTERVAL) : saved;
setInterval(stamp, 1000);
schedule();
refresh();
</script>
</body>
</html>
"""
