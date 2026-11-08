#!/usr/bin/env python3
"""Desktop mission-map visualizer for mrobosub-style repositories.

The scanner is static: scanned modules are never imported or executed. Run it
with::

    python mission_map_ui.py
    python mission_map_ui.py --root mrobosub --focus mrobosub_planning/standard_run.py

Then use the Tkinter window to scan the repository, scroll through every
discovered mission map, and inspect states and transitions. No HTTP server is
started and no files are written into the scanned checkout.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repo_state_vis import Machine, Repository, StateRef, wrap_label

NOTE = "Static possibilities; not a runtime reachability proof."


class GraphvizUnavailable(RuntimeError):
    """Raised when the optional Graphviz renderer cannot run."""


@dataclass
class ScanState:
    """Cached result of one repository scan."""

    root: Path
    repository: Repository
    machines: list[Machine]
    warnings: list[str]
    unvisualized: list[str]
    files: list[str]
    focus: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class LayoutNode:
    """One Graphviz node position, in points."""

    id: str
    x: float
    y: float
    width: float
    height: float


@dataclass
class LayoutEdge:
    """One Graphviz edge path, in points."""

    id: str
    tail: str
    head: str
    points: list[tuple[float, float]] = field(default_factory=list)
    arrow: list[tuple[float, float]] = field(default_factory=list)
    label_pos: tuple[float, float] | None = None


@dataclass
class MapLayout:
    """Coordinate system and drawable elements for one map."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float
    nodes: dict[str, LayoutNode] = field(default_factory=dict)
    edges: dict[str, LayoutEdge] = field(default_factory=dict)

    @property
    def width(self) -> float:
        return max(self.max_x - self.min_x, 1.0)

    @property
    def height(self) -> float:
        return max(self.max_y - self.min_y, 1.0)


def graphviz_available() -> bool:
    """Return whether both the Python package and dot executable are present."""

    return shutil.which("dot") is not None and importlib.util.find_spec("graphviz") is not None


def default_root() -> Path:
    """Choose a sensible repository when the caller does not provide one."""

    for candidate in (Path.cwd() / "mrobosub", Path(__file__).resolve().parent / "mrobosub"):
        if candidate.is_dir():
            return candidate.resolve()
    return Path.cwd().resolve()


def _module_of(repository: Repository, machine: Machine):
    module_name, _, _ = machine.name.rpartition(".")
    return repository.modules.get(module_name)


def _relative_file(repository: Repository, machine: Machine) -> str:
    module = _module_of(repository, machine)
    if module is None:
        return ""
    try:
        return module.path.relative_to(repository.root).as_posix()
    except ValueError:
        return module.path.as_posix()


def _focus_matches(repository: Repository, machine: Machine, focus: str) -> bool:
    if not focus:
        return True
    normalized = focus.strip().replace("\\", "/")
    if not normalized:
        return True
    relative = _relative_file(repository, machine)
    variable = machine.name.rpartition(".")[2]
    return (
        normalized == relative
        or relative.endswith("/" + normalized)
        or normalized == machine.name
        or normalized == variable
        or normalized.lower() in machine.name.lower()
        or normalized.lower() in relative.lower()
    )


def _map_source_line(repository: Repository, machine: Machine) -> int | None:
    module = _module_of(repository, machine)
    variable = machine.name.rpartition(".")[2]
    if module is None:
        return None
    expression = module.values.get(variable)
    return getattr(expression, "lineno", None)


def _class_source(repository: Repository, name: str) -> dict[str, Any]:
    info = repository.classes.get(name)
    if info is None:
        return {}
    try:
        path = info.module.path.relative_to(repository.root).as_posix()
    except ValueError:
        path = info.module.path.as_posix()
    return {"file": path, "line": info.node.lineno}


def _ordered_nodes(machine: Machine) -> list[StateRef]:
    return sorted(machine.nodes, key=lambda node: (node.name, node.params))


def _display_names(nodes: list[StateRef]) -> dict[StateRef, str]:
    short_names = [node.name.rsplit(".", 1)[-1] for node in nodes]
    counts = Counter(short_names)
    result: dict[StateRef, str] = {}
    for node, short in zip(nodes, short_names):
        result[node] = short if counts[short] == 1 else node.name
    return result


def _warnings_for_machine(machine: Machine, relative: str, warnings: list[str]) -> list[str]:
    windows_relative = relative.replace("/", "\\")
    return [
        warning
        for warning in warnings
        if machine.name in warning
        or (relative and (relative in warning or windows_relative in warning))
    ]


def _node_graph_label(machine: Machine, node: StateRef, display: str) -> str:
    label = display
    if node in machine.initial:
        label = f"START · {label}"
    if node in machine.stops:
        label += "\nSTOP"
    if node.params:
        label += "\n" + ", ".join(f"{key}={value}" for key, value in node.params)
    return wrap_label(label, width=28)


def _edge_graph_label_for(edge: Any, machine: Machine) -> str:
    """Build the short label used on a map, including declared-only status."""

    outcome = edge.label.partition("\n")[0]
    if edge.kind == "soft_stop":
        label = "soft stop"
    else:
        label = outcome or edge.label
        if edge.source in machine.stops:
            label = f"{label}\n(declared only)"
    return wrap_label(label, width=30)


def build_map_payload(
    repository: Repository,
    machine: Machine,
    index: int,
    all_warnings: list[str],
) -> dict[str, Any]:
    """Convert one analyzer machine into data used by the desktop window."""

    nodes = _ordered_nodes(machine)
    node_ids = {node: f"map{index}_node{number}" for number, node in enumerate(nodes)}
    displays = _display_names(nodes)
    edge_ids = {edge: f"map{index}_edge{number}" for number, edge in enumerate(machine.edges)}
    relative = _relative_file(repository, machine)
    line = _map_source_line(repository, machine)

    incoming = {edge.target for edge in machine.edges if edge.kind != "soft_stop"}
    outgoing = {edge.source for edge in machine.edges if edge.kind != "soft_stop"}
    outgoing_edges: dict[StateRef, list[str]] = {node: [] for node in nodes}
    incoming_edges: dict[StateRef, list[str]] = {node: [] for node in nodes}
    for edge in machine.edges:
        outgoing_edges.setdefault(edge.source, []).append(edge_ids[edge])
        incoming_edges.setdefault(edge.target, []).append(edge_ids[edge])

    node_payloads = []
    for node in nodes:
        start = node in machine.initial
        stop = node in machine.stops
        inferred_entry = not start and node in outgoing - incoming
        inferred_terminal = node in incoming - outgoing
        node_payloads.append(
            {
                "id": node_ids[node],
                "name": node.name,
                "short": displays[node],
                "graph_label": _node_graph_label(machine, node, displays[node]),
                "params": [{"name": key, "value": value} for key, value in node.params],
                "configured_start": start,
                "configured_stop": stop,
                "inferred_entry": inferred_entry,
                "inferred_terminal": inferred_terminal,
                "source": _class_source(repository, node.name),
                "outgoing": outgoing_edges.get(node, []),
                "incoming": incoming_edges.get(node, []),
            }
        )

    edge_payloads = []
    for edge in machine.edges:
        outcome, _, condition = edge.label.partition("\n")
        soft_stop = edge.kind == "soft_stop"
        declared_only = edge.source in machine.stops and not soft_stop
        edge_payloads.append(
            {
                "id": edge_ids[edge],
                "source": node_ids[edge.source],
                "target": node_ids[edge.target],
                "source_name": edge.source.name,
                "target_name": edge.target.name,
                "outcome": outcome or edge.label,
                "condition": condition.strip(),
                "label": edge.label,
                "graph_label": _edge_graph_label_for(edge, machine),
                "kind": edge.kind,
                "soft_stop": soft_stop,
                "declared_only": declared_only,
                "target_params": [{"name": key, "value": value} for key, value in edge.target.params],
                "source_location": _class_source(repository, edge.source.name),
            }
        )

    map_warnings = _warnings_for_machine(machine, relative, all_warnings)
    variable = machine.name.rpartition(".")[2]
    return {
        "id": index,
        "name": machine.name,
        "module": machine.name.rpartition(".")[0],
        "variable": variable,
        "file": relative,
        "line": line,
        "source": f"{relative}:{line}" if line else relative,
        "note": NOTE,
        "stats": {
            "states": len(nodes),
            "transitions": sum(edge.kind != "soft_stop" for edge in machine.edges),
            "soft_stops": sum(edge.kind == "soft_stop" for edge in machine.edges),
        },
        "configured_starts": [
            displays[node] for node in nodes if node in machine.initial
        ],
        "configured_stops": [
            displays[node] for node in nodes if node in machine.stops
        ],
        "nodes": node_payloads,
        "edges": edge_payloads,
        "warnings": map_warnings,
    }


def discover(root: Path, focus: str = "") -> ScanState:
    """Scan a repository without importing target modules."""

    root = root.expanduser().resolve()
    focus = focus.strip()
    if not root.is_dir():
        raise ValueError(f"Repository directory does not exist: {root}")

    repository = Repository(root)
    all_machines = repository.analyze()
    all_missions = [machine for machine in all_machines if machine.category == "mission"]
    files = sorted(
        {
            relative
            for machine in all_missions
            if (relative := _relative_file(repository, machine))
        }
    )
    missions = [
        machine for machine in all_missions if _focus_matches(repository, machine, focus)
    ]
    warnings = list(repository.warnings)
    unvisualized = sorted(
        {
            warning
            for warning in warnings
            if any(
                marker in warning
                for marker in (
                    "cannot parse",
                    "no statically resolvable",
                    "unsupported mutation",
                )
            )
        }
    )
    maps = [
        build_map_payload(repository, machine, index, warnings)
        for index, machine in enumerate(missions)
    ]
    payload = {
        "root": str(root),
        "root_name": root.name,
        "focus": focus,
        "files_scanned": repository.file_count,
        "map_count": len(maps),
        "total_map_count": len(all_missions),
        "files": files,
        "maps": maps,
        "warnings": warnings,
        "unvisualized": unvisualized,
        "note": NOTE,
        "graphviz": graphviz_available(),
    }
    return ScanState(
        root=root,
        repository=repository,
        machines=missions,
        warnings=warnings,
        unvisualized=unvisualized,
        files=files,
        focus=focus,
        payload=payload,
    )


def _node_attrs(machine: Machine, node: StateRef) -> dict[str, str]:
    attrs: dict[str, str] = {
        "shape": "ellipse",
        "style": "filled",
        "fillcolor": "#ffffff",
        "color": "#64748b",
        "fontcolor": "#0f172a",
        "penwidth": "1.5",
    }
    if node in machine.initial and node in machine.stops:
        attrs.update(
            fillcolor="#fef9c3",
            color="#a16207",
            penwidth="2.2",
            peripheries="2",
        )
    elif node in machine.initial:
        attrs.update(fillcolor="#dcfce7", color="#166534", penwidth="2.2")
    elif node in machine.stops:
        attrs.update(
            fillcolor="#fee2e2", color="#991b1b", penwidth="2.2", peripheries="2"
        )
    else:
        incoming = {edge.target for edge in machine.edges if edge.kind != "soft_stop"}
        outgoing = {edge.source for edge in machine.edges if edge.kind != "soft_stop"}
        if node in outgoing - incoming:
            attrs.update(style="filled,dashed", fillcolor="#ecfeff", color="#0e7490")
        elif node in incoming - outgoing:
            attrs.update(style="filled,dashed", fillcolor="#fffbeb", color="#b45309")
    return attrs


def build_digraph(
    repository: Repository,
    machine: Machine,
    index: int,
    rankdir: str = "TB",
    labels: bool = True,
) -> Any:
    """Build the Graphviz graph used for both layout and export."""

    if rankdir not in {"TB", "LR", "RL", "BT"}:
        raise ValueError(f"Unsupported graph direction: {rankdir}")
    try:
        import graphviz
    except ImportError as exc:
        raise GraphvizUnavailable(
            "The Python graphviz package is not installed. Install it with "
            "`python -m pip install graphviz`."
        ) from exc

    nodes = _ordered_nodes(machine)
    node_ids = {node: f"map{index}_node{number}" for number, node in enumerate(nodes)}
    displays = _display_names(nodes)
    edge_ids = {edge: f"map{index}_edge{number}" for number, edge in enumerate(machine.edges)}

    graph = graphviz.Digraph(f"mission_map_{index}")
    graph.attr(
        rankdir=rankdir,
        bgcolor="transparent",
        pad="0.25",
        nodesep="0.48",
        ranksep="0.72",
        fontname="Helvetica",
        margin="0",
    )
    graph.attr(
        "node",
        fontname="Helvetica",
        fontsize="11",
        margin="0.16,0.09",
    )
    graph.attr(
        "edge",
        color="#64748b",
        fontcolor="#334155",
        fontname="Helvetica",
        fontsize="9",
        arrowsize="0.72",
        penwidth="1.2",
    )

    for node in nodes:
        attrs = _node_attrs(machine, node)
        attrs.update(
            id=node_ids[node],
            tooltip=node.label,
            label=_node_graph_label(machine, node, displays[node]),
        )
        graph.node(node_ids[node], **attrs)

    for edge in machine.edges:
        attrs: dict[str, str] = {
            "id": edge_ids[edge],
            "tooltip": edge.label,
        }
        if edge.kind == "soft_stop":
            attrs.update(style="dashed", color="#b91c1c", fontcolor="#991b1b")
        elif edge.source in machine.stops:
            attrs.update(style="dotted", color="#94a3b8", fontcolor="#64748b")
        graph.edge(
            node_ids[edge.source],
            node_ids[edge.target],
            label=_edge_graph_label_for(edge, machine) if labels else "",
            **attrs,
        )
    return graph


def _parse_point(text: str) -> tuple[float, float]:
    x, y = text.split(",", 1)
    return float(x), float(y)


def _parse_points(text: str) -> list[tuple[float, float]]:
    tokens = text.split()
    if tokens and tokens[0].startswith(("e,", "s,")):
        tokens[0] = tokens[0][2:]
    points: list[tuple[float, float]] = []
    for token in tokens:
        if "," not in token:
            continue
        points.append(_parse_point(token))
    return points


def parse_graph_layout(data: dict[str, Any]) -> MapLayout:
    """Parse Graphviz JSON into a small, renderer-neutral coordinate model."""

    bb = [float(part) for part in str(data.get("bb", "")).split(",")]
    if len(bb) != 4:
        raise ValueError("Graphviz did not return a graph bounding box.")
    layout = MapLayout(min_x=bb[0], min_y=bb[1], max_x=bb[2], max_y=bb[3])

    gvid_to_name: dict[int, str] = {}
    for item in data.get("objects", []):
        if "tail" in item:
            continue
        name = item.get("name")
        pos = item.get("pos")
        if not name or not pos:
            continue
        x, y = _parse_point(str(pos))
        width = float(item.get("width", "0")) * 72.0
        height = float(item.get("height", "0")) * 72.0
        layout.nodes[str(name)] = LayoutNode(str(name), x, y, width, height)
        try:
            gvid_to_name[int(item["_gvid"])] = str(name)
        except (KeyError, TypeError, ValueError):
            pass

    for number, item in enumerate(data.get("edges", [])):
        edge_id = str(item.get("id") or f"edge{number}")
        try:
            tail = gvid_to_name[int(item["tail"])]
            head = gvid_to_name[int(item["head"])]
        except (KeyError, TypeError, ValueError):
            continue
        points = _parse_points(str(item.get("pos", "")))
        if not points:
            continue
        arrow: list[tuple[float, float]] = []
        for operation in item.get("_hdraw_", []):
            if operation.get("op") == "P":
                arrow = [
                    (float(point[0]), float(point[1]))
                    for point in operation.get("points", [])
                ]
                break
        label_pos = None
        if item.get("lp"):
            try:
                label_pos = _parse_point(str(item["lp"]))
            except (TypeError, ValueError):
                label_pos = None
        layout.edges[edge_id] = LayoutEdge(
            id=edge_id,
            tail=tail,
            head=head,
            points=points,
            arrow=arrow,
            label_pos=label_pos,
        )
    return layout


def build_map_graph(
    repository: Repository,
    machine: Machine,
    index: int,
    rankdir: str = "TB",
    labels: bool = True,
) -> tuple[Any, MapLayout]:
    """Build a Graphviz graph and parse its JSON layout for canvas rendering."""

    graph = build_digraph(repository, machine, index, rankdir=rankdir, labels=labels)
    try:
        raw = graph.pipe(format="json").decode("utf-8")
    except Exception as exc:
        import graphviz

        if isinstance(exc, graphviz.backend.ExecutableNotFound):
            raise GraphvizUnavailable(
                "Graphviz's `dot` executable was not found on PATH. Install Graphviz "
                "to render diagrams; the scan and state tables still work."
            ) from exc
        if isinstance(exc, graphviz.backend.CalledProcessError):
            raise GraphvizUnavailable(f"Graphviz could not render this map: {exc}") from exc
        raise
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GraphvizUnavailable("Graphviz returned an invalid layout description.") from exc
    return graph, parse_graph_layout(data)


def render_graph_bytes(graph: Any, format_: str) -> bytes:
    """Export a built graph without writing into the scanned repository."""

    try:
        return graph.pipe(format=format_)
    except Exception as exc:
        import graphviz

        if isinstance(exc, graphviz.backend.ExecutableNotFound):
            raise GraphvizUnavailable(
                "Graphviz's `dot` executable was not found on PATH."
            ) from exc
        if isinstance(exc, graphviz.backend.CalledProcessError):
            raise GraphvizUnavailable(f"Graphviz could not render this map: {exc}") from exc
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=default_root(),
        help="repository root to scan (default: ./mrobosub when present)",
    )
    parser.add_argument(
        "--focus",
        default="",
        help="optional repository-relative file, map name, or map variable",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"error: repository directory does not exist: {root}", file=sys.stderr)
        return 2
    try:
        from mission_map_gui import launch_gui
    except ImportError as exc:
        print(f"error: the desktop window needs Tkinter: {exc}", file=sys.stderr)
        return 2
    return launch_gui(root, focus=args.focus)


if __name__ == "__main__":
    raise SystemExit(main())
