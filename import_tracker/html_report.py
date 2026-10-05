"""
Render a recorded dependency tree as a single, self-contained HTML file that
can be opened directly in a browser.
"""

# Standard
from typing import Any, Dict, List, Optional, Set, Tuple
import html as html_lib

# Local
from . import constants
from .log import log


def render_tree_html(
    tree: Dict[str, Any],
    title: Optional[str] = None,
) -> str:
    """Render an exported dependency tree dict as a standalone HTML document"""
    root = tree.get(constants.TREE_ROOT, "")
    nodes: List[Dict[str, Any]] = tree.get(constants.TREE_NODES, [])
    edges: List[Dict[str, Any]] = tree.get(constants.TREE_EDGES, [])

    node_names = {node["name"] for node in nodes}
    truncated_nodes = {
        node["name"] for node in nodes if node.get(constants.TREE_NODE_TRUNCATED)
    }
    depths = {node["name"]: node.get(constants.TREE_NODE_DEPTH, 0) for node in nodes}

    # Build adjacency from the already-truncated edge list
    adjacency: Dict[str, List[Dict[str, Any]]] = {}
    back_pairs: Set[Tuple[str, str]] = set()
    for edge in edges:
        src = edge[constants.TREE_EDGE_SRC]
        dst = edge[constants.TREE_EDGE_DST]
        adjacency.setdefault(src, []).append(edge)
        if edge.get(constants.TREE_EDGE_KIND) == constants.TREE_EDGE_BACK:
            back_pairs.add((src, dst))

    def render_node(name: str, ancestors: Tuple[str, ...]) -> str:
        """Render one node and its children, guarding against cycles"""
        esc = html_lib.escape(name)
        depth = depths.get(name, 0)
        classes = ["node"]
        if name == root:
            classes.append("root")
        if name in truncated_nodes:
            classes.append("truncated")
        badge = (
            '<span class="badge truncated-badge">truncated</span>'
            if name in truncated_nodes
            else ""
        )
        child_edges = sorted(
            adjacency.get(name, []), key=lambda e: e[constants.TREE_EDGE_DST]
        )

        child_html_parts: List[str] = []
        for edge in child_edges:
            dst = edge[constants.TREE_EDGE_DST]
            is_back = edge.get(constants.TREE_EDGE_KIND) == constants.TREE_EDGE_BACK
            opt = edge.get(constants.TREE_EDGE_OPTIONAL, False)
            edge_classes = ["edge"]
            if is_back:
                edge_classes.append("back")
            if opt:
                edge_classes.append("optional")
            kind_label = "back edge / cycle" if is_back else "forward"
            opt_label = "optional" if opt else "required"
            marker = (
                '<span class="badge cycle-badge">&#x21BB; cycle</span>'
                if is_back
                else ""
            )
            edge_header = (
                '<span class="edge-label">{} &rarr; {}</span>'
                ' <span class="meta">{} &middot; {}</span>{}'.format(
                    esc,
                    html_lib.escape(dst),
                    kind_label,
                    opt_label,
                    marker,
                )
            )
            if dst in ancestors:
                # Guard against runaway recursion when an edge closes a cycle
                child_html_parts.append(
                    '<li class="{}">{}</li>'.format(" ".join(edge_classes), edge_header)
                )
            else:
                child_html_parts.append(
                    '<li class="{}">{}<ul class="children">{}</ul></li>'.format(
                        " ".join(edge_classes),
                        edge_header,
                        render_node(dst, ancestors + (name,)),
                    )
                )

        children = "".join(child_html_parts)
        return (
            '<li class="{cls}"><span class="node-label">{name}'
            '<span class="depth">d{depth}</span></span>{badge}'
            '<ul class="children">{children}</ul></li>'.format(
                cls=" ".join(classes),
                name=esc,
                depth=depth,
                badge=badge,
                children=children,
            )
        )

    tree_html = (
        '<ul class="tree-root">{}</ul>'.format(render_node(root, (root,)))
        if root in node_names
        else '<p class="empty">No root node recorded.</p>'
    )

    # Flat edge table (authoritative list for manual verification)
    edge_rows = []
    for edge in edges:
        src = edge[constants.TREE_EDGE_SRC]
        dst = edge[constants.TREE_EDGE_DST]
        is_back = edge.get(constants.TREE_EDGE_KIND) == constants.TREE_EDGE_BACK
        opt = edge.get(constants.TREE_EDGE_OPTIONAL, False)
        edge_rows.append(
            "<tr{}><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                ' class="back"' if is_back else "",
                html_lib.escape(src),
                html_lib.escape(dst),
                "back (cycle)" if is_back else "forward",
                "optional" if opt else "required",
            )
        )

    n_back = sum(
        1
        for edge in edges
        if edge.get(constants.TREE_EDGE_KIND) == constants.TREE_EDGE_BACK
    )
    heading = html_lib.escape(title or root or "Dependency Tree")
    log.debug2(
        "Rendering HTML report: %d nodes, %d edges (%d back)",
        len(nodes),
        len(edges),
        n_back,
    )

    return _HTML_TEMPLATE.format(
        title=heading,
        root=html_lib.escape(root),
        n_nodes=len(nodes),
        n_edges=len(edges),
        n_back=n_back,
        tree=tree_html,
        edge_rows="".join(edge_rows) or '<tr><td colspan="4">No edges</td></tr>',
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
:root {{
  --bg:#f7f8fa; --card:#ffffff; --ink:#1f2430; --muted:#6b7280;
  --line:#d9dee7; --accent:#2563eb; --cycle:#b45309; --cycle-bg:#fef3c7;
  --trunc:#7c3aed; --trunc-bg:#ede9fe; --opt:#047857;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
header {{ padding:20px 28px; background:var(--card); border-bottom:1px solid var(--line); }}
header h1 {{ margin:0 0 4px; font-size:20px; }}
header .sub {{ color:var(--muted); font-size:13px; }}
.stats {{ margin-top:10px; display:flex; gap:10px; flex-wrap:wrap; }}
.stat {{ background:var(--bg); border:1px solid var(--line); border-radius:999px;
  padding:3px 12px; font-size:12px; }}
main {{ padding:22px 28px 60px; }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:16px 20px; margin-bottom:22px; }}
h2 {{ font-size:15px; margin:0 0 12px; }}
ul {{ list-style:none; padding-left:18px; margin:4px 0; }}
ul.tree-root {{ padding-left:0; }}
.node-label, .edge-label {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
.node.root > .node-label {{ font-weight:700; color:var(--accent); }}
.depth {{ color:var(--muted); font-size:11px; margin-left:8px;
  border:1px solid var(--line); border-radius:6px; padding:0 5px; }}
.badge {{ font-size:11px; border-radius:6px; padding:1px 7px; margin-left:8px;
  vertical-align:middle; }}
.cycle-badge {{ background:var(--cycle-bg); color:var(--cycle); border:1px solid var(--cycle); }}
.truncated-badge {{ background:var(--trunc-bg); color:var(--trunc); border:1px solid var(--trunc); }}
li.edge.back > .edge-label {{ color:var(--cycle); font-weight:700; }}
li.edge.optional > .edge-label {{ font-style:italic; }}
.meta {{ color:var(--muted); font-size:11px; margin-left:6px; }}
li.node.truncated > .node-label {{ color:var(--trunc); }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th, td {{ text-align:left; border-bottom:1px solid var(--line); padding:6px 10px; }}
th {{ color:var(--muted); font-weight:600; font-size:12px; }}
tr.back td {{ background:var(--cycle-bg); }}
td:nth-child(1), td:nth-child(2) {{
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
.legend {{ color:var(--muted); font-size:12px; margin-top:8px; }}
.legend .cycle-badge, .legend .truncated-badge {{ margin:0 4px 0 12px; }}
.empty {{ color:var(--muted); }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="sub">root module&nbsp;&middot;&nbsp;<code>{root}</code></div>
  <div class="stats">
    <span class="stat">{n_nodes} nodes</span>
    <span class="stat">{n_edges} edges</span>
    <span class="stat">{n_back} back edges</span>
  </div>
</header>
<main>
  <section>
    <h2>Dependency tree</h2>
    {tree}
    <div class="legend">
      <span class="badge cycle-badge">&#x21BB; cycle</span> marks a back edge
      (self reference or a dependency that points back to an ancestor)
      <span class="badge truncated-badge">truncated</span> marks a frontier node
      whose children were cut off by the depth limit.
    </div>
  </section>
  <section>
    <h2>All edges ({n_edges})</h2>
    <table>
      <thead><tr><th>From</th><th>To</th><th>Kind</th><th>Requirement</th></tr></thead>
      <tbody>{edge_rows}</tbody>
    </table>
  </section>
</main>
</body>
</html>
"""
