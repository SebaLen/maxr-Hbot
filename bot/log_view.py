#!/usr/bin/env python3
"""MAXR bot log viewer: turns a bot_*.log into an HTML view in which
ANGENOMMEN is highlighted green and ABGELEHNT red. With a filter bar
(all / actions only / rejected only / full-text search) and line numbers.

Usage:  python log_view.py <logfile> [output.html]
Default output: <logfile>.html
"""
import sys
import html
import os


def build_html(log_path, out_path):
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        raw_lines = f.read().splitlines()

    rows = []
    n_acc = n_rej = 0
    for i, line in enumerate(raw_lines, 1):
        line = line.rstrip("\r")
        esc = html.escape(line)
        cls = "plain"
        is_action = "AKTION:" in line
        if "ANGENOMMEN" in line:
            esc = esc.replace("ANGENOMMEN",
                              '<span class="acc">ANGENOMMEN</span>')
            cls = "row-acc"
            n_acc += 1
        elif "ABGELEHNT" in line:
            esc = esc.replace("ABGELEHNT",
                              '<span class="rej">ABGELEHNT</span>')
            # subtly highlight the rejection reason
            cls = "row-rej"
            n_rej += 1
        data = []
        if is_action:
            data.append("action")
        if "ANGENOMMEN" in line:
            data.append("acc")
        if "ABGELEHNT" in line:
            data.append("rej")
        rows.append(
            f'<div class="row {cls}" data-kind="{" ".join(data)}">'
            f'<span class="ln">{i}</span>'
            f'<span class="txt">{esc}</span></div>'
        )

    body = "\n".join(rows)
    title = html.escape(os.path.basename(log_path))
    doc = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<title>{title} - Bot-Log</title>
<style>
  :root {{
    --bg:#1115; --acc:#1a7f37; --rej:#cf222e; --line:#8b949e;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
         background:#0d1117; color:#c9d1d9; }}
  header {{ position:sticky; top:0; background:#161b22; border-bottom:1px solid #30363d;
           padding:10px 14px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; z-index:5; }}
  header h1 {{ font-size:14px; margin:0 12px 0 0; font-weight:600; color:#e6edf3; }}
  .stat {{ font-size:12px; color:#8b949e; }}
  .stat b.acc {{ color:#3fb950; }}
  .stat b.rej {{ color:#f85149; }}
  button {{ background:#21262d; color:#c9d1d9; border:1px solid #30363d; border-radius:6px;
           padding:5px 10px; cursor:pointer; font:inherit; font-size:12px; }}
  button.active {{ background:#1f6feb; border-color:#1f6feb; color:#fff; }}
  input[type=search] {{ background:#0d1117; color:#c9d1d9; border:1px solid #30363d;
           border-radius:6px; padding:5px 8px; font:inherit; font-size:12px; min-width:220px; }}
  main {{ padding:6px 0 40px; }}
  .row {{ display:flex; gap:0; padding:0 14px; white-space:pre-wrap; word-break:break-word; }}
  .row:hover {{ background:#161b22; }}
  .ln {{ color:#484f58; min-width:54px; text-align:right; padding-right:14px;
        user-select:none; flex:none; }}
  .txt {{ flex:1; }}
  .acc {{ color:#3fb950; font-weight:700; }}
  .rej {{ color:#f85149; font-weight:700; }}
  .row-rej {{ background:rgba(248,81,73,.08); }}
  .row-rej:hover {{ background:rgba(248,81,73,.16); }}
  .row-acc .txt {{ color:#aff5b4; }}
  .hidden {{ display:none; }}
</style></head>
<body>
<header>
  <h1>{title}</h1>
  <span class="stat">Zeilen: {len(raw_lines)} &nbsp;|&nbsp;
    <b class="acc">ANGENOMMEN {n_acc}</b> &nbsp;|&nbsp;
    <b class="rej">ABGELEHNT {n_rej}</b></span>
  <span style="flex:1"></span>
  <button data-f="all" class="active">Alle</button>
  <button data-f="action">Nur Aktionen</button>
  <button data-f="rej">Nur Abgelehnte</button>
  <button data-f="acc">Nur Angenommene</button>
  <input type="search" id="q" placeholder="Volltext filtern (z. B. 54, 45)">
</header>
<main id="log">
{body}
</main>
<script>
  const rows=[...document.querySelectorAll('.row')];
  let filter='all', q='';
  function apply() {{
    const ql=q.toLowerCase();
    for(const r of rows) {{
      const k=r.dataset.kind||'';
      let ok = filter==='all' || k.split(' ').includes(filter);
      if(ok && ql) ok = r.textContent.toLowerCase().includes(ql);
      r.classList.toggle('hidden', !ok);
    }}
  }}
  document.querySelectorAll('header button').forEach(b=>b.onclick=()=>{{
    document.querySelectorAll('header button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active'); filter=b.dataset.f; apply();
  }});
  document.getElementById('q').addEventListener('input',e=>{{q=e.target.value; apply();}});
</script>
</body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return n_acc, n_rej, len(raw_lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Aufruf: python log_view.py <logdatei> [ausgabe.html]")
        sys.exit(1)
    log_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else log_path + ".html"
    acc, rej, total = build_html(log_path, out_path)
    print(f"OK: {total} Zeilen, {acc} angenommen, {rej} abgelehnt -> {out_path}")
