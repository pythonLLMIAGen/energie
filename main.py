#!/usr/bin/env python3
"""
Compare Claude vs Gemma — CloudWatch Insights JSON format
Format: [{"@timestamp": "...", "@message": "{\"level\":...}"}, ...]
"""
import json
import re
import os
import sys
import argparse
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

# ─────────────────────────────────────────────
#  PARSE
# ─────────────────────────────────────────────

def parse_inner_message(msg: str) -> Dict:
    """Parse the inner @message field (escaped JSON string or plain text)"""
    data = {}

    # Try to parse as JSON first (it's often escaped JSON)
    inner = None
    try:
        inner = json.loads(msg)
    except Exception:
        # Not valid JSON — treat as plain text
        inner = msg

    # If inner is a dict, extract fields directly
    if isinstance(inner, dict):
        # model name
        for key in ('model', 'model_name', 'ModelId'):
            if key in inner:
                data['model'] = str(inner[key])
                break

        # elapsed
        for key in ('elapsed', 'elapsed_seconds', 'elapsed_time'):
            if key in inner:
                try:
                    data['elapsed'] = float(str(inner[key]).replace('s', ''))
                except:
                    pass
                break

        # tokens
        if 'output_tokens' in inner:
            try: data['output_tokens'] = int(inner['output_tokens'])
            except: pass
        if 'input_tokens' in inner:
            try: data['input_tokens'] = int(inner['input_tokens'])
            except: pass

        # message text (nested)
        msg_text = inner.get('message', '')
        if msg_text:
            data.update(_regex_extract(msg_text))

        # location/node
        if 'location' in inner:
            data['location'] = inner['location']

        # agent
        if 'agent_called' in inner:
            data['agent'] = inner['agent_called']

        # model_response field (nested JSON again sometimes)
        if 'model_response' in inner:
            mr = inner['model_response']
            if isinstance(mr, str):
                try:
                    mr = json.loads(mr)
                except: pass
            if isinstance(mr, dict):
                if 'content' in mr:
                    data['answer'] = str(mr['content'])[:500]

    # Always run regex on the raw string too (catches edge cases)
    data.update({k: v for k, v in _regex_extract(msg).items() if k not in data})

    return data


def _regex_extract(text: str) -> Dict:
    """Regex fallback on raw text"""
    data = {}

    # model
    for pattern in [
        r'model[=:]\s*["\']?([a-zA-Z0-9._-]{4,})["\']?',
        r'"ModelId"\s*:\s*"([^"]+)"',
        r'model_name["\s:=]+([a-zA-Z0-9._-]{4,})',
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip('",\\ ')
            if val not in ('True', 'False', 'None', 'Event_0'):
                data['model'] = val
                break

    # elapsed
    for pattern in [r'elapsed[=_\s:]+(\d+\.?\d*)s', r'elapsed_seconds["\s:=]+(\d+\.?\d*)']:
        m = re.search(pattern, text)
        if m:
            data['elapsed'] = float(m.group(1))
            break

    # output tokens
    m = re.search(r'output_tokens[=:\s"]+(\d+)', text)
    if m: data['output_tokens'] = int(m.group(1))

    # input tokens
    m = re.search(r'input_tokens[=:\s"]+(\d+)', text)
    if m: data['input_tokens'] = int(m.group(1))

    # orchestration total
    m = re.search(r"Temps d'ex[eé]cution call orchestration:\s*(\d+\.?\d*)\s*secondes", text)
    if m: data['orchestration_total'] = float(m.group(1))

    # llm_calls
    m = re.search(r'llm_calls[=:\s"]+(\d+)', text)
    if m: data['llm_calls'] = int(m.group(1))

    # content / answer snippets
    m = re.search(r"content='([^']{20,})'", text)
    if m: data['answer'] = m.group(1)[:500]

    return data


def detect_model(data: Dict, raw: str) -> Optional[str]:
    model = data.get('model', '').lower()
    raw_lower = raw.lower()

    if 'claude' in model or 'anthropic' in model:
        return 'claude'
    if 'gemma' in model:
        return 'gemma'

    # Fallback: search raw string
    if 'claude' in raw_lower or 'anthropic.claude' in raw_lower:
        return 'claude'
    if 'gemma' in raw_lower:
        return 'gemma'

    return None


# ─────────────────────────────────────────────
#  LOAD & ANALYZE
# ─────────────────────────────────────────────

def load_cloudwatch_json(path: str) -> List[Dict]:
    with open(path, 'r', encoding='utf-8') as f:
        raw = f.read().strip()

    # CloudWatch Insights exports as array
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and 'logEvents' in data:
            return data['logEvents']
        return [data]
    except json.JSONDecodeError:
        # JSONL fallback
        entries = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except:
                    pass
        return entries


def analyze(path: str) -> Dict:
    entries = load_cloudwatch_json(path)
    print(f"  → {len(entries)} log entries loaded")

    requests = {'claude': [], 'gemma': []}
    current = {k: {'elapsed': [], 'out_tokens': [], 'in_tokens': [],
                   'orch_total': None, 'llm_calls': None,
                   'answer': None, 'question': None, 'timestamp': None}
               for k in ('claude', 'gemma')}

    def flush(model_type):
        c = current[model_type]
        if c['elapsed'] or c['orch_total']:
            requests[model_type].append(dict(c))
        current[model_type] = {'elapsed': [], 'out_tokens': [], 'in_tokens': [],
                               'orch_total': None, 'llm_calls': None,
                               'answer': None, 'question': None, 'timestamp': None}

    for entry in entries:
        ts = entry.get('@timestamp', '')
        msg_raw = entry.get('@message', '')
        if not msg_raw:
            continue

        parsed = parse_inner_message(msg_raw)
        model_type = detect_model(parsed, msg_raw)

        if not model_type:
            continue

        c = current[model_type]

        if not c['timestamp'] and ts:
            c['timestamp'] = ts

        if 'elapsed' in parsed:
            # New call = flush previous if we already have data
            if c['elapsed']:
                flush(model_type)
            c['elapsed'].append(parsed['elapsed'])

        if 'output_tokens' in parsed:
            c['out_tokens'].append(parsed['output_tokens'])
        if 'input_tokens' in parsed:
            c['in_tokens'].append(parsed['input_tokens'])
        if 'orchestration_total' in parsed:
            c['orch_total'] = parsed['orchestration_total']
        if 'llm_calls' in parsed:
            c['llm_calls'] = parsed['llm_calls']
        if 'answer' in parsed and not c['answer']:
            c['answer'] = parsed['answer']

    # Flush remaining
    for m in ('claude', 'gemma'):
        flush(m)

    print(f"  → Claude requests: {len(requests['claude'])}")
    print(f"  → Gemma  requests: {len(requests['gemma'])}")
    return requests


# ─────────────────────────────────────────────
#  STATS
# ─────────────────────────────────────────────

def stats(values):
    if not values:
        return {'min': 0, 'max': 0, 'avg': 0, 'total': 0, 'count': 0}
    return {
        'min': round(min(values), 3),
        'max': round(max(values), 3),
        'avg': round(sum(values) / len(values), 3),
        'total': round(sum(values), 3),
        'count': len(values),
    }


def summarize(reqs: List[Dict]) -> Dict:
    times, out_tok, in_tok, tps = [], [], [], []
    for r in reqs:
        t = r.get('orch_total') or (sum(r['elapsed']) if r['elapsed'] else None)
        if t: times.append(t)
        out_tok.extend(r['out_tokens'])
        in_tok.extend(r['in_tokens'])
        tok = sum(r['out_tokens'])
        if t and tok: tps.append(tok / t)

    return {
        'count': len(reqs),
        'response_time': stats(times),
        'output_tokens': stats(out_tok),
        'input_tokens': stats(in_tok),
        'tokens_per_sec': stats(tps),
        'requests': reqs,
    }


# ─────────────────────────────────────────────
#  HTML REPORT
# ─────────────────────────────────────────────

def html_report(sc, sg, out_path):
    def winner(v1, v2, lower=True):
        if v1 == v2: return 'tie', 'tie'
        if lower: return ('win','lose') if v1 < v2 else ('lose','win')
        return ('win','lose') if v1 > v2 else ('lose','win')

    def row(label, v1, v2, unit='', lower=True, fmt='.2f'):
        w1, w2 = winner(v1, v2, lower)
        return f"<tr><td>{label}</td><td class='{w1}'>{v1:{fmt}}{unit}</td><td class='{w2}'>{v2:{fmt}}{unit}</td></tr>"

    rt_rows = (
        row('Avg Response Time', sc['response_time']['avg'], sg['response_time']['avg'], 's') +
        row('Min Response Time', sc['response_time']['min'], sg['response_time']['min'], 's') +
        row('Max Response Time', sc['response_time']['max'], sg['response_time']['max'], 's') +
        row('Avg Output Tokens', sc['output_tokens']['avg'], sg['output_tokens']['avg'], '', lower=False, fmt='.0f') +
        row('Tokens / Second',   sc['tokens_per_sec']['avg'], sg['tokens_per_sec']['avg'], '', lower=False) +
        row('Total Requests',    sc['count'], sg['count'], '', lower=False, fmt='.0f')
    )

    def time_series(reqs):
        return json.dumps([
            round(r.get('orch_total') or (sum(r['elapsed']) if r['elapsed'] else 0), 2)
            for r in reqs
        ])

    def token_series(reqs):
        return json.dumps([sum(r['out_tokens']) for r in reqs])

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    nc, ng = sc['count'], sg['count']
    labels = json.dumps([f"Q{i+1}" for i in range(max(nc, ng))])

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude vs Gemma — CloudWatch Comparison</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{{--bg:#0d0f14;--s:#161b27;--s2:#1e2435;--bd:#2a3147;--t:#e2e8f0;--m:#64748b;
  --a:#60a5fa;--b:#a78bfa;--win:#34d399;--lose:#f87171;--tie:#94a3b8;--r:12px}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--t);font-family:system-ui,sans-serif;font-size:14px}}
header{{background:linear-gradient(135deg,#0d1529,#161b27);border-bottom:1px solid var(--bd);
  padding:2rem;text-align:center}}
h1{{font-size:1.6rem;font-weight:700;margin:.5rem 0}}
.pills{{display:flex;gap:1rem;justify-content:center;margin:.8rem 0}}
.pa{{padding:4px 16px;border-radius:20px;background:rgba(96,165,250,.15);color:var(--a);
  border:1px solid rgba(96,165,250,.3);font-weight:600;font-size:.82rem}}
.pb{{padding:4px 16px;border-radius:20px;background:rgba(167,139,250,.15);color:var(--b);
  border:1px solid rgba(167,139,250,.3);font-weight:600;font-size:.82rem}}
.meta{{color:var(--m);font-size:.75rem}}
.wrap{{max-width:1050px;margin:0 auto;padding:2rem}}
section{{margin-bottom:2.5rem}}
h2{{font-size:.78rem;font-weight:600;text-transform:uppercase;letter-spacing:1px;
  color:var(--m);border-bottom:1px solid var(--bd);padding-bottom:.5rem;margin-bottom:1.2rem}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:1rem;margin-bottom:2rem}}
.kpi{{background:var(--s);border:1px solid var(--bd);border-radius:var(--r);padding:1.2rem;text-align:center}}
.kpi-l{{font-size:.7rem;color:var(--m);text-transform:uppercase;letter-spacing:.8px;margin-bottom:.3rem}}
.kpi-v{{font-size:1.5rem;font-weight:700}}
.kpi-s{{font-size:.72rem;color:var(--m);margin-top:.2rem}}
.ca{{color:var(--a)}} .cb{{color:var(--b)}}
.tw{{background:var(--s);border:1px solid var(--bd);border-radius:var(--r);overflow:hidden}}
table{{width:100%;border-collapse:collapse}}
thead th{{background:var(--s2);padding:.7rem 1rem;font-size:.75rem;font-weight:600;
  text-transform:uppercase;letter-spacing:.6px;text-align:left;border-bottom:1px solid var(--bd)}}
tbody tr{{border-bottom:1px solid var(--bd)}}
tbody tr:last-child{{border-bottom:none}}
tbody tr:hover{{background:var(--s2)}}
td{{padding:.65rem 1rem;font-size:.88rem}}
.win{{color:var(--win);font-weight:600}} .lose{{color:var(--lose)}} .tie{{color:var(--tie)}}
.charts{{display:grid;grid-template-columns:1fr 1fr;gap:1.2rem}}
@media(max-width:650px){{.charts{{grid-template-columns:1fr}}}}
.cc{{background:var(--s);border:1px solid var(--bd);border-radius:var(--r);padding:1.2rem}}
.ct{{font-size:.75rem;font-weight:600;color:var(--m);text-transform:uppercase;
  letter-spacing:.6px;margin-bottom:.8rem}}
footer{{text-align:center;color:var(--m);font-size:.72rem;padding:1.5rem;
  border-top:1px solid var(--bd)}}
</style>
</head>
<body>
<header>
  <div class="meta">CloudWatch Insights — Model Comparison</div>
  <h1>Claude vs Gemma Performance</h1>
  <div class="pills"><span class="pa">Claude</span><span style="color:var(--m)">vs</span><span class="pb">Gemma</span></div>
  <div class="meta">Generated {now} · {nc} Claude requests · {ng} Gemma requests</div>
</header>
<div class="wrap">

<section>
  <h2>Key Metrics</h2>
  <div class="kpis">
    <div class="kpi">
      <div class="kpi-l">Avg Response Time</div>
      <div class="kpi-v"><span class="ca">{sc['response_time']['avg']:.2f}s</span></div>
      <div class="kpi-s"><span class="cb">{sg['response_time']['avg']:.2f}s</span> — Gemma</div>
    </div>
    <div class="kpi">
      <div class="kpi-l">Avg Output Tokens</div>
      <div class="kpi-v"><span class="ca">{sc['output_tokens']['avg']:.0f}</span></div>
      <div class="kpi-s"><span class="cb">{sg['output_tokens']['avg']:.0f}</span> — Gemma</div>
    </div>
    <div class="kpi">
      <div class="kpi-l">Tokens / Second</div>
      <div class="kpi-v"><span class="ca">{sc['tokens_per_sec']['avg']:.1f}</span></div>
      <div class="kpi-s"><span class="cb">{sg['tokens_per_sec']['avg']:.1f}</span> — Gemma</div>
    </div>
    <div class="kpi">
      <div class="kpi-l">Total Requests</div>
      <div class="kpi-v"><span class="ca">{nc}</span> / <span class="cb">{ng}</span></div>
      <div class="kpi-s">Claude / Gemma</div>
    </div>
  </div>
</section>

<section>
  <h2>Detailed Metrics</h2>
  <div class="tw"><table>
    <thead><tr><th>Metric</th><th class="ca">Claude</th><th class="cb">Gemma</th></tr></thead>
    <tbody>{rt_rows}</tbody>
  </table></div>
  <p style="color:var(--m);font-size:.72rem;margin-top:.5rem">
    <span style="color:var(--win)">■</span> Better &nbsp;
    <span style="color:var(--lose)">■</span> Worse &nbsp;
    <span style="color:var(--tie)">■</span> Tie
  </p>
</section>

<section>
  <h2>Per-Request Charts</h2>
  <div class="charts">
    <div class="cc"><div class="ct">Response Time per Request (s)</div><canvas id="tc" height="200"></canvas></div>
    <div class="cc"><div class="ct">Output Tokens per Request</div><canvas id="tkc" height="200"></canvas></div>
  </div>
</section>

</div>
<footer>Claude vs Gemma · {now}</footer>
<script>
const OPT = {{responsive:true,
  plugins:{{legend:{{labels:{{color:'#94a3b8',font:{{size:11}}}}}}}},
  scales:{{x:{{ticks:{{color:'#64748b'}},grid:{{color:'#2a3147'}}}},
           y:{{ticks:{{color:'#64748b'}},grid:{{color:'#2a3147'}}}}}}}};
const labels={labels};
new Chart(document.getElementById('tc'),{{type:'bar',data:{{labels,datasets:[
  {{label:'Claude',data:{time_series(sc['requests'])},backgroundColor:'rgba(96,165,250,.7)',borderRadius:4}},
  {{label:'Gemma', data:{time_series(sg['requests'])},backgroundColor:'rgba(167,139,250,.7)',borderRadius:4}}
]}},options:OPT}});
new Chart(document.getElementById('tkc'),{{type:'bar',data:{{labels,datasets:[
  {{label:'Claude',data:{token_series(sc['requests'])},backgroundColor:'rgba(96,165,250,.7)',borderRadius:4}},
  {{label:'Gemma', data:{token_series(sg['requests'])},backgroundColor:'rgba(167,139,250,.7)',borderRadius:4}}
]}},options:OPT}});
</script>
</body></html>"""

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✅  Rapport HTML → {out_path}")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description='Claude vs Gemma — CloudWatch JSON logs')
    p.add_argument('file', help='Fichier JSON exporté depuis CloudWatch Insights')
    p.add_argument('--output', default='comparison_report.html')
    args = p.parse_args()

    print(f"\n📂 Chargement de {args.file} …")
    reqs = analyze(args.file)

    sc = summarize(reqs['claude'])
    sg = summarize(reqs['gemma'])

    print(f"\n{'─'*52}")
    print(f"  {'Métrique':<25} {'Claude':>10} {'Gemma':>10}")
    print(f"{'─'*52}")
    print(f"  {'Requêtes':<25} {sc['count']:>10} {sg['count']:>10}")
    print(f"  {'Temps moyen (s)':<25} {sc['response_time']['avg']:>10.2f} {sg['response_time']['avg']:>10.2f}")
    print(f"  {'Tokens output moy':<25} {sc['output_tokens']['avg']:>10.0f} {sg['output_tokens']['avg']:>10.0f}")
    print(f"  {'Tokens/sec':<25} {sc['tokens_per_sec']['avg']:>10.1f} {sg['tokens_per_sec']['avg']:>10.1f}")
    print(f"{'─'*52}\n")

    html_report(sc, sg, args.output)
    print(f"🌐 Ouvrir dans le navigateur : file://{os.path.abspath(args.output)}\n")

if __name__ == '__main__':
    main()
