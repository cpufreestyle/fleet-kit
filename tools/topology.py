#!/usr/bin/env python3
"""Render and police the fleet topology from one data file.

Why this exists
---------------
The topology used to live as a hand-drawn diagram: an ASCII block in a handoff
doc and a canvas file nobody regenerated. Both drifted the moment a bridge
moved, and an AI asked to "update the diagram" had to reverse-engineer the
picture before it could touch it.

Now the picture is generated. fleet-topology.json holds the whole thing --
layers, nodes, edges, bridges -- and this script turns it into HTML (for a
human) and Mermaid (for a doc or an AI that only reads text). Maintenance is
therefore: edit the JSON, run `python3 topology.py all`. No HTML, no SVG, no
canvas file is ever edited by hand.

Three subcommands, in the order an AI should run them:

  check    data integrity + drift against the live machine. Prints a
           machine-readable report with --json, so a caller can act on it
           instead of reading prose. It catches the two mistakes that actually
           happen: an edge pointing at a node that was renamed away, and a
           port declared here that nothing listens on any more.
  refresh  re-measure the live half -- is each port listening, how many models
           does each entry advertise, which bridges does fleet_probe currently
           call reachable, and which are agentic -- and write it into the
           JSON's "live" block. Curated annotations (state, tags, free tiers)
           are NOT touched: they are measured by fleet_probe.py and hand-set.
  render   write fleet-topology.html and fleet-topology.md from the JSON.

Usage:
  topology.py                     check + refresh + render (the usual run)
  topology.py check [--json]
  topology.py refresh
  topology.py render [--format html|md|both]
  topology.py all
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.abspath(os.path.join(HERE, os.pardir, "docs"))
DATA = os.path.join(DOCS, "fleet-topology.json")
HTML_OUT = os.path.join(DOCS, "fleet-topology.html")
MD_OUT = os.path.join(DOCS, "fleet-topology.md")
REACH_FILES = (
    os.path.expanduser("~/.codex/fleet-reach.json"),
    os.path.abspath(os.path.join(HERE, os.pardir, "runtime", "tools",
                                 "fleet-reach.json")),
)

KIND_COLOR = {"main": "#58a6ff", "fall": "#d29922", "free": "#3fb950",
              "fix": "#bc8cff", "bad": "#f85149"}


# ---------------------------------------------------------------- data plumbing

def load(path=DATA):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(data, path=DATA):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


# ------------------------------------------------------------------ live probing

def listening(port, host="127.0.0.1", timeout=0.6):
    """True when something accepts a connection on this port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def model_count(port, timeout=6.0):
    """How many models this port advertises, or None when it will not say."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/models" % port, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    items = data.get("data") if isinstance(data, dict) else data
    return len(items or [])


def prefix_breakdown(port=10100, timeout=5.0):
    """Model counts per provider prefix, e.g. {"workbuddy": 16, ...}."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/models" % port, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return {}
    counts = {}
    for row in (data.get("data") or []):
        slug = str(row.get("id") or "")
        key = slug.split("/")[0] if "/" in slug else "(bare)"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def load_reach():
    for path in REACH_FILES:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except OSError:
            continue
    return {}


def refresh(data, quiet=False):
    """Re-measure the live half and store it under data["live"]."""
    live = {"measured_at": time.strftime("%Y-%m-%d %H:%M:%S"), "ports": {},
            "bridges": {}}

    for node in data.get("nodes", []):
        probe = node.get("probe") or {}
        port = probe.get("port")
        if not port:
            continue
        entry = {"listening": listening(port)}
        if probe.get("models"):
            entry["models"] = model_count(port)
        live["ports"][str(port)] = entry

    for br in data.get("bridges", []):
        port = br.get("port")
        if not port:
            continue
        live["bridges"][str(port)] = {"listening": listening(port)}

    ocx_port = None
    for node in data.get("nodes", []):
        if node.get("id") == "ocx":
            ocx_port = (node.get("probe") or {}).get("port")
    if ocx_port:
        counts = prefix_breakdown(ocx_port)
        if counts:
            live["ocx_models"] = sum(counts.values())
            live["ocx_prefixes"] = counts

    reach = load_reach() or {}
    if reach:
        live["fleet_reach"] = {
            "measured_at": reach.get("measured_at"),
            "reachable": reach.get("reachable"),
            "unreachable": reach.get("unreachable"),
            "agentic_text_only": reach.get("agentic_text_only"),
        }
        live["agentic"] = reach.get("agentic") or {}

    data["live"] = live
    if not quiet:
        up = sum(1 for v in live["ports"].values() if v.get("listening"))
        bup = sum(1 for v in live["bridges"].values() if v.get("listening"))
        print("refreshed: %d/%d entry ports, %d/%d bridge ports listening, "
              "ocx %s models"
              % (up, len(live["ports"]), bup, len(live["bridges"]),
                 live.get("ocx_models", "?")))
    return data


# ------------------------------------------------------------------------ check

def check(data, live_probe=False):
    """Return a list of problems. Empty list = the picture agrees with reality.

    live_probe=True also connects to every declared port, which is the whole
    point of the command but costs a few hundred ms.
    """
    problems = []
    layer_ids = {l["id"] for l in data.get("layers", [])}
    node_ids, seen = set(), set()
    for node in data.get("nodes", []):
        nid = node.get("id")
        if not nid:
            problems.append({"kind": "node-missing-id", "node": node})
            continue
        if nid in seen:
            problems.append({"kind": "duplicate-node-id", "node": nid})
        seen.add(nid)
        node_ids.add(nid)
        if node.get("layer") not in layer_ids:
            problems.append({"kind": "unknown-layer", "node": nid,
                             "layer": node.get("layer")})

    bridge_ids = set()
    for br in data.get("bridges", []):
        bid = "br-%s" % br.get("port")
        if bid in bridge_ids:
            problems.append({"kind": "duplicate-bridge-port", "port": br.get("port")})
        bridge_ids.add(bid)
        if br.get("state") not in ("ok", "bad", "unk"):
            problems.append({"kind": "bad-bridge-state", "port": br.get("port"),
                             "state": br.get("state")})
    # the bridge cluster hangs off the tier node; render() wires it as one
    known = node_ids | bridge_ids | {"tier1"}

    for edge in data.get("edges", []):
        for side in ("from", "to"):
            target = edge.get(side)
            if target not in known:
                problems.append({"kind": "dangling-edge", "edge": edge,
                                 "side": side, "target": target})
        if edge.get("kind") not in KIND_COLOR:
            problems.append({"kind": "unknown-edge-kind", "edge": edge})

    for legend in data.get("legend", []):
        if legend.get("kind") not in KIND_COLOR:
            problems.append({"kind": "unknown-legend-kind", "legend": legend})

    if live_probe:
        for node in data.get("nodes", []):
            port = (node.get("probe") or {}).get("port")
            if port and not listening(port):
                problems.append({"kind": "port-not-listening", "node": node.get("id"),
                                 "port": port})
        for br in data.get("bridges", []):
            port = br.get("port")
            if port and not listening(port):
                problems.append({"kind": "bridge-port-not-listening",
                                 "port": port, "bridge": br.get("name")})
    return problems


# ----------------------------------------------------------------------- render

def esc(text):
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_html(data, path=HTML_OUT):
    live = data.get("live") or {}
    ports = live.get("ports") or {}
    node_by_layer = {}
    for node in data.get("nodes", []):
        node_by_layer.setdefault(node.get("layer"), []).append(node)

    def tags_html(tags):
        out = []
        for t in tags or []:
            out.append('<span class="tag %s">%s</span>' % (esc(t.get("k") or "dim"),
                                                           esc(t.get("t"))))
        return "".join(out)

    def facts_html(facts):
        return "".join("<div><b>%s</b>：%s</div>" % (esc(k), esc(v))
                       for k, v in (facts or {}).items())

    cols = []
    for layer in data.get("layers", []):
        blocks = []
        for node in node_by_layer.get(layer["id"], []):
            port = (node.get("probe") or {}).get("port")
            live_bit = ""
            if port is not None:
                p = ports.get(str(port)) or {}
                dot = "ok" if p.get("listening") else "bad"
                extra = " · %s 模型" % p["models"] if p.get("models") else ""
                live_bit = ('<span class="tag %s">:%s %s%s</span>'
                            % (dot, port, "在听" if p.get("listening") else "未监听",
                               extra))
            blocks.append(
                '<div class="node" data-id="%s"><div class="t">%s</div>'
                '<div class="s">%s</div><div class="tags">%s%s</div></div>'
                % (esc(node["id"]), esc(node["title"]), esc(node.get("sub")),
                   tags_html(node.get("tags")), live_bit))
        if layer["id"] == "tier1":
            cells = []
            for br in data.get("bridges", []):
                p = (live.get("bridges") or {}).get(str(br.get("port"))) or {}
                cells.append(
                    '<div class="bridge %s" data-id="br-%s"><b>%s</b> %s'
                    '<br><span>%s</span>%s</div>'
                    % (esc(br.get("state")), br.get("port"), br.get("port"),
                       esc(br.get("name")), esc(br.get("label")),
                       ('<br><span class="free">%s</span>'
                        % esc(" / ".join(br.get("tags") or [])))
                       if br.get("tags") else ""))
            blocks.append('<div class="group" data-id="tier1"><div class="t">'
                          '档 1 · 订阅积分桥</div><div class="grid2">%s</div></div>'
                          % "".join(cells))
        cols.append('<div class="col"><h2>%s</h2><div class="stack">%s</div></div>'
                    % (esc(layer["title"]), "".join(blocks)))

    legend = "".join(
        '<span><i style="border-color:%s"></i>%s</span>' % (KIND_COLOR[l["kind"]],
                                                            esc(l["label"]))
        for l in data.get("legend", []) if l.get("kind") in KIND_COLOR)

    info = {}
    for node in data.get("nodes", []):
        info[node["id"]] = (node.get("title"), node.get("facts") or {})
    for br in data.get("bridges", []):
        info["br-%s" % br["port"]] = (
            "桥 %s · %s" % (br.get("port"), br.get("name")),
            {"端口": br.get("port"), "名称": br.get("name"), "来源": br.get("label"),
             "可达性": {"ok": "实测通", "bad": "实测不通", "unk": "未测"}.get(br.get("state")),
             "免费/限额": " / ".join(br.get("tags") or []) or "—"})

    edges = data.get("edges", [])
    stamp = live.get("measured_at") or data.get("updated") or ""
    ocx = live.get("ocx_models")
    prefixes = live.get("ocx_prefixes") or {}

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FleetKit 舰队拓扑 · %s</title>
<style>
 :root{--bg:#0e1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--dim:#8b949e;
 --ok:#3fb950;--warn:#d29922;--bad:#f85149;--main:#58a6ff;--free:#3fb950;--fix:#bc8cff}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
 font:13px/1.5 ui-sans-serif,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
 header{padding:18px 24px 14px;border-bottom:1px solid var(--line);display:flex;
 gap:16px;align-items:baseline;flex-wrap:wrap}h1{font-size:17px;margin:0}
 .stamp,.footer{color:var(--dim);font-size:12px}
 .legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--dim)}
 .legend i{display:inline-block;width:22px;height:0;border-top:2px solid;
 vertical-align:middle;margin-right:5px}
 main{position:relative;padding:8px 24px 40px;overflow-x:auto}
 #board{position:relative;min-width:1180px}
 #wires{position:absolute;inset:0;width:100%%;height:100%%;pointer-events:none}
 .cols{display:grid;grid-template-columns:210px 250px 300px 320px;gap:34px;
 position:relative;z-index:1}
 .col h2{font-size:12px;color:var(--dim);letter-spacing:.08em;margin:14px 0 10px;
 padding-bottom:6px;border-bottom:1px dashed var(--line)}
 .stack{display:flex;flex-direction:column;gap:10px}
 .node{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--line);
 border-radius:7px;padding:8px 10px;cursor:pointer}
 .node:hover,.node.on{border-color:var(--main);background:#1c2430}
 .node .t{font-weight:600}.node .s{color:var(--dim);font-size:11.5px;margin-top:2px}
 .tags{margin-top:5px;display:flex;gap:5px;flex-wrap:wrap}
 .tag{font-size:10.5px;padding:1px 6px;border-radius:9px;border:1px solid}
 .tag.ok{color:var(--ok);border-color:#245c2e}.tag.bad{color:var(--bad);border-color:#6e2b26}
 .tag.warn{color:var(--warn);border-color:#5c4519}.tag.dim{color:var(--dim);border-color:var(--line)}
 .tag.free{color:var(--free);border-color:#245c2e;background:#12241a}
 .tag.fix{color:var(--fix);border-color:#4b3670;background:#1d1729}
 .group{border:1px dashed var(--line);border-radius:8px;padding:8px 10px;background:#10151c}
 .group>.t{font-size:12px;font-weight:600;margin-bottom:7px}
 .grid2{display:grid;grid-template-columns:1fr 1fr;gap:7px}
 .bridge{font-size:11.5px;padding:5px 7px;border-radius:5px;background:var(--panel);
 border:1px solid var(--line);border-left:3px solid var(--line);cursor:pointer}
 .bridge.ok{border-left-color:var(--ok)}.bridge.bad{border-left-color:var(--bad)}
 .bridge.unk{border-left-color:var(--warn)}.bridge b{font-weight:600}
 .bridge span{color:var(--dim)}.bridge .free{color:var(--free)}
 #panel{position:fixed;right:18px;bottom:18px;width:340px;max-height:62vh;overflow:auto;
 background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;
 box-shadow:0 10px 30px rgba(0,0,0,.5);display:none;z-index:5}
 #panel h3{margin:0 0 6px;font-size:14px}#panel .kv{color:var(--dim);font-size:12px}
 #panel .kv b{color:var(--text)}#panel ul{margin:8px 0 0;padding-left:18px;
 color:var(--dim);font-size:12px}.close{float:right;cursor:pointer;color:var(--dim)}
 .fade{opacity:.28}
</style></head><body>
<header><h1>FleetKit 舰队拓扑</h1>
<span class="stamp">实测 %s%s</span>
<div class="legend">%s</div></header>
<main><div id="board"><svg id="wires"></svg><div class="cols">%s</div></div></main>
<div id="panel"><span class="close" onclick="document.getElementById('panel').style.display='none'">✕</span>
<h3 id="p-title"></h3><div class="kv" id="p-body"></div><ul id="p-list"></ul></div>
<div class="footer" style="padding:0 24px 28px">
 由 <code>kit/docs/fleet-topology.json</code> 生成（<code>python3 tools/topology.py all</code>）。
 改拓扑改 JSON，不要手改本文件。%s
</div>
<script>
const INFO=%s, EDGES=%s;
const board=document.getElementById('board'), wires=document.getElementById('wires');
const NS='http://www.w3.org/2000/svg';
function center(id){const el=board.querySelector('[data-id="'+id+'"]');if(!el)return null;
 const b=el.getBoundingClientRect(),r=board.getBoundingClientRect();
 return {x:b.left-r.left,y:b.top-r.top,w:b.width,h:b.height,
  cx:b.left-r.left+b.width/2,cy:b.top-r.top+b.height/2};}
function color(k){return k==='fix'?'#bc8cff':k==='free'?'#3fb950':k==='fall'?'#d29922':'#58a6ff';}
function draw(){const r=board.getBoundingClientRect();
 wires.setAttribute('viewBox','0 0 '+r.width+' '+r.height);wires.innerHTML='';
 EDGES.forEach(function(e){const p=center(e[0]),q=center(e[1]);if(!p||!q)return;
  const x1=p.x+p.w,y1=p.cy,x2=q.x,y2=q.cy,mx=(x1+x2)/2;
  const d='M'+x1+','+y1+' C'+mx+','+y1+' '+mx+','+y2+' '+x2+','+y2;
  const path=document.createElementNS(NS,'path');path.setAttribute('d',d);
  path.setAttribute('fill','none');path.setAttribute('stroke',color(e[2]));
  path.setAttribute('stroke-width',e[2]==='fix'?2:1.4);
  if(e[2]==='fall')path.setAttribute('stroke-dasharray','5 4');
  path.setAttribute('opacity',e[2]==='fix'?.95:.55);
  path.dataset.a=e[0];path.dataset.b=e[1];wires.appendChild(path);});}
function highlight(id){const n=new Set([id]);
 EDGES.forEach(function(e){if(e[0]===id)n.add(e[1]);if(e[1]===id)n.add(e[0]);});
 board.querySelectorAll('.node,.bridge,.group').forEach(function(el){
  el.classList.toggle('fade',!n.has(el.dataset.id));});
 wires.querySelectorAll('path').forEach(function(el){
  el.style.opacity=(el.dataset.a===id||el.dataset.b===id)?1:.12;});}
function clearAll(){board.querySelectorAll('.fade').forEach(function(el){
 el.classList.remove('fade');});
 wires.querySelectorAll('path').forEach(function(el){
  el.style.opacity=(el.dataset.k||'');});draw();}
function show(id){const info=INFO[id],panel=document.getElementById('panel');
 if(!info){panel.style.display='none';return;}
 document.getElementById('p-title').textContent=info[0];
 document.getElementById('p-body').innerHTML=Object.keys(info[1]).map(function(k){
  return '<div><b>'+k+'</b>：'+info[1][k]+'</div>';}).join('');
 var up=EDGES.filter(function(e){return e[0]===id;}).map(function(e){
  return '出 → '+(INFO[e[1]]?INFO[e[1]][0]:e[1])+(e[3]?'（'+e[3]+'）':'');});
 var dn=EDGES.filter(function(e){return e[1]===id;}).map(function(e){
  return '入 ← '+(INFO[e[0]]?INFO[e[0]][0]:e[0])+(e[3]?'（'+e[3]+'）':'');});
 document.getElementById('p-list').innerHTML=up.concat(dn).map(function(x){
  return '<li>'+x+'</li>';}).join('');
 panel.style.display='block';highlight(id);}
board.querySelectorAll('.node,.group,.bridge').forEach(function(el){
 el.onclick=function(){show(el.dataset.id);};
 el.onmouseenter=function(){highlight(el.dataset.id);};
 el.onmouseleave=function(){clearAll();};});
window.addEventListener('load',draw);window.addEventListener('resize',draw);
setTimeout(draw,60);
</script></body></html>""" % (
        esc(stamp), esc(stamp),
        (" · ocx %s 模型（%s）" % (ocx, "、".join(
            "%s %d" % (k, v) for k, v in list(prefixes.items())[:6])))
        if ocx else "",
        legend, "".join(cols),
        esc("当前 %s" % (", ".join("%s:%d" % (k, v) for k, v in
                                   sorted((live.get("fleet_reach") or {})
                                          .get("reachable", []) and
                                          {p: 1 for p in live["fleet_reach"]["reachable"]}.items())
                                   ) if live.get("fleet_reach") else "")),
        json.dumps(info, ensure_ascii=False),
        json.dumps([[e.get("from"), e.get("to"), e.get("kind"), e.get("note") or ""]
                    for e in edges], ensure_ascii=False))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


def render_md(data, path=MD_OUT):
    """Mermaid + tables: the same picture as text an AI can diff."""
    live = data.get("live") or {}
    ports = live.get("ports") or {}
    lines = ["# FleetKit 舰队拓扑", "",
             "> 由 `kit/docs/fleet-topology.json` 生成（`python3 tools/topology.py all`）。",
             "> 改拓扑改 JSON，不要手改本文件。实测：%s"
             % (live.get("measured_at") or data.get("updated") or "—"), ""]

    lines.append("```mermaid")
    lines.append("flowchart LR")
    for layer in data.get("layers", []):
        members = [n["id"] for n in data.get("nodes", []) if n.get("layer") == layer["id"]]
        if not members:
            continue
        lines.append('  subgraph %s["%s"]' % (layer["id"], layer["title"]))
        for mid in members:
            node = next(n for n in data["nodes"] if n["id"] == mid)
            lines.append('    %s["%s"]' % (mid, node["title"].replace('"', "'")))
        lines.append("  end")
    for br in data.get("bridges", []):
        lines.append('  br%s["%s %s"]' % (br["port"], br["port"], br["name"]))
    for e in data.get("edges", []):
        arrow = "-.->" if e.get("kind") == "fall" else "-->"
        note = ("|%s|" % e["note"]) if e.get("note") else ""
        lines.append("  %s %s%s %s" % (e["from"], arrow, note, e["to"]))
    lines.append("  tier1 --- br8787")
    lines.append("```")
    lines.append("")

    lines.append("## 入口端口（实测在听）")
    lines.append("")
    lines.append("| 组件 | 端口 | 状态 | 模型 |")
    lines.append("|---|---|---|---|")
    for node in data.get("nodes", []):
        port = (node.get("probe") or {}).get("port")
        if not port:
            continue
        p = ports.get(str(port)) or {}
        lines.append("| %s | %s | %s | %s |" % (
            node["title"], port, "在听" if p.get("listening") else "未监听",
            p.get("models", "—")))
    lines.append("")

    lines.append("## 订阅积分桥")
    lines.append("")
    lines.append("| 端口 | 桥 | 来源 | 状态 | 免费/限额 | 在听 |")
    lines.append("|---|---|---|---|---|---|")
    for br in data.get("bridges", []):
        p = (live.get("bridges") or {}).get(str(br.get("port"))) or {}
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            br.get("port"), br.get("name"), br.get("label"),
            {"ok": "通", "bad": "不通", "unk": "未测"}.get(br.get("state")),
            " / ".join(br.get("tags") or []) or "—",
            "是" if p.get("listening") else "否"))
    lines.append("")

    reach = live.get("fleet_reach") or {}
    if reach:
        lines.append("## fleet_probe 快照（%s）" % (reach.get("measured_at") or "—"))
        lines.append("")
        lines.append("- 可达：%s" % ", ".join(reach.get("reachable") or []))
        lines.append("- 不可达：%s" % ", ".join(reach.get("unreachable") or []))
        if reach.get("agentic_text_only"):
            lines.append("- **文本能答但工具调用不交回客户端**（agentic 客户端会卡死）：%s"
                         % ", ".join(reach["agentic_text_only"]))
        lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


# -------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", nargs="?", default="all",
                    choices=("check", "refresh", "render", "all"))
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--format", default="both", choices=("html", "md", "both"))
    ap.add_argument("--json", action="store_true",
                    help="check: print the problem list as JSON")
    ap.add_argument("--no-live", action="store_true",
                    help="check: skip connecting to declared ports")
    args = ap.parse_args(argv)

    data = load(args.data)

    if args.action in ("check", "all"):
        problems = check(data, live_probe=not args.no_live)
        if args.json:
            print(json.dumps(problems, ensure_ascii=False, indent=1))
        elif problems:
            print("check: %d problem(s)" % len(problems))
            for p in problems:
                print("  -", json.dumps(p, ensure_ascii=False))
        else:
            print("check: ok (%d nodes, %d edges, %d bridges)"
                  % (len(data.get("nodes", [])), len(data.get("edges", [])),
                     len(data.get("bridges", []))))
        if args.action == "check":
            return 1 if problems else 0

    if args.action in ("refresh", "all"):
        refresh(data)
        save(data, args.data)

    if args.action in ("render", "all"):
        if args.format in ("html", "both"):
            print("wrote", render_html(data))
        if args.format in ("md", "both"):
            print("wrote", render_md(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
