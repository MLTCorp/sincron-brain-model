"""Static HTML debug viewer for a Sincron Brain vault."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sincron_brain import storage
from sincron_brain.config import VaultConfig

VIEWER_FILENAME = "_viewer.html"


def write_viewer(config: VaultConfig, output: Path | None = None) -> Path:
    """Write a self-contained HTML snapshot for debugging a vault."""
    output_path = (output or config.vault_path / VIEWER_FILENAME).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_viewer_html(build_viewer_data(config)), encoding="utf-8")
    return output_path


def build_viewer_data(config: VaultConfig) -> dict[str, Any]:
    """Collect memories, tags, go_deeper edges, queues, and audit-derived sleeps."""
    with storage.open_db(config) as conn:
        stats = storage.stats(conn)
        rows = conn.execute(
            """
            SELECT id, file_path
            FROM memories
            ORDER BY score DESC, last_accessed DESC
            """
        ).fetchall()
        memories = []
        for row in rows:
            memory = storage.read_memory_file(config.vault_path / row["file_path"])
            memories.append(
                {
                    "id": memory.id,
                    "major_tags": memory.major_tags,
                    "score": memory.score,
                    "emotion_floor": memory.emotion_floor,
                    "access_count": memory.access_count,
                    "source_type": memory.source_type,
                    "asset_ref": memory.asset_ref,
                    "go_deeper": memory.go_deeper,
                    "synopsis": memory.synopsis,
                    "content": memory.content,
                    "created": memory.created.isoformat(),
                    "last_accessed": memory.last_accessed.isoformat(),
                    "last_scored": memory.last_scored.isoformat(),
                    "file_path": row["file_path"],
                }
            )
        major_tags = storage.list_major_tags(conn)

    audit = storage.read_audit(config)
    tag_counts = Counter(tag for memory in memories for tag in memory["major_tags"])
    edges = [
        {"from": memory["id"], "to": target}
        for memory in memories
        for target in memory["go_deeper"]
    ]
    queues = {
        "drafts": _queue_items(config.draft_dir),
        "reactivations": _queue_items(config.reactivation_dir),
    }
    stats = {
        **stats,
        "draft_queue": len(queues["drafts"]),
        "reactivation_queue": len(queues["reactivations"]),
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "vault_path": str(config.vault_path),
        "config": {
            "locale": config.locale,
            "judge_provider": config.judge.provider,
            "judge_model": config.judge.model,
            "decay_per_day": config.score.decay_per_day,
            "emotion_bonus_max": config.score.emotion_bonus_max,
            "audit_enabled": config.audit.enabled,
            "audit_retention_days": config.audit.retention_days,
        },
        "stats": stats,
        "major_tags": major_tags,
        "tags": [{"tag": tag, "count": count} for tag, count in tag_counts.most_common()],
        "memories": memories,
        "go_deeper_edges": edges,
        "sleeps": _sleep_runs(audit),
        "audit": audit[-500:],
        "queues": queues,
    }


def _queue_items(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    items = []
    for path in sorted(directory.glob("*.json")):
        item: dict[str, Any] = {
            "file": path.name,
            "modified": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        for key in ("id", "source_type", "hint_tags", "memory_ids", "reason", "timestamp"):
            if key in payload:
                item[key] = payload[key]
        items.append(item)
    return items


def _sleep_runs(audit: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runs = []
    current: dict[str, Any] | None = None
    for event in audit:
        name = event.get("event")
        if name == "sleep.started":
            current = {"started_at": event.get("ts"), "events": []}
            runs.append(current)
            continue
        if not isinstance(name, str) or not name.startswith("sleep."):
            continue
        if current is None:
            current = {"started_at": None, "events": []}
            runs.append(current)
        current["events"].append(event)
        if name == "sleep.finished":
            current.update(
                {
                    "finished_at": event.get("ts"),
                    "processed": event.get("processed", 0),
                    "created": event.get("created", 0),
                    "merged": event.get("merged", 0),
                    "reactivated": event.get("reactivated", 0),
                    "duration_seconds": event.get("duration_seconds", 0),
                }
            )
            current = None
    return runs


def render_viewer_html(data: dict[str, Any]) -> str:
    """Render the static viewer. All data is embedded as JSON."""
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sincron Brain Viewer</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f4;
      --panel: #ffffff;
      --ink: #18201b;
      --muted: #667067;
      --line: #d9ded6;
      --accent: #1f7a5c;
      --warn: #b7791f;
      --danger: #b42318;
      --soft: #e7f2ec;
      --radius: 8px;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); }}
    .app {{ min-height: 100vh; display: grid; grid-template-columns: 300px minmax(0, 1fr); }}
    aside {{ border-right: 1px solid var(--line); background: #eef1ea; padding: 20px; overflow: auto; }}
    main {{ padding: 20px; overflow: auto; }}
    h1 {{ font-size: 24px; margin: 0 0 8px; letter-spacing: 0; }}
    h2 {{ font-size: 17px; margin: 0 0 12px; }}
    h3 {{ font-size: 14px; margin: 0 0 8px; }}
    p {{ margin: 0; }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    .stack {{ display: grid; gap: 12px; }}
    .stats {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 16px 0; }}
    .stat {{ background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 10px; }}
    .stat b {{ display: block; font-size: 20px; }}
    label {{ display: grid; gap: 6px; font-size: 12px; color: var(--muted); }}
    input, select {{ width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 9px; background: #fff; color: var(--ink); }}
    .tabs {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }}
    button {{ border: 1px solid var(--line); background: #fff; color: var(--ink); border-radius: 6px; padding: 9px 12px; cursor: pointer; }}
    button.active {{ border-color: var(--accent); background: var(--soft); color: #0f513c; }}
    .grid {{ display: grid; grid-template-columns: minmax(320px, 460px) minmax(0, 1fr); gap: 16px; align-items: start; }}
    .list {{ display: grid; gap: 8px; }}
    .row {{ border: 1px solid var(--line); background: var(--panel); border-radius: var(--radius); padding: 12px; cursor: pointer; }}
    .row:hover, .row.selected {{ border-color: var(--accent); box-shadow: 0 0 0 2px rgba(31, 122, 92, .12); }}
    .row-title {{ display: flex; justify-content: space-between; gap: 12px; font-weight: 650; }}
    .score {{ color: var(--accent); font-weight: 700; }}
    .pillbar {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
    .pill {{ border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; background: #f8faf7; font-size: 12px; color: var(--muted); }}
    .detail, .panel {{ border: 1px solid var(--line); background: var(--panel); border-radius: var(--radius); padding: 16px; }}
    .content {{ white-space: pre-wrap; line-height: 1.55; font-size: 14px; }}
    .meta {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 12px 0; }}
    .meta div {{ border: 1px solid var(--line); border-radius: 6px; padding: 8px; font-size: 12px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px; text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ background: #eef1ea; font-size: 12px; color: var(--muted); }}
    tr:last-child td {{ border-bottom: 0; }}
    .graph {{ display: grid; gap: 10px; }}
    .edge {{ display: grid; grid-template-columns: minmax(0, 1fr) 32px minmax(0, 1fr); gap: 8px; align-items: center; }}
    .node {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 8px; min-height: 40px; }}
    .arrow {{ text-align: center; color: var(--accent); font-weight: 700; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #f8faf7; border: 1px solid var(--line); border-radius: 6px; padding: 10px; }}
    .hidden {{ display: none; }}
    @media (max-width: 900px) {{
      .app {{ grid-template-columns: 1fr; }}
      aside {{ border-right: 0; border-bottom: 1px solid var(--line); }}
      .grid, .meta {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
<div class="app">
  <aside>
    <h1>Sincron Brain</h1>
    <p class="muted" id="vaultPath"></p>
    <div class="stats" id="stats"></div>
    <div class="stack">
      <label>Busca <input id="search" type="search" placeholder="id, conteúdo, sinopse, tag"></label>
      <label>Major tag <select id="tagFilter"></select></label>
      <label>Score mínimo <input id="scoreFilter" type="number" min="0" max="100" value="0"></label>
    </div>
  </aside>
  <main>
    <div class="tabs">
      <button data-tab="memories" class="active">Memórias</button>
      <button data-tab="tags">Tags</button>
      <button data-tab="sleeps">Sleeps</button>
      <button data-tab="graph">Go deeper</button>
      <button data-tab="queues">Filas</button>
      <button data-tab="audit">Audit</button>
    </div>
    <section id="tab-memories" class="tab"></section>
    <section id="tab-tags" class="tab hidden"></section>
    <section id="tab-sleeps" class="tab hidden"></section>
    <section id="tab-graph" class="tab hidden"></section>
    <section id="tab-queues" class="tab hidden"></section>
    <section id="tab-audit" class="tab hidden"></section>
  </main>
</div>
<script id="viewer-data" type="application/json">{data_json}</script>
<script>
const DATA = JSON.parse(document.getElementById('viewer-data').textContent);
const byId = Object.fromEntries(DATA.memories.map(m => [m.id, m]));
let selectedId = DATA.memories[0]?.id || null;
const fmt = value => value === null || value === undefined || value === '' ? '-' : String(value);
const esc = value => fmt(value).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
function shortId(id) {{ return id.length > 34 ? id.slice(0, 31) + '...' : id; }}
function init() {{
  document.getElementById('vaultPath').textContent = DATA.vault_path;
  document.getElementById('stats').innerHTML = [
    ['Memórias', DATA.stats.total],
    ['Major tags', DATA.major_tags.length],
    ['Drafts', DATA.stats.draft_queue],
    ['Reativações', DATA.stats.reactivation_queue],
    ['Score médio', DATA.stats.avg_score],
    ['High-score', DATA.stats.high_score_count],
  ].map(([k,v]) => `<div class="stat"><span class="muted">${{esc(k)}}</span><b>${{esc(v)}}</b></div>`).join('');
  const tagFilter = document.getElementById('tagFilter');
  tagFilter.innerHTML = '<option value="">Todas</option>' + DATA.major_tags.map(t => `<option>${{esc(t.major_tag)}}</option>`).join('');
  ['search','tagFilter','scoreFilter'].forEach(id => document.getElementById(id).addEventListener('input', renderMemories));
  document.querySelectorAll('[data-tab]').forEach(btn => btn.addEventListener('click', () => showTab(btn.dataset.tab)));
  renderAll();
}}
function showTab(tab) {{
  document.querySelectorAll('[data-tab]').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
  document.querySelectorAll('.tab').forEach(el => el.classList.add('hidden'));
  document.getElementById('tab-' + tab).classList.remove('hidden');
}}
function filteredMemories() {{
  const q = document.getElementById('search').value.trim().toLowerCase();
  const tag = document.getElementById('tagFilter').value;
  const minScore = Number(document.getElementById('scoreFilter').value || 0);
  return DATA.memories.filter(m => {{
    const text = [m.id, m.synopsis, m.content, m.source_type, ...(m.major_tags || [])].join(' ').toLowerCase();
    return (!q || text.includes(q)) && (!tag || m.major_tags.includes(tag)) && m.score >= minScore;
  }});
}}
function renderMemories() {{
  const memories = filteredMemories();
  if (!memories.find(m => m.id === selectedId)) selectedId = memories[0]?.id || null;
  const list = memories.map(m => `
    <div class="row ${{m.id === selectedId ? 'selected' : ''}}" data-memory-id="${{esc(m.id)}}">
      <div class="row-title"><span>${{esc(m.synopsis || m.id)}}</span><span class="score">${{m.score}}</span></div>
      <div class="muted">${{esc(shortId(m.id))}} · floor ${{m.emotion_floor}} · usos ${{m.access_count}}</div>
      <div class="pillbar">${{m.major_tags.map(t => `<span class="pill">${{esc(t)}}</span>`).join('')}}</div>
    </div>`).join('') || '<div class="panel">Nenhuma memória encontrada.</div>';
  document.getElementById('tab-memories').innerHTML = `
    <div class="grid">
      <div class="list">${{list}}</div>
      ${{renderMemoryDetail(byId[selectedId])}}
    </div>`;
  document.querySelectorAll('[data-memory-id]').forEach(row => {{
    row.addEventListener('click', () => selectMemory(row.dataset.memoryId));
  }});
}}
function selectMemory(id) {{ selectedId = id; renderMemories(); }}
function renderMemoryDetail(m) {{
  if (!m) return '<div class="detail">Selecione uma memória.</div>';
  const go = (m.go_deeper || []).map(id => `<span class="pill">${{esc(id)}}</span>`).join('') || '<span class="muted">Sem links</span>';
  return `<div class="detail">
    <h2>${{esc(m.synopsis || m.id)}}</h2>
    <div class="meta">
      <div><b>ID</b><br>${{esc(m.id)}}</div>
      <div><b>Arquivo</b><br>${{esc(m.file_path)}}</div>
      <div><b>Score</b><br>${{m.score}} / floor ${{m.emotion_floor}}</div>
      <div><b>Uso</b><br>${{m.access_count}} acessos</div>
      <div><b>Criada</b><br>${{esc(m.created)}}</div>
      <div><b>Último acesso</b><br>${{esc(m.last_accessed)}}</div>
    </div>
    <h3>Tags</h3><div class="pillbar">${{m.major_tags.map(t => `<span class="pill">${{esc(t)}}</span>`).join('')}}</div>
    <h3>Go deeper</h3><div class="pillbar">${{go}}</div>
    <h3>Conteúdo</h3><div class="content">${{esc(m.content)}}</div>
  </div>`;
}}
function renderTags() {{
  document.getElementById('tab-tags').innerHTML = `
    <div class="grid">
      <div class="panel"><h2>Major tags</h2>${{table(DATA.major_tags, ['major_tag','count','max_score','avg_score'])}}</div>
      <div class="panel"><h2>Tags no vault</h2>${{table(DATA.tags, ['tag','count'])}}</div>
    </div>`;
}}
function renderSleeps() {{
  const rows = DATA.sleeps.map((s, i) => ({{
    run: i + 1,
    started_at: s.started_at,
    processed: s.processed || 0,
    created: s.created || 0,
    merged: s.merged || 0,
    reactivated: s.reactivated || 0,
    duration_seconds: s.duration_seconds || 0,
  }}));
  document.getElementById('tab-sleeps').innerHTML = `<div class="panel"><h2>Sleeps</h2>${{table(rows, ['run','started_at','processed','created','merged','reactivated','duration_seconds'])}}</div>`;
}}
function renderGraph() {{
  const edges = DATA.go_deeper_edges;
  const html = edges.length ? edges.map(e => `
    <div class="edge">
      <div class="node">${{esc(byId[e.from]?.synopsis || e.from)}}<br><span class="muted">${{esc(shortId(e.from))}}</span></div>
      <div class="arrow">→</div>
      <div class="node">${{esc(byId[e.to]?.synopsis || e.to)}}<br><span class="muted">${{esc(shortId(e.to))}}</span></div>
    </div>`).join('') : '<div class="panel">Nenhum link go_deeper registrado.</div>';
  document.getElementById('tab-graph').innerHTML = `<div class="graph">${{html}}</div>`;
}}
function renderQueues() {{
  document.getElementById('tab-queues').innerHTML = `
    <div class="grid">
      <div class="panel"><h2>Drafts</h2>${{table(DATA.queues.drafts, ['file','id','source_type','hint_tags','timestamp'])}}</div>
      <div class="panel"><h2>Reativações</h2>${{table(DATA.queues.reactivations, ['file','id','memory_ids','reason','timestamp'])}}</div>
    </div>`;
}}
function renderAudit() {{
  const rows = [...DATA.audit].reverse();
  document.getElementById('tab-audit').innerHTML = `<div class="panel"><h2>Audit recente</h2>${{table(rows, ['ts','event','memory_id','draft_id','queue_size','processed','created','merged','reactivated'])}}</div>`;
}}
function table(rows, keys) {{
  if (!rows.length) return '<p class="muted">Sem dados.</p>';
  return `<table><thead><tr>${{keys.map(k => `<th>${{esc(k)}}</th>`).join('')}}</tr></thead><tbody>${{rows.map(r => `<tr>${{keys.map(k => `<td>${{esc(Array.isArray(r[k]) ? r[k].join(', ') : r[k])}}</td>`).join('')}}</tr>`).join('')}}</tbody></table>`;
}}
function renderAll() {{
  renderMemories();
  renderTags();
  renderSleeps();
  renderGraph();
  renderQueues();
  renderAudit();
}}
init();
</script>
</body>
</html>
"""
