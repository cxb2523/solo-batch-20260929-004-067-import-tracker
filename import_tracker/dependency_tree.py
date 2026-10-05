"""
This module implements the dependency-tree recorder used to keep raw,
idempotent import edges (including self references and import cycles) while a
module is being tracked.

Depth truncation is intentionally applied only at export time so that a single
recorded tree can produce reports at multiple depths without the reports
interfering with one another.
"""

# Standard
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple
import threading

# Local
from . import constants
from .log import log

## Public ######################################################################


class DependencyTree:
    """A raw dependency graph recorded for a single tracking session

    The tree holds a set of nodes (fully-qualified module names) and an
    insertion-ordered set of directed ``(src, dst)`` edges. Edge creation is
    idempotent: duplicate imports, module self references and A->B->A cycles
    never produce duplicate edges and can never trigger runaway recursion
    because the structure is a plain set keyed on node names.
    """

    def __init__(self, name: Optional[str] = None, ignore_stdlib: bool = True):
        self.name = name
        self.ignore_stdlib = ignore_stdlib
        self._nodes: Set[str] = set()
        self._edges: Set[Tuple[str, str]] = set()
        self._edge_order: List[Tuple[str, str]] = []
        self._optional_edges: Set[Tuple[str, str]] = set()

    def add_edge(self, src: str, dst: str, optional: bool = False) -> Tuple[str, str]:
        """Record a single directed import edge

        Repeated calls with the same ``(src, dst)`` pair are a no-op. A module
        importing itself (``src == dst``) is retained as a self back-edge.
        """
        edge = (src, dst)
        self._nodes.add(src)
        self._nodes.add(dst)
        if edge not in self._edges:
            self._edges.add(edge)
            self._edge_order.append(edge)
            log.debug2("Tree edge added: %s -> %s", src, dst)
        if optional:
            self._optional_edges.add(edge)
        return edge

    @property
    def nodes(self) -> Set[str]:
        """The set of recorded node names"""
        return set(self._nodes)

    @property
    def edges(self) -> List[Tuple[str, str]]:
        """The de-duplicated edges in first-seen order"""
        return list(self._edge_order)

    def _bfs_levels(self, root: str) -> Dict[str, int]:
        """Compute the minimum relative depth of every node reachable from the
        root. Back edges (including cycles) are ignored while assigning levels,
        so a cycle can never cause recursion to run away.
        """
        adjacency: Dict[str, List[str]] = {}
        for src, dst in self._edge_order:
            adjacency.setdefault(src, []).append(dst)

        levels: Dict[str, int] = {root: 0}
        frontier = [root]
        while frontier:
            next_frontier: List[str] = []
            for src in frontier:
                for dst in adjacency.get(src, []):
                    if dst not in levels:
                        levels[dst] = levels[src] + 1
                        next_frontier.append(dst)
            frontier = next_frontier

        # Defensive: any node not reachable from the root is pinned at depth 0
        for node in self._nodes:
            levels.setdefault(node, 0)
        return levels

    def _strong_components(self) -> Dict[str, int]:
        """Assign a strongly-connected-component id to every node

        An edge whose endpoints share a component participates in a directed
        cycle. Implemented iteratively (Kosaraju) so an import cycle can never
        cause runaway recursion.
        """
        adjacency: Dict[str, List[str]] = {node: [] for node in self._nodes}
        reverse: Dict[str, List[str]] = {node: [] for node in self._nodes}
        for src, dst in self._edge_order:
            adjacency.setdefault(src, []).append(dst)
            reverse.setdefault(dst, []).append(src)

        # First pass: finishing order on the forward graph
        visited: Set[str] = set()
        finish_order: List[str] = []
        for start in self._nodes:
            if start in visited:
                continue
            stack: List[Tuple[str, int]] = [(start, 0)]
            visited.add(start)
            while stack:
                node, child_idx = stack[-1]
                children = adjacency.get(node, [])
                if child_idx < len(children):
                    child = children[child_idx]
                    stack[-1] = (node, child_idx + 1)
                    if child not in visited:
                        visited.add(child)
                        stack.append((child, 0))
                else:
                    finish_order.append(node)
                    stack.pop()

        # Second pass: flood the reverse graph in reverse finish order
        components: Dict[str, int] = {}
        component_id = 0
        for start in reversed(finish_order):
            if start in components:
                continue
            flood = [start]
            components[start] = component_id
            while flood:
                node = flood.pop()
                for parent in reverse.get(node, []):
                    if parent not in components:
                        components[parent] = component_id
                        flood.append(parent)
            component_id += 1
        return components

    def export(
        self,
        root: str,
        max_depth: Optional[int] = constants.DEFAULT_MAX_DEPTH,
    ) -> Dict[str, Any]:
        """Export the tree as a JSON-serializable mapping

        Args:
            root:  str
                The fully-qualified name of the root tracked module.
            max_depth:  Optional[int]
                If given, edges whose source node is at or below this relative
                depth are hidden and the frontier nodes are retained with a
                ``truncated`` marker. ``None`` applies no limit. The recorded
                raw edges are never modified.
        """
        levels = self._bfs_levels(root)
        components = self._strong_components()

        def _participates_in_cycle(edge: Tuple[str, str]) -> bool:
            src, dst = edge
            return components.get(src) == components.get(dst) and levels.get(
                dst, 0
            ) <= levels.get(src, 0)

        # A node has outgoing edges in the raw graph if it appears as a source
        has_outgoing: Set[str] = {src for src, _ in self._edge_order}
        if max_depth is None:
            kept_edges = list(self._edge_order)
            truncated_nodes: Set[str] = set()
        else:
            # First cut forward edges at the depth frontier. The nodes exposed
            # by those edges define what is visible at this depth.
            forward_edges = [
                edge
                for edge in self._edge_order
                if (
                    not _participates_in_cycle(edge)
                    and levels.get(edge[0], 0) < max_depth
                )
            ]
            visible_nodes = {root}
            for src, dst in forward_edges:
                visible_nodes.add(src)
                visible_nodes.add(dst)

            # Back edges that close a cycle are retained as long as both ends
            # are visible at this depth. This keeps cycles visible at depth 2
            # without resurrecting nodes that the frontier cut hid (e.g. at
            # depth 0 the report still contains only the root).
            kept_pairs = set(forward_edges)
            for edge in self._edge_order:
                if (
                    _participates_in_cycle(edge)
                    and edge[0] in visible_nodes
                    and edge[1] in visible_nodes
                ):
                    kept_pairs.add(edge)
            kept_edges = [edge for edge in self._edge_order if edge in kept_pairs]

            truncated_nodes = {
                node
                for node in visible_nodes
                if levels.get(node, 0) >= max_depth and node in has_outgoing
            }

        # The visible nodes are the root plus the endpoints of every kept edge
        if max_depth is None:
            visible_nodes = {root}
            for src, dst in kept_edges:
                visible_nodes.add(src)
                visible_nodes.add(dst)

        edge_records: List[Dict[str, Any]] = []
        for src, dst in kept_edges:
            # A back edge participates in a cycle (see _participates_in_cycle)
            is_cycle = _participates_in_cycle((src, dst))
            edge_records.append(
                {
                    constants.TREE_EDGE_SRC: src,
                    constants.TREE_EDGE_DST: dst,
                    constants.TREE_EDGE_KIND: (
                        constants.TREE_EDGE_BACK
                        if is_cycle
                        else constants.TREE_EDGE_FORWARD
                    ),
                    constants.TREE_EDGE_OPTIONAL: (src, dst) in self._optional_edges,
                }
            )

        node_records = [
            {
                "name": node,
                constants.TREE_NODE_DEPTH: levels.get(node, 0),
                constants.TREE_NODE_TRUNCATED: node in truncated_nodes,
            }
            for node in sorted(visible_nodes)
        ]

        return {
            constants.TREE_ROOT: root,
            constants.TREE_NODES: node_records,
            constants.TREE_EDGES: edge_records,
        }


@contextmanager
def track_imports(
    name: Optional[str] = None,
    ignore_stdlib: bool = constants.IGNORE_STDLIB,
) -> Iterator[DependencyTree]:
    """Context manager that records a dependency tree for imports made inside

    Contexts may be nested. Each context owns an independent
    :class:`DependencyTree`; tracking performed while an inner context is
    active records into that inner tree only. If an inner context raises, the
    active-context stack is always restored on the way out so that the outer
    context keeps working and exits cleanly.
    """
    tree = DependencyTree(name=name, ignore_stdlib=ignore_stdlib)
    stack = getattr(_STATE, "stack", None)
    if stack is None:
        stack = []
        _STATE.stack = stack
    stack.append(tree)
    log.debug2("Opened track_imports context (depth=%d)", len(_STATE.stack))
    try:
        yield tree
    finally:
        # Restore the active-context state no matter how the context exits
        popped = stack.pop()
        assert popped is tree, "track_imports context stack corrupted!"
        log.debug2("Closed track_imports context (depth=%d)", len(_STATE.stack))


def active_tree() -> Optional[DependencyTree]:
    """Return the innermost active tracking tree, if any"""
    stack = getattr(_STATE, "stack", None)
    return stack[-1] if stack else None


## Private #####################################################################

_STATE = threading.local()
