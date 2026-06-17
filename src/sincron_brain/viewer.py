"""Static HTML debug viewer for a Sincron Brain vault."""

from __future__ import annotations

import json
from base64 import b64encode
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from sincron_brain import judge as judge_module
from sincron_brain import storage
from sincron_brain.config import VaultConfig

VIEWER_FILENAME = "_viewer.html"
LOGO_RESOURCE = "assets/logo-sincronia.jpg"


def write_viewer(
    config: VaultConfig,
    output: Path | None = None,
    limit: int | None = None,
    summary_only: bool = True,
) -> Path:
    """Write a self-contained HTML snapshot for debugging a vault."""
    output_path = (output or config.vault_path / VIEWER_FILENAME).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_viewer_html(
            build_viewer_data(config, limit=limit, summary_only=summary_only)
        ),
        encoding="utf-8",
    )
    return output_path


def build_viewer_data(
    config: VaultConfig,
    limit: int | None = None,
    summary_only: bool = True,
) -> dict[str, Any]:
    """Collect memories, tags, go_deeper edges, queues, and audit-derived sleeps."""
    if limit is not None and limit <= 0:
        raise ValueError("limit must be greater than 0")

    with storage.open_db(config) as conn:
        stats = storage.stats(conn)
        sql = """
            SELECT id, major_tags, tags, score, created, last_accessed, last_scored,
                   access_count, emotion_floor, source_type, asset_ref, go_deeper,
                   synopsis, file_path
            FROM memories
            ORDER BY score DESC, last_accessed DESC
            """
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = conn.execute(sql, params).fetchall()
        memories = []
        for row in rows:
            content = "" if summary_only else _read_markdown_body(config.vault_path / row["file_path"])
            memories.append(
                {
                    "id": row["id"],
                    "major_tags": json.loads(row["major_tags"]),
                    "tags": json.loads(row["tags"]),
                    "score": row["score"],
                    "emotion_floor": row["emotion_floor"],
                    "access_count": row["access_count"],
                    "source_type": row["source_type"],
                    "asset_ref": row["asset_ref"],
                    "go_deeper": json.loads(row["go_deeper"]),
                    "synopsis": row["synopsis"],
                    "content": content,
                    "content_omitted": summary_only,
                    "created": row["created"],
                    "last_accessed": row["last_accessed"],
                    "last_scored": row["last_scored"],
                    "file_path": row["file_path"],
                }
            )
        major_tags = storage.list_major_tags(conn)
        common_tags = storage.list_common_tags(conn)

    audit = storage.read_audit(config)
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
        "viewer": {
            "memory_limit": limit,
            "summary_only": summary_only,
            "displayed_memories": len(memories),
            "total_memories": stats.get("total", len(memories)),
            "omitted_memories": max(0, stats.get("total", len(memories)) - len(memories)),
        },
        "config": {
            "locale": config.locale,
            "judge_provider": config.judge.provider,
            "judge_model": config.judge.model,
            "decay_per_day": config.score.decay_per_day,
            "emotion_bonus_max": config.score.emotion_bonus_max,
            "audit_enabled": config.audit.enabled,
            "audit_retention_days": config.audit.retention_days,
        },
        "judge_status": judge_module.judge_status(config),
        "branding": {
            "logo_data_uri": _logo_data_uri(),
            "developer": "Sincron IA",
            "website": "sincronia.digital",
            "author": "Matheus Massari",
        },
        "stats": stats,
        "major_tags": major_tags,
        "tags": common_tags,
        "memories": memories,
        "go_deeper_edges": edges,
        "sleeps": _sleep_runs(audit),
        "audit": audit[-500:],
        "queues": queues,
    }


def _logo_data_uri() -> str:
    try:
        logo = resources.files("sincron_brain").joinpath(LOGO_RESOURCE).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        return ""
    return "data:image/jpeg;base64," + b64encode(logo).decode("ascii")


def _read_markdown_body(path: Path) -> str:
    """Read only the markdown body; SQLite already has the indexed metadata."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text
    return parts[2].lstrip("\r\n")


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
      --canvas: #fbfaf7;
      --fog: #f4f2ee;
      --warm-mist: #fbe6d6;
      --ink: #0e0f12;
      --ink-soft: #1c1e22;
      --graphite: #3b3d42;
      --stone: #6e7079;
      --hint: #c5c6cb;
      --ember-300: #ff9450;
      --ember-500: #ed5e0a;
      --brand-blue: #0f4761;
      --danger: #b42318;
      --panel: rgba(255, 255, 255, 0.72);
      --line: rgba(197, 198, 203, 0.75);
      --soft: rgba(251, 230, 214, 0.62);
      --shadow: 0 22px 60px rgba(14, 15, 18, 0.12);
      --radius-card: 18px;
      --radius-ui: 8px;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html {{ background: #e8e6e0; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: #e8e6e0;
      color: var(--ink);
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }}
    .app {{
      width: min(1540px, calc(100vw - 48px));
      min-height: calc(100vh - 48px);
      margin: 24px auto;
      display: grid;
      grid-template-columns: 320px minmax(0, 1fr);
      position: relative;
      overflow: hidden;
      background:
        linear-gradient(135deg, rgba(251, 250, 247, 0.98) 0%, rgba(251, 250, 247, 0.9) 58%, rgba(244, 242, 238, 0.96) 100%);
      border: 1px solid rgba(255, 255, 255, 0.72);
      box-shadow: var(--shadow);
    }}
    .app::before {{
      content: "";
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 3px;
      background: linear-gradient(90deg, var(--brand-blue) 0%, var(--ember-500) 60%, transparent 100%);
      z-index: 3;
    }}
    aside, main {{ position: relative; z-index: 1; }}
    aside {{
      border-right: 1px solid var(--line);
      background: rgba(244, 242, 238, 0.78);
      padding: 28px;
      overflow: auto;
    }}
    main {{ padding: 28px; overflow: auto; }}
    h1 {{
      font-family: Georgia, "Times New Roman", serif;
      font-size: 34px;
      font-weight: 400;
      line-height: 1;
      margin: 0 0 6px;
      color: var(--brand-blue);
      letter-spacing: 0;
    }}
    h2 {{
      font-family: Georgia, "Times New Roman", serif;
      font-size: 26px;
      font-weight: 400;
      line-height: 1.15;
      margin: 0 0 14px;
      color: var(--brand-blue);
    }}
    h3 {{
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 13px;
      font-weight: 700;
      margin: 18px 0 8px;
      color: var(--ink-soft);
    }}
    p {{ margin: 0; }}
    .muted {{ color: var(--stone); font-size: 13px; line-height: 1.45; }}
    .brand {{
      display: flex;
      gap: 14px;
      align-items: center;
      margin-bottom: 18px;
      padding: 14px;
      background: #0e0f12;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: var(--radius-ui);
      box-shadow: 0 16px 34px rgba(14, 15, 18, 0.16);
    }}
    .brand-logo {{
      width: 62px;
      height: 62px;
      border-radius: var(--radius-ui);
      object-fit: cover;
      background: #000;
      flex: 0 0 auto;
      box-shadow: none;
    }}
    .brand h1 {{
      margin: 0 0 4px;
      color: #fbfaf7;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 32px;
      letter-spacing: -0.02em;
    }}
    .brand h1 em {{
      color: var(--ember-500);
      font-style: italic;
    }}
    .brand .muted {{ color: rgba(251, 250, 247, 0.68); }}
    .credit {{
      border-top: 1px solid var(--line);
      margin-top: 20px;
      padding-top: 16px;
      display: grid;
      gap: 4px;
      font-size: 12px;
      color: var(--stone);
    }}
    .credit strong {{
      color: var(--brand-blue);
      font-size: 13px;
      font-weight: 700;
    }}
    .credit a {{ color: var(--brand-blue); text-decoration: none; }}
    .credit a:hover {{ text-decoration: underline; }}
    .stack {{ display: grid; gap: 12px; }}
    .notice {{
      display: none;
      margin: 14px 0 0;
      padding: 10px 12px;
      border: 1px solid rgba(237, 94, 10, 0.28);
      border-radius: var(--radius-ui);
      background: rgba(251, 230, 214, 0.48);
      color: var(--graphite);
      font-size: 12px;
      line-height: 1.45;
    }}
    .judge-card {{
      margin: 14px 0 0;
      padding: 12px 13px;
      border-radius: var(--radius-ui);
      border: 1px solid var(--line);
      background: var(--panel);
      font-size: 12px;
      line-height: 1.45;
      color: var(--graphite);
    }}
    .judge-card.ready {{
      border-color: rgba(34, 139, 34, 0.45);
      background: rgba(220, 245, 220, 0.5);
    }}
    .judge-card.fallback {{
      border-color: rgba(237, 94, 10, 0.55);
      background: rgba(251, 230, 214, 0.65);
    }}
    .judge-card b {{ display: block; margin-bottom: 4px; }}
    .judge-card.ready b {{ color: #1f6f1f; }}
    .judge-card.fallback b {{ color: var(--ember-500); }}
    .judge-card code {{
      font-family: ui-monospace, "SF Mono", Consolas, monospace;
      font-size: 11px;
      background: rgba(14, 15, 18, 0.06);
      padding: 1px 4px;
      border-radius: 4px;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin: 20px 0;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius-card);
      padding: 12px;
    }}
    .stat b {{
      display: block;
      margin-top: 3px;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 21px;
      color: var(--ink-soft);
    }}
    label {{
      display: grid;
      gap: 7px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--stone);
    }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: var(--radius-ui);
      padding: 10px 11px;
      background: rgba(255, 255, 255, 0.78);
      color: var(--ink);
      font: inherit;
      letter-spacing: 0;
      text-transform: none;
      outline: none;
    }}
    input:focus, select:focus {{
      border-color: var(--ember-500);
      box-shadow: 0 0 0 3px rgba(237, 94, 10, 0.12);
    }}
    .tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 20px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--line);
    }}
    button {{
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.72);
      color: var(--ink-soft);
      border-radius: var(--radius-ui);
      padding: 10px 13px;
      cursor: pointer;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 12px;
      font-weight: 600;
    }}
    button:hover {{ border-color: rgba(237, 94, 10, 0.42); }}
    button.active {{
      border-color: rgba(237, 94, 10, 0.35);
      background: var(--warm-mist);
      color: var(--ember-500);
    }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(320px, 460px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }}
    .split {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }}
    .list {{ display: grid; gap: 10px; }}
    .row {{
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: var(--radius-card);
      padding: 14px;
      cursor: pointer;
      transition: border-color 160ms ease, box-shadow 160ms ease, transform 160ms ease;
    }}
    .row:hover, .row.selected {{
      border-color: rgba(237, 94, 10, 0.45);
      box-shadow: 0 12px 26px rgba(14, 15, 18, 0.08);
    }}
    .row:hover {{ transform: translateY(-1px); }}
    .row.selected {{ background: rgba(251, 230, 214, 0.34); }}
    .row-title {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-weight: 700;
      color: var(--ink-soft);
    }}
    .score {{
      color: var(--ember-500);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-weight: 700;
    }}
    .pillbar {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
    .pill {{
      border: 1px solid rgba(15, 71, 97, 0.14);
      border-radius: 999px;
      padding: 4px 9px;
      background: rgba(255, 255, 255, 0.62);
      font-size: 12px;
      color: var(--stone);
    }}
    .link-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      color: var(--brand-blue);
      cursor: pointer;
    }}
    .link-pill:hover {{
      border-color: rgba(237, 94, 10, 0.42);
      color: var(--ember-500);
    }}
    .relation-grid {{
      display: grid;
      grid-template-columns: minmax(360px, 1fr) minmax(420px, 0.9fr);
      gap: 18px;
      align-items: start;
    }}
    .relation-list {{ display: grid; gap: 12px; }}
    .relation-card {{
      border: 1px solid var(--line);
      border-radius: var(--radius-card);
      background: var(--panel);
      padding: 14px;
      box-shadow: 0 12px 28px rgba(14, 15, 18, 0.05);
    }}
    .relation-card.selected {{
      border-color: rgba(237, 94, 10, 0.45);
      background: rgba(251, 230, 214, 0.3);
    }}
    .relation-card-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
      margin-bottom: 10px;
    }}
    .relation-title {{
      border: 0;
      background: transparent;
      padding: 0;
      text-align: left;
      color: var(--ink-soft);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      line-height: 1.35;
      font-weight: 700;
    }}
    .relation-title:hover {{ color: var(--ember-500); }}
    .relation-count {{
      white-space: nowrap;
      border: 1px solid rgba(237, 94, 10, 0.24);
      border-radius: 999px;
      padding: 4px 8px;
      color: var(--ember-500);
      background: rgba(251, 230, 214, 0.42);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 11px;
      font-weight: 700;
    }}
    .relation-section {{
      display: grid;
      gap: 6px;
      padding-top: 9px;
      border-top: 1px solid rgba(197, 198, 203, 0.54);
      margin-top: 9px;
    }}
    .relation-section-label {{
      color: var(--stone);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .detail, .panel {{
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: var(--radius-card);
      padding: 18px;
      box-shadow: 0 12px 32px rgba(14, 15, 18, 0.05);
    }}
    .content {{
      white-space: pre-wrap;
      line-height: 1.62;
      font-size: 14px;
      color: var(--graphite);
    }}
    .meta {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin: 12px 0;
    }}
    .meta div {{
      border: 1px solid var(--line);
      border-radius: var(--radius-ui);
      padding: 10px;
      font-size: 12px;
      color: var(--graphite);
      background: rgba(255, 255, 255, 0.48);
    }}
    .meta b {{ color: var(--brand-blue); }}
    table {{
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      background: rgba(255, 255, 255, 0.56);
      border: 1px solid var(--line);
      border-radius: var(--radius-ui);
      overflow: hidden;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 11px;
      text-align: left;
      vertical-align: top;
      font-size: 13px;
      color: var(--graphite);
    }}
    th {{
      background: var(--fog);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 11px;
      font-weight: 700;
      color: var(--brand-blue);
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .graph {{ display: grid; gap: 14px; }}
    .graph-head {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: start;
    }}
    .graph-head p {{ max-width: 720px; }}
    .graph-legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }}
    .legend-item {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 9px;
      background: rgba(255, 255, 255, 0.62);
      color: var(--stone);
      font-size: 12px;
    }}
    .legend-dot {{
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--ember-500);
      display: inline-block;
    }}
    .legend-dot.mid {{ background: var(--brand-blue); }}
    .legend-dot.deep {{ background: var(--stone); }}
    .graph-stage {{
      position: relative;
      width: 100%;
      min-height: 680px;
      border: 1px solid var(--line);
      border-radius: var(--radius-card);
      background:
        linear-gradient(180deg, rgba(251, 230, 214, 0.56) 0%, rgba(255, 255, 255, 0.52) 36%, rgba(244, 242, 238, 0.78) 100%);
      overflow: hidden;
      box-shadow: 0 12px 32px rgba(14, 15, 18, 0.05);
    }}
    .graph-group {{
      position: absolute;
      top: 0;
      bottom: 0;
      border-left: 1px solid rgba(15, 71, 97, 0.12);
      border-right: 1px solid rgba(15, 71, 97, 0.08);
      background: rgba(255, 255, 255, 0.14);
      pointer-events: none;
    }}
    .graph-group b {{
      position: absolute;
      top: 12px;
      left: 10px;
      right: 10px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--brand-blue);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 10px;
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }}
    .graph-group span {{
      position: absolute;
      top: 30px;
      left: 10px;
      color: var(--stone);
      font-size: 11px;
    }}
    .surface-band {{
      position: absolute;
      left: 0;
      right: 0;
      border-top: 1px dashed rgba(110, 112, 121, 0.28);
      color: rgba(59, 61, 66, 0.72);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      padding: 5px 10px;
      pointer-events: none;
    }}
    .graph-svg {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
    }}
    .graph-edge {{
      stroke: rgba(15, 71, 97, 0.28);
      stroke-width: 1.4;
      fill: none;
    }}
    .graph-node {{
      position: absolute;
      transform: translate(-50%, -50%);
      padding: 0;
      border: 1px solid rgba(255, 255, 255, 0.68);
      border-radius: 999px;
      display: grid;
      place-items: center;
      color: #fff;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 10px;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 10px 24px rgba(14, 15, 18, 0.18);
      transition: transform 160ms ease, box-shadow 160ms ease, outline-color 160ms ease;
    }}
    .graph-node:hover, .graph-node.selected {{
      transform: translate(-50%, -50%) scale(1.08);
      box-shadow: 0 16px 34px rgba(14, 15, 18, 0.24);
      outline: 3px solid rgba(237, 94, 10, 0.2);
    }}
    .graph-label {{
      position: absolute;
      transform: translate(-50%, 12px);
      width: 132px;
      text-align: center;
      color: var(--graphite);
      font-size: 11px;
      line-height: 1.25;
      pointer-events: none;
      text-shadow: 0 1px 0 rgba(255, 255, 255, 0.72);
    }}
    .graph-empty {{
      min-height: 260px;
      display: grid;
      place-items: center;
      text-align: center;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: rgba(244, 242, 238, 0.78);
      border: 1px solid var(--line);
      border-radius: var(--radius-ui);
      padding: 10px;
    }}
    .hidden {{ display: none; }}
    @media (max-width: 900px) {{
      .app {{
        width: 100%;
        min-height: 100vh;
        margin: 0;
        border: 0;
      }}
      .app {{ grid-template-columns: 1fr; }}
      aside {{ border-right: 0; border-bottom: 1px solid var(--line); }}
      .grid, .split, .meta, .relation-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
<div class="app">
  <aside>
    <div class="brand">
      <img id="brandLogo" class="brand-logo" alt="Sincron IA">
      <div>
        <h1>Sincron <em>Brain</em></h1>
        <p class="muted">Visualizador de memórias</p>
      </div>
    </div>
    <p class="muted" id="vaultPath"></p>
    <p class="notice" id="viewerMode"></p>
    <div id="judgeStatus"></div>
    <div class="stats" id="stats"></div>
    <div class="stack">
      <label>Busca <input id="search" type="search" placeholder="id, conteúdo, sinopse, tag"></label>
      <label>Major tag <select id="tagFilter"></select></label>
      <label>Score mínimo <input id="scoreFilter" type="number" min="0" max="100" value="0"></label>
    </div>
    <div class="credit">
      <strong>Desenvolvido por Sincron IA</strong>
      <a href="https://sincronia.digital">sincronia.digital</a>
      <span>Autor Matheus Massari</span>
    </div>
  </aside>
  <main>
    <div class="tabs">
      <button data-tab="memories" class="active">Memórias</button>
      <button data-tab="go-deeper">Go deeper</button>
      <button data-tab="tags">Tags</button>
      <button data-tab="sleeps">Sleeps</button>
      <button data-tab="graph">Grafo</button>
      <button data-tab="queues">Filas</button>
      <button data-tab="audit">Audit</button>
    </div>
    <section id="tab-memories" class="tab"></section>
    <section id="tab-go-deeper" class="tab hidden"></section>
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
let activeTab = 'memories';
const fmt = value => value === null || value === undefined || value === '' ? '-' : String(value);
const esc = value => fmt(value).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
function shortId(id) {{ return id.length > 34 ? id.slice(0, 31) + '...' : id; }}
function init() {{
  const branding = DATA.branding || {{}};
  const logo = document.getElementById('brandLogo');
  if (branding.logo_data_uri) logo.src = branding.logo_data_uri;
  else logo.classList.add('hidden');
  document.getElementById('vaultPath').textContent = DATA.vault_path;
  const viewer = DATA.viewer || {{}};
  const viewerMode = document.getElementById('viewerMode');
  const modeBits = [];
  if (viewer.memory_limit) modeBits.push(`Snapshot limitado a ${{viewer.displayed_memories}} de ${{viewer.total_memories}} memórias.`);
  if (viewer.omitted_memories > 0) modeBits.push(`${{viewer.omitted_memories}} memórias omitidas do HTML.`);
  if (viewer.summary_only) modeBits.push('Corpos das memórias omitidos no modo resumo.');
  if (modeBits.length) {{
    viewerMode.textContent = modeBits.join(' ');
    viewerMode.style.display = 'block';
  }}
  const judge = DATA.judge_status || {{}};
  const judgeEl = document.getElementById('judgeStatus');
  if (judge && judge.provider) {{
    if (judge.ready) {{
      judgeEl.innerHTML = `
        <div class="judge-card ready">
          <b>Judge ativo</b>
          ${{esc(judge.provider)}}/${{esc(judge.model)}} · chave <code>${{esc(judge.api_key_env)}}</code> detectada.
        </div>`;
    }} else {{
      judgeEl.innerHTML = `
        <div class="judge-card fallback">
          <b>Judge em fallback</b>
          ${{esc(judge.provider)}}/${{esc(judge.model)}} · chave <code>${{esc(judge.api_key_env)}}</code> ausente.
          Sleep indexa drafts em <code>_uncategorized</code> sem reescrever sinopse nem sugerir go_deeper.
          Para ativar, exporte a chave no shell que inicia o MCP client antes de rodar <code>sleep_now()</code>.
        </div>`;
    }}
  }}
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
  ['search','tagFilter','scoreFilter'].forEach(id => document.getElementById(id).addEventListener('input', () => {{
    renderAll();
    showTab(activeTab);
  }}));
  document.querySelectorAll('[data-tab]').forEach(btn => btn.addEventListener('click', () => showTab(btn.dataset.tab)));
  renderAll();
}}
function showTab(tab) {{
  activeTab = tab;
  document.querySelectorAll('[data-tab]').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
  document.querySelectorAll('.tab').forEach(el => el.classList.add('hidden'));
  document.getElementById('tab-' + tab).classList.remove('hidden');
}}
function filteredMemories() {{
  const q = document.getElementById('search').value.trim().toLowerCase();
  const tag = document.getElementById('tagFilter').value;
  const minScore = Number(document.getElementById('scoreFilter').value || 0);
  return DATA.memories.filter(m => {{
    const text = [m.id, m.synopsis, m.content, m.source_type, ...(m.major_tags || []), ...(m.tags || [])].join(' ').toLowerCase();
    return (!q || text.includes(q)) && (!tag || (m.major_tags || []).includes(tag)) && Number(m.score || 0) >= minScore;
  }});
}}
function renderMemories() {{
  const memories = filteredMemories();
  if (!memories.find(m => m.id === selectedId)) selectedId = memories[0]?.id || null;
  const list = memories.map(m => `
    <div class="row ${{m.id === selectedId ? 'selected' : ''}}" data-memory-id="${{esc(m.id)}}">
      <div class="row-title"><span>${{esc(m.synopsis || m.id)}}</span><span class="score">${{m.score}}</span></div>
      <div class="muted">${{esc(shortId(m.id))}} · floor ${{m.emotion_floor}} · usos ${{m.access_count}}</div>
      <div class="pillbar">${{m.major_tags.map(t => `<span class="pill">${{esc(t)}}</span>`).join('')}}${{(m.tags || []).map(t => `<span class="pill">${{esc(t)}}</span>`).join('')}}</div>
    </div>`).join('') || '<div class="panel">Nenhuma memória encontrada.</div>';
  document.getElementById('tab-memories').innerHTML = `
    <div class="grid">
      <div class="list">${{list}}</div>
      ${{renderMemoryDetail(byId[selectedId])}}
    </div>`;
  document.querySelectorAll('[data-memory-id]').forEach(row => {{
    row.addEventListener('click', () => selectMemory(row.dataset.memoryId));
  }});
  bindMemoryLinks();
}}
function selectMemory(id) {{
  selectedId = id;
  renderAll();
  showTab(activeTab);
}}
function bindMemoryLinks() {{
  document.querySelectorAll('[data-open-memory]').forEach(el => {{
    if (el.dataset.boundMemoryLink) return;
    el.dataset.boundMemoryLink = 'true';
    el.addEventListener('click', event => {{
      event.stopPropagation();
      selectMemory(el.dataset.openMemory);
    }});
  }});
}}
function memoryChip(id) {{
  const memory = byId[id];
  if (!memory) return `<span class="pill">${{esc(shortId(id))}}</span>`;
  return `<button class="pill link-pill" data-open-memory="${{esc(id)}}" title="${{esc(memory.synopsis || id)}}">${{esc(memory.synopsis || shortId(id))}}</button>`;
}}
function renderMemoryDetail(m) {{
  if (!m) return '<div class="detail">Selecione uma memória.</div>';
  const go = (m.go_deeper || []).map(id => memoryChip(id)).join('') || '<span class="muted">Sem links</span>';
  const content = m.content_omitted ? '<span class="muted">Conteúdo omitido neste snapshot. Gere novamente com --include-content para incluir os corpos das memórias.</span>' : esc(m.content);
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
    <h3>Major tags</h3><div class="pillbar">${{m.major_tags.map(t => `<span class="pill">${{esc(t)}}</span>`).join('')}}</div>
    <h3>Tags</h3><div class="pillbar">${{(m.tags || []).map(t => `<span class="pill">${{esc(t)}}</span>`).join('') || '<span class="muted">Sem tags comuns</span>'}}</div>
    <h3>Go deeper</h3><div class="pillbar">${{go}}</div>
    <h3>Conteúdo</h3><div class="content">${{content}}</div>
  </div>`;
}}
function filteredEdges(limitIds = null) {{
  const ids = limitIds || new Set(filteredMemories().map(m => m.id));
  return DATA.go_deeper_edges.filter(e => ids.has(e.from) && ids.has(e.to));
}}
function renderGoDeeper() {{
  const memories = filteredMemories();
  const visibleIds = new Set(memories.map(m => m.id));
  const edges = filteredEdges(visibleIds);
  const outgoing = new Map();
  const incoming = new Map();
  edges.forEach(e => {{
    if (!outgoing.has(e.from)) outgoing.set(e.from, []);
    if (!incoming.has(e.to)) incoming.set(e.to, []);
    outgoing.get(e.from).push(e.to);
    incoming.get(e.to).push(e.from);
  }});
  const hubs = memories
    .map(m => ({{ memory: m, outgoing: outgoing.get(m.id) || [], incoming: incoming.get(m.id) || [] }}))
    .filter(item => item.outgoing.length || item.incoming.length)
    .sort((a, b) => (b.outgoing.length + b.incoming.length) - (a.outgoing.length + a.incoming.length) || b.memory.score - a.memory.score);
  const cards = hubs.map(item => {{
    const m = item.memory;
    const total = item.outgoing.length + item.incoming.length;
    const out = item.outgoing.map(memoryChip).join('') || '<span class="muted">Nenhuma saída filtrada.</span>';
    const inc = item.incoming.map(memoryChip).join('') || '<span class="muted">Nenhuma entrada filtrada.</span>';
    return `
      <div class="relation-card ${{m.id === selectedId ? 'selected' : ''}}">
        <div class="relation-card-head">
          <button class="relation-title" data-open-memory="${{esc(m.id)}}">${{esc(m.synopsis || m.id)}}</button>
          <span class="relation-count">${{total}} links</span>
        </div>
        <div class="muted">${{esc(shortId(m.id))}} · score ${{m.score}} · ${{(m.major_tags || []).map(esc).join(', ') || 'sem-major-tag'}}</div>
        <div class="relation-section">
          <span class="relation-section-label">Abre para</span>
          <div class="pillbar">${{out}}</div>
        </div>
        <div class="relation-section">
          <span class="relation-section-label">Recebe de</span>
          <div class="pillbar">${{inc}}</div>
        </div>
      </div>`;
  }}).join('') || '<div class="panel">Nenhuma relação go_deeper encontrada no escopo dos filtros.</div>';
  const selected = byId[selectedId] && visibleIds.has(selectedId) ? byId[selectedId] : null;
  document.getElementById('tab-go-deeper').innerHTML = `
    <div class="relation-grid">
      <div>
        <div class="panel graph-head">
          <div>
            <h2>Go deeper</h2>
            <p class="muted">Memórias agrupadas como hubs: cada card mostra todas as saídas e entradas visíveis dentro dos filtros atuais.</p>
          </div>
          <div class="graph-legend">
            <span class="legend-item">${{memories.length}} memórias</span>
            <span class="legend-item">${{edges.length}} links filtrados</span>
          </div>
        </div>
        <div class="relation-list">${{cards}}</div>
      </div>
      ${{renderMemoryDetail(selected)}}
    </div>`;
  bindMemoryLinks();
}}
function renderTags() {{
  document.getElementById('tab-tags').innerHTML = `
    <div class="split">
      <div class="panel"><h2>Major tags</h2>${{table(DATA.major_tags, ['major_tag','count','max_score','avg_score'])}}</div>
      <div class="panel"><h2>Tags comuns</h2>${{table(DATA.tags, ['tag','count','max_score','avg_score'])}}</div>
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
function renderGraphVisual() {{
  const allFiltered = filteredMemories();
  const memories = allFiltered.slice(0, 220);
  const visibleIds = new Set(memories.map(m => m.id));
  const edges = filteredEdges(visibleIds);
  if (!memories.length) {{
    document.getElementById('tab-graph').innerHTML = '<div class="panel graph-empty"><p class="muted">Nenhuma memória encontrada para desenhar o grafo.</p></div>';
    return;
  }}
  const width = 1060;
  const height = 680;
  const padX = 54;
  const padY = 88;
  const groups = [...new Set(memories.map(m => (m.major_tags && m.major_tags[0]) || 'sem-major-tag'))];
  const groupWidth = (width - padX * 2) / Math.max(groups.length, 1);
  const positions = new Map();
  const byGroup = new Map(groups.map(group => [group, []]));
  memories.forEach(m => byGroup.get((m.major_tags && m.major_tags[0]) || 'sem-major-tag').push(m));
  groups.forEach((group, groupIndex) => {{
    const groupMemories = byGroup.get(group).sort((a, b) => Number(b.score || 0) - Number(a.score || 0));
    const left = padX + groupWidth * groupIndex;
    const center = left + groupWidth / 2;
    groupMemories.forEach((m, index) => {{
      const score = Math.max(0, Math.min(100, Number(m.score || 0)));
      const columns = groupWidth > 140 ? 3 : groupWidth > 88 ? 2 : 1;
      const column = index % columns;
      const row = Math.floor(index / columns);
      const rows = Math.max(1, Math.ceil(groupMemories.length / columns));
      const spread = Math.min(groupWidth * 0.28, 42);
      const x = center + (column - (columns - 1) / 2) * spread;
      const y = padY + ((row + 0.6) / Math.max(rows, 1)) * (height - padY - 74);
      positions.set(m.id, {{ x: Math.max(34, Math.min(width - 34, x)), y, score }});
    }});
  }});
  const groupGuides = groups.map((group, index) => {{
    const left = ((padX + groupWidth * index) / width) * 100;
    const w = (groupWidth / width) * 100;
    const count = byGroup.get(group).length;
    return `<div class="graph-group" style="left:${{left}}%; width:${{w}}%;"><b>${{esc(group)}}</b><span>${{count}} memórias</span></div>`;
  }}).join('');
  const lines = edges.map(e => {{
    const a = positions.get(e.from);
    const b = positions.get(e.to);
    if (!a || !b) return '';
    return `<path class="graph-edge" marker-end="url(#arrow)" d="M ${{a.x}} ${{a.y}} C ${{a.x}} ${{(a.y + b.y) / 2}}, ${{b.x}} ${{(a.y + b.y) / 2}}, ${{b.x}} ${{b.y}}" />`;
  }}).join('');
  const nodes = memories.map(m => {{
    const pos = positions.get(m.id);
    const score = pos.score;
    const size = 18 + Math.round(score / 100 * 24);
    const color = score >= 75 ? 'var(--ember-500)' : score >= 45 ? 'var(--brand-blue)' : 'var(--stone)';
    const label = esc(m.synopsis || shortId(m.id));
    return `
      <button class="graph-node ${{m.id === selectedId ? 'selected' : ''}}" data-graph-id="${{esc(m.id)}}"
        style="left:${{(pos.x / width) * 100}}%; top:${{(pos.y / height) * 100}}%; width:${{size}}px; height:${{size}}px; background:${{color}};"
        title="${{label}} | score ${{score}} | ${{esc(shortId(m.id))}}">
        ${{Math.round(score)}}
      </button>
      <div class="graph-label" style="left:${{(pos.x / width) * 100}}%; top:${{(pos.y / height) * 100}}%;">${{label}}</div>`;
  }}).join('');
  const bands = [
    ['Mais relevantes', 0],
    ['Meio do grupo', 50],
    ['Menos relevantes', 100],
  ].map(([label, pct]) => {{
    const y = padY + (pct / 100) * (height - padY - 74);
    return `<div class="surface-band" style="top:${{(y / height) * 100}}%">${{label}}</div>`;
  }}).join('');
  const hidden = allFiltered.length > memories.length ? `<p class="muted">Mostrando as primeiras ${{memories.length}} memórias filtradas para preservar a fluidez do HTML.</p>` : '';
  document.getElementById('tab-graph').innerHTML = `
    <div class="graph">
      <div class="panel graph-head">
        <div>
          <h2>Grafo de memórias</h2>
          <p class="muted">Cada coluna é uma Major Tag. Dentro da coluna, memórias mais relevantes aparecem primeiro; linhas indicam conexões <code>go_deeper</code> dentro do escopo filtrado.</p>
          ${{hidden}}
        </div>
        <div class="graph-legend">
          <span class="legend-item"><span class="legend-dot"></span>score 75-100</span>
          <span class="legend-item"><span class="legend-dot mid"></span>score 45-74</span>
          <span class="legend-item"><span class="legend-dot deep"></span>score 0-44</span>
          <span class="legend-item">${{edges.length}} links</span>
        </div>
      </div>
      <div class="graph-stage">
        ${{groupGuides}}
        ${{bands}}
        <svg class="graph-svg" viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3.5" orient="auto">
              <path d="M 0 0 L 8 3.5 L 0 7 z" fill="rgba(15, 71, 97, 0.34)"></path>
            </marker>
          </defs>
          ${{lines}}
        </svg>
        ${{nodes}}
      </div>
    </div>`;
  document.querySelectorAll('[data-graph-id]').forEach(node => {{
    node.addEventListener('click', () => {{
      selectMemory(node.dataset.graphId);
    }});
  }});
}}
function renderQueues() {{
  document.getElementById('tab-queues').innerHTML = `
    <div class="split">
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
  renderGoDeeper();
  renderTags();
  renderSleeps();
  renderGraphVisual();
  renderQueues();
  renderAudit();
}}
init();
</script>
</body>
</html>
"""
