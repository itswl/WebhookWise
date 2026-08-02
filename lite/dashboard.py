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
</style>
</head>
<body>
<header>
  <h1>WebhookWise Lite</h1>
  <span class="sub">every alert, and why it did or didn't reach you</span>
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
  const [stats, data] = await Promise.all([
    fetch('api/stats').then(r => r.json()),
    fetch('api/decisions?limit=60').then(r => r.json())
  ]);

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

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
