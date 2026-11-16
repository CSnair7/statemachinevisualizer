#!/usr/bin/env python3
"""Mission-map mission-control window for mrobosub-style repositories.

This window is self-contained: it includes the static scanner and the
Graphviz layout adapter so it does not import ``mission_map_ui``. The scanner
never imports or executes modules from the scanned repository, and the window
starts no HTTP server or network listener.

Run it directly with::

    python mission_map_gui.py --root mrobosub
    python mission_map_gui.py --root mrobosub --focus testing_run.py

CustomTkinter is required for the desktop shell (``pip install customtkinter``);
Graphviz is optional and the state/transition tables remain available when
Graphviz is missing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import shutil
import sys
import threading
import tkinter as tk
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import customtkinter as ctk

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

# ---------------------------------------------------------------------------
# Mission-control visual design
# ---------------------------------------------------------------------------

BG = "#061326"
HEADER = "#081A31"
SIDEBAR = "#0A1E37"
CARD = "#0D2745"
CARD_DEEP = "#091D35"
CARD_SOFT = "#112F52"
GRAPH_BG = "#071B31"
LINE = "#1B3A5C"
LINE_SOFT = "#15314F"
TEXT = "#F3F8FF"
MUTED = "#91AAC4"
DIM = "#5E7C9B"
CYAN = "#67E8F9"
CYAN_DARK = "#21C4D9"
CYAN_SOFT = "#0C3D51"
NAV_SELECTED = "#0D3D52"
NAV_HOVER = "#102E4C"
GREEN = "#66E3AC"
GREEN_SOFT = "#103D33"
RED = "#FF8794"
RED_SOFT = "#43202B"
AMBER = "#FFD166"
AMBER_SOFT = "#3C2E14"
MAIZE = "#FFCB05"
SCROLL = "#28506F"
GRID = "#0D2943"
WHITE = "#FFFFFF"

FONT_BODY = ("Segoe UI", 12)
FONT_SMALL = ("Segoe UI", 10)
FONT_TINY = ("Segoe UI", 9)
FONT_MICRO = ("Segoe UI", 8)
FONT_DISPLAY = ("Segoe UI Semibold", 29)
FONT_CARD = ("Segoe UI Semibold", 21)
FONT_SECTION = ("Segoe UI Semibold", 11)
FONT_MONO = ("Consolas", 10)

FOCUS_ALL = "All mission maps"
DIRECTIONS = {
    "Top to bottom": "TB",
    "Left to right": "LR",
    "Right to left": "RL",
    "Bottom to top": "BT",
}


def short_map_name(map_data: dict[str, Any]) -> str:
    module = str(map_data.get("module", ""))
    variable = str(map_data.get("variable", ""))
    tail = module.rsplit(".", 1)[-1] if module else ""
    return f"{tail}.{variable}" if tail else variable or str(map_data.get("name", "map"))


def short_node_name(map_data: dict[str, Any], node_id: str) -> str:
    for node in map_data.get("nodes", []):
        if node["id"] == node_id:
            return node["short"]
    return node_id


def source_line(source: dict[str, Any] | None) -> str:
    if not source:
        return "unavailable"
    file = source.get("file", "")
    line = source.get("line")
    if file and line:
        return f"{file}:{line}"
    return file or "unavailable"


def format_params(params: list[dict[str, Any]]) -> str:
    return ", ".join(f"{item['name']}={item['value']}" for item in params) or "—"


def node_flags(node: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if node.get("configured_start"):
        flags.append("configured start")
    if node.get("configured_stop"):
        flags.append("configured stop")
    if node.get("inferred_entry"):
        flags.append("inferred entry")
    if node.get("inferred_terminal"):
        flags.append("inferred terminal")
    return flags or ["state"]


def edge_flags(edge: dict[str, Any]) -> list[str]:
    if edge.get("soft_stop"):
        return ["soft stop"]
    if edge.get("declared_only"):
        return ["declared only"]
    return ["declared transition"]


def _shorten(text: str, limit: int = 52) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


class MissionScroll(ctk.CTkScrollableFrame):
    """Scrollable surface that leaves graph canvases in control of the wheel."""

    def __init__(self, master: Any, **kwargs: Any):
        kwargs.setdefault("corner_radius", 0)
        kwargs.setdefault("fg_color", "transparent")
        kwargs.setdefault("scrollbar_button_color", SCROLL)
        kwargs.setdefault("scrollbar_button_hover_color", CYAN)
        super().__init__(master, **kwargs)

    def _check_if_valid_scroll(self, widget: Any) -> bool:
        # The graph needs the wheel for zoom and pan; do not also scroll the
        # surrounding card stack when the pointer is over its Canvas.
        if getattr(widget, "_mission_graph", False):
            return False
        return super()._check_if_valid_scroll(widget)


class Pill(ctk.CTkFrame):
    """Small status/metadata capsule used throughout the interface."""

    def __init__(
        self,
        master: Any,
        text: str,
        *,
        fg_color: str = CARD_SOFT,
        text_color: str = MUTED,
        font: tuple[Any, ...] = FONT_TINY,
        **kwargs: Any,
    ):
        super().__init__(master, fg_color=fg_color, corner_radius=9, **kwargs)
        self.label = ctk.CTkLabel(
            self,
            text=text,
            text_color=text_color,
            font=font,
            height=20,
            anchor="center",
        )
        self.label.pack(padx=9, pady=3)

    def set_text(self, text: str) -> None:
        self.label.configure(text=text)


def divider(
    master: Any,
    *,
    color: str = LINE_SOFT,
    pady: tuple[int, int] = (0, 0),
    padx: tuple[int, int] = (0, 0),
    grid_row: int | None = None,
) -> ctk.CTkFrame:
    line = ctk.CTkFrame(master, height=1, fg_color=color, corner_radius=0)
    if grid_row is None:
        line.pack(fill="x", padx=padx, pady=pady)
    else:
        line.grid(
            row=grid_row,
            column=0,
            columnspan=99,
            sticky="ew",
            padx=padx,
            pady=pady,
        )
    return line


class InspectorPanel(ctk.CTkFrame):
    """The right-hand context rail for a selected state or transition."""

    def __init__(self, parent: Any):
        super().__init__(
            parent,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=LINE,
        )
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 0))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            top,
            text="INSPECTOR",
            text_color=CYAN,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            top,
            text="READ ONLY",
            text_color=DIM,
            font=FONT_MICRO,
            anchor="e",
        ).grid(row=0, column=1, sticky="e")

        self.title_label = ctk.CTkLabel(
            self,
            text="Nothing selected",
            text_color=TEXT,
            font=("Segoe UI Semibold", 21),
            anchor="w",
            justify="left",
            wraplength=270,
        )
        self.title_label.grid(row=1, column=0, sticky="ew", padx=18, pady=(12, 0))

        self.context_label = ctk.CTkLabel(
            self,
            text="Pick a state or transition to inspect it",
            text_color=MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=270,
        )
        self.context_label.grid(row=2, column=0, sticky="ew", padx=18, pady=(4, 0))

        divider(self, color=LINE_SOFT, padx=(18, 18), pady=(14, 0), grid_row=3)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=4, column=0, columnspan=1, sticky="nsew", padx=14, pady=(0, 14))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        self.text = ctk.CTkTextbox(
            body,
            fg_color=CARD_DEEP,
            border_width=0,
            corner_radius=12,
            text_color=TEXT,
            font=("Segoe UI", 11),
            wrap="word",
            activate_scrollbars=True,
            scrollbar_button_color=SCROLL,
            scrollbar_button_hover_color=CYAN,
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        self._configure_tags()
        self.text.configure(state="disabled")
        self.show_prompt()

    def _configure_tags(self) -> None:
        # CTkTextbox deliberately forbids font overrides through its public
        # tag API, so style the underlying text widget for typographic tags.
        target = self.text._textbox
        target.tag_configure("eyebrow", foreground=CYAN, font=("Segoe UI Semibold", 9))
        target.tag_configure("title", foreground=TEXT, font=("Segoe UI Semibold", 18))
        target.tag_configure("heading", foreground=CYAN, font=("Segoe UI Semibold", 9))
        target.tag_configure("code", foreground="#D9E9FF", font=FONT_MONO)
        target.tag_configure("route", foreground=CYAN, font=FONT_MONO)
        target.tag_configure("muted", foreground=MUTED, font=FONT_SMALL)
        target.tag_configure("warn", foreground=AMBER, font=FONT_SMALL)

    def _reset(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")

    def _finish(self) -> None:
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def _write(self, value: str, tag: str | None = None) -> None:
        self.text.insert("end", value, tag or ())

    def _section(self, title: str) -> None:
        self._write("\n" + title.upper() + "\n", "heading")

    def show_prompt(self) -> None:
        self.title_label.configure(text="Nothing selected")
        self.context_label.configure(text="Pick a state or transition to inspect it")
        self._reset()
        self._write("◎\n\n", "title")
        self._write("Select a node or edge in a map, or choose a row from its tables.", "muted")
        self._write(
            "\n\nThe inspector stays read-only: it describes the static mission data "
            "without running the vehicle or the scanned modules.",
            "muted",
        )
        self._finish()

    def show_node(self, map_data: dict[str, Any], node: dict[str, Any]) -> None:
        self.title_label.configure(text=node["short"])
        self.context_label.configure(text=short_map_name(map_data))
        self._reset()
        self._write("STATE\n", "eyebrow")
        self._write(node["name"] + "\n", "title")
        self._write(
            "  ·  ".join(node_flags(node)) + "\n",
            "warn" if node.get("inferred_entry") else "muted",
        )
        self._section("Source")
        self._write(source_line(node.get("source")) + "\n", "code")
        self._section("Parameters")
        self._write(format_params(node.get("params", [])) + "\n", "code")
        self._section("Outgoing transitions")
        edges = {edge["id"]: edge for edge in map_data.get("edges", [])}
        outgoing = [edges[edge_id] for edge_id in node.get("outgoing", []) if edge_id in edges]
        if outgoing:
            for edge in outgoing:
                self._write(
                    f"{edge['outcome']}  →  {short_node_name(map_data, edge['target'])}\n",
                    "route",
                )
        else:
            self._write("None\n", "muted")
        self._section("Incoming transitions")
        incoming = [edges[edge_id] for edge_id in node.get("incoming", []) if edge_id in edges]
        if incoming:
            for edge in incoming:
                self._write(
                    f"{short_node_name(map_data, edge['source'])}  →  {edge['outcome']}\n",
                    "route",
                )
        else:
            self._write("None\n", "muted")
        self._finish()

    def show_edge(self, map_data: dict[str, Any], edge: dict[str, Any]) -> None:
        self.title_label.configure(text=edge["outcome"])
        self.context_label.configure(text=short_map_name(map_data))
        self._reset()
        self._write("TRANSITION\n", "eyebrow")
        self._write(edge["outcome"] + "\n", "title")
        self._write(
            "  ·  ".join(edge_flags(edge)) + "\n",
            "warn" if edge.get("soft_stop") else "muted",
        )
        self._section("Route")
        self._write(
            f"{short_node_name(map_data, edge['source'])}  →  "
            f"{short_node_name(map_data, edge['target'])}\n",
            "route",
        )
        self._section("Condition")
        self._write((edge.get("condition") or "—") + "\n", "code")
        self._section("Full outcome label")
        self._write(edge.get("label", edge["outcome"]) + "\n", "code")
        self._section("Target parameters")
        self._write(format_params(edge.get("target_params", [])) + "\n", "code")
        self._section("Source")
        self._write(source_line(edge.get("source_location")) + "\n", "code")
        self._finish()


class GraphView:
    """Pan/zoom canvas renderer for one Graphviz map layout."""

    MARGIN = 32

    def __init__(self, canvas: tk.Canvas, card: "MapCard", layout: MapLayout):
        self.canvas = canvas
        self.card = card
        self.layout = layout
        self.node_map = {node["id"]: node for node in card.map_data["nodes"]}
        self.edge_map = {edge["id"]: edge for edge in card.map_data["edges"]}
        self.scale = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.selected: tuple[str, str] | None = None
        self.initialized = False
        self.manual = False
        self._disposed = False
        self._press: tuple[float, float] | None = None
        self._dragged = False
        for sequence in (
            "<Configure>",
            "<ButtonPress-1>",
            "<B1-Motion>",
            "<ButtonRelease-1>",
            "<Motion>",
            "<MouseWheel>",
            "<Button-4>",
            "<Button-5>",
            "<KeyPress-0>",
            "<KeyPress-plus>",
            "<KeyPress-equal>",
            "<KeyPress-minus>",
        ):
            canvas.unbind(sequence)
        canvas.configure(bg=GRAPH_BG, cursor="fleur", takefocus=1, highlightthickness=0)
        canvas._mission_graph = True  # type: ignore[attr-defined]
        canvas.bind("<Configure>", self._on_configure)
        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_motion)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        canvas.bind("<Motion>", self._on_hover)
        canvas.bind("<MouseWheel>", self._on_wheel)
        canvas.bind("<Button-4>", self._on_wheel)
        canvas.bind("<Button-5>", self._on_wheel)
        canvas.bind("<KeyPress-0>", lambda _event: self.fit())
        canvas.bind("<KeyPress-plus>", lambda _event: self.zoom(1.18))
        canvas.bind("<KeyPress-equal>", lambda _event: self.zoom(1.18))
        canvas.bind("<KeyPress-minus>", lambda _event: self.zoom(0.85))
        self.canvas.after_idle(self.fit)

    def dispose(self) -> None:
        self._disposed = True

    def fit(self) -> None:
        if self._disposed:
            return
        try:
            if not self.canvas.winfo_exists():
                return
        except tk.TclError:
            return
        width = max(self.canvas.winfo_width(), 260)
        height = max(self.canvas.winfo_height(), 220)
        usable_w = max(width - 2 * self.MARGIN, 1)
        usable_h = max(height - 2 * self.MARGIN, 1)
        self.scale = max(
            0.08,
            min(usable_w / self.layout.width, usable_h / self.layout.height, 3.0),
        )
        self.pan_x = (width - self.layout.width * self.scale) / 2
        self.pan_y = (height - self.layout.height * self.scale) / 2
        self.manual = False
        self.initialized = True
        self.redraw()

    def zoom(self, factor: float, x: float | None = None, y: float | None = None) -> None:
        if not self.initialized:
            self.fit()
            return
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        x = width / 2 if x is None else x
        y = height / 2 if y is None else y
        new_scale = max(0.08, min(6.0, self.scale * factor))
        if abs(new_scale - self.scale) < 0.0001:
            return
        world_x = (x - self.pan_x) / self.scale + self.layout.min_x
        world_y = self.layout.max_y - (y - self.pan_y) / self.scale
        self.scale = new_scale
        self.pan_x = x - (world_x - self.layout.min_x) * self.scale
        self.pan_y = y - (self.layout.max_y - world_y) * self.scale
        self.manual = True
        self.redraw()

    def set_selected(self, kind: str | None, item_id: str | None) -> None:
        self.selected = (kind, item_id) if kind and item_id else None
        self.redraw()

    def _point(self, x: float, y: float) -> tuple[float, float]:
        return (
            (x - self.layout.min_x) * self.scale + self.pan_x,
            (self.layout.max_y - y) * self.scale + self.pan_y,
        )

    def redraw(self) -> None:
        if self._disposed:
            return
        self.canvas.delete("all")
        width = max(self.canvas.winfo_width(), 2)
        height = max(self.canvas.winfo_height(), 2)
        self.canvas.create_rectangle(
            0, 0, width, height, fill=GRAPH_BG, outline="", tags=("graph-background",)
        )
        for x in range(0, width, 36):
            self.canvas.create_line(x, 0, x, height, fill=GRID, tags=("graph-grid",))
        for y in range(0, height, 36):
            self.canvas.create_line(0, y, width, y, fill=GRID, tags=("graph-grid",))

        for edge in self.layout.edges.values():
            self._draw_edge(edge)
        for node in self.layout.nodes.values():
            self._draw_node(node)

    def _draw_edge(self, edge: Any) -> None:
        meta = self.edge_map.get(edge.id, {})
        soft = bool(meta.get("soft_stop"))
        declared = bool(meta.get("declared_only"))
        selected = self.selected == ("edge", edge.id)
        color = RED if soft else (DIM if declared else "#7597B8")
        label_color = AMBER if soft else (DIM if declared else MUTED)
        width = 2.0 if soft else 1.4
        dash: tuple[int, int] | None = (6, 4) if soft else ((2, 3) if declared else None)
        if selected:
            color = CYAN
            label_color = CYAN
            width += 1.4
        tags = (f"edge:{edge.id}", "edge")
        points = [self._point(x, y) for x, y in edge.points]
        if len(points) >= 2:
            flat = [coordinate for point in points for coordinate in point]
            kwargs: dict[str, Any] = {
                "fill": color,
                "width": max(1, int(width * max(0.8, min(self.scale, 1.4)))),
                "smooth": True,
                "splinesteps": 24,
                "tags": tags,
            }
            if dash:
                kwargs["dash"] = dash
            self.canvas.create_line(*flat, **kwargs)

        if edge.arrow:
            arrow = [self._point(x, y) for x, y in edge.arrow]
            self.canvas.create_polygon(
                *[coordinate for point in arrow for coordinate in point],
                fill=color,
                outline=color,
                tags=tags,
            )
        else:
            self._draw_arrow(points, color, tags)

        label = meta.get("graph_label", "")
        if label:
            position = edge.label_pos
            if position is None and points:
                position = points[len(points) // 2]
            if position is not None:
                if edge.label_pos is not None:
                    x, y = self._point(*edge.label_pos)
                else:
                    x, y = position
                self.canvas.create_text(
                    x,
                    y,
                    text=label,
                    fill=label_color,
                    font=("Segoe UI", max(8, int(9 * self.scale))),
                    justify="center",
                    tags=tags,
                )

    def _draw_arrow(self, points: list[tuple[float, float]], color: str, tags: tuple[str, ...]) -> None:
        if len(points) < 2:
            return
        (x1, y1), (x2, y2) = points[-2], points[-1]
        angle = math.atan2(y2 - y1, x2 - x1)
        size = max(5, min(12, 8 * self.scale))
        left = (
            x2 - size * math.cos(angle - 0.45),
            y2 - size * math.sin(angle - 0.45),
        )
        right = (
            x2 - size * math.cos(angle + 0.45),
            y2 - size * math.sin(angle + 0.45),
        )
        self.canvas.create_polygon(
            x2,
            y2,
            left[0],
            left[1],
            right[0],
            right[1],
            fill=color,
            outline=color,
            tags=tags,
        )

    def _draw_node(self, node: Any) -> None:
        meta = self.node_map.get(node.id, {})
        center_x, center_y = self._point(node.x, node.y)
        width = max(8, node.width * self.scale)
        height = max(8, node.height * self.scale)
        left, top = center_x - width / 2, center_y - height / 2
        right, bottom = center_x + width / 2, center_y + height / 2
        fill, outline, dash, double = self._node_style(meta)
        selected = self.selected == ("node", node.id)
        line_width = max(1, int((2.4 if selected else 1.5) * max(0.8, min(self.scale, 1.4))))
        tags = (f"node:{node.id}", "node")
        if selected:
            self.canvas.create_oval(
                left - 5,
                top - 5,
                right + 5,
                bottom + 5,
                fill="",
                outline="#0C566B",
                width=5,
                tags=tags,
            )
            outline = CYAN
            line_width = max(3, line_width)
        kwargs: dict[str, Any] = {
            "fill": fill,
            "outline": outline,
            "width": line_width,
            "tags": tags,
        }
        if dash:
            kwargs["dash"] = dash
        self.canvas.create_oval(left, top, right, bottom, **kwargs)
        if double:
            inset = max(2, 3 * self.scale)
            self.canvas.create_oval(
                left + inset,
                top + inset,
                right - inset,
                bottom - inset,
                fill="",
                outline=outline,
                width=max(1, line_width - 1),
                tags=tags,
            )
        label = meta.get("graph_label") or meta.get("short") or node.id
        self.canvas.create_text(
            center_x,
            center_y,
            text=label,
            fill=TEXT,
            width=max(44, int(width - 12)),
            justify="center",
            font=("Segoe UI", max(8, int(10 * self.scale))),
            tags=tags,
        )

    def _node_style(self, meta: dict[str, Any]) -> tuple[str, str, tuple[int, int] | None, bool]:
        if meta.get("configured_start") and meta.get("configured_stop"):
            return "#3B3210", MAIZE, None, True
        if meta.get("configured_start"):
            return GREEN_SOFT, GREEN, None, False
        if meta.get("configured_stop"):
            return RED_SOFT, RED, None, True
        if meta.get("inferred_entry"):
            return "#0B3448", CYAN, (5, 3), False
        if meta.get("inferred_terminal"):
            return "#3B2D12", AMBER, (5, 3), False
        return CARD_SOFT, "#5E7C9C", None, False

    def _on_configure(self, _event: tk.Event[Any]) -> None:
        if self._disposed:
            return
        if not self.initialized:
            self.canvas.after_idle(self.fit)
        elif not self.manual:
            self.fit()
        else:
            self.redraw()

    def _on_press(self, event: tk.Event[Any]) -> None:
        self.canvas.focus_set()
        self._press = (event.x, event.y)
        self._dragged = False

    def _on_motion(self, event: tk.Event[Any]) -> None:
        if self._press is None:
            return
        dx = event.x - self._press[0]
        dy = event.y - self._press[1]
        if abs(dx) + abs(dy) < 4 and not self._dragged:
            return
        self._dragged = True
        self._press = (event.x, event.y)
        self.pan_x += dx
        self.pan_y += dy
        self.manual = True
        self.redraw()

    def _on_release(self, event: tk.Event[Any]) -> None:
        dragged = self._dragged
        self._press = None
        self._dragged = False
        self.canvas.configure(cursor="fleur")
        if dragged:
            return
        found: dict[str, str] = {}
        for item in self.canvas.find_overlapping(
            event.x - 2, event.y - 2, event.x + 2, event.y + 2
        ):
            for tag in self.canvas.gettags(item):
                if tag.startswith("node:"):
                    found.setdefault("node", tag.split(":", 1)[1])
                elif tag.startswith("edge:"):
                    found.setdefault("edge", tag.split(":", 1)[1])
        if "node" in found:
            self.card.app.select_node(self.card, found["node"])
        elif "edge" in found:
            self.card.app.select_edge(self.card, found["edge"])

    def _on_hover(self, event: tk.Event[Any]) -> None:
        if self._press is not None:
            return
        cursor = "fleur"
        for item in self.canvas.find_overlapping(
            event.x - 2, event.y - 2, event.x + 2, event.y + 2
        ):
            tags = self.canvas.gettags(item)
            if any(tag.startswith(("node:", "edge:")) for tag in tags):
                cursor = "hand2"
                break
        self.canvas.configure(cursor=cursor)

    def _on_wheel(self, event: tk.Event[Any]) -> str:
        if getattr(event, "num", None) == 4:
            factor = 1.12
        elif getattr(event, "num", None) == 5:
            factor = 0.89
        else:
            factor = 1.12 if event.delta > 0 else 0.89
        self.zoom(factor, event.x, event.y)
        return "break"


class MapCard(ctk.CTkFrame):
    """One mission-map surface with diagram, state table, and transition table."""

    def __init__(
        self,
        parent: Any,
        app: "MissionMapApp",
        map_data: dict[str, Any],
        machine: Machine,
    ):
        super().__init__(
            parent,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=LINE,
        )
        self.app = app
        self.map_data = map_data
        self.machine = machine
        self.graph: Any = None
        self.layout: MapLayout | None = None
        self.graph_view: GraphView | None = None
        self._selected: tuple[str, str] | None = None
        self._syncing = False
        self._build()
        self.reload_graph()

    def _build(self) -> None:
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=18)

        top = ctk.CTkFrame(content, fg_color="transparent")
        top.pack(fill="x")
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            top,
            text=f"MISSION MAP {self.map_data['id'] + 1:02d}",
            text_color=CYAN,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            top,
            text="STATIC LAYOUT",
            text_color=DIM,
            font=FONT_MICRO,
            anchor="e",
        ).grid(row=0, column=1, sticky="e")

        self.title_label = ctk.CTkLabel(
            content,
            text=self.map_data["name"],
            text_color=TEXT,
            font=FONT_CARD,
            anchor="w",
            justify="left",
            wraplength=650,
        )
        self.title_label.pack(fill="x", pady=(5, 0))
        ctk.CTkLabel(
            content,
            text=self.map_data.get("source") or self.map_data.get("file", ""),
            text_color=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(3, 0))

        stats = ctk.CTkFrame(content, fg_color="transparent")
        stats.pack(fill="x", pady=(11, 0))
        map_stats = self.map_data["stats"]
        Pill(stats, f"{map_stats['states']} states").pack(side="left", padx=(0, 6))
        Pill(stats, f"{map_stats['transitions']} transitions").pack(side="left", padx=(0, 6))
        if map_stats.get("soft_stops"):
            Pill(
                stats,
                f"{map_stats['soft_stops']} soft stop"
                + ("s" if map_stats["soft_stops"] != 1 else ""),
                fg_color=RED_SOFT,
                text_color=RED,
            ).pack(side="left", padx=(0, 6))
        if self.map_data.get("configured_starts"):
            Pill(
                stats,
                "start: " + ", ".join(self.map_data["configured_starts"]),
                fg_color=GREEN_SOFT,
                text_color=GREEN,
            ).pack(side="left", padx=(0, 6))
        if self.map_data.get("configured_stops"):
            Pill(
                stats,
                "stop: " + ", ".join(self.map_data["configured_stops"]),
                fg_color=RED_SOFT,
                text_color=RED,
            ).pack(side="left", padx=(0, 6))

        if self.map_data.get("warnings"):
            warn = ctk.CTkFrame(
                content,
                fg_color=AMBER_SOFT,
                corner_radius=11,
                border_width=1,
                border_color="#6A5220",
            )
            warn.pack(fill="x", pady=(13, 0))
            ctk.CTkLabel(
                warn,
                text="⚠  " + "\n".join(self.map_data["warnings"]),
                text_color=AMBER,
                font=FONT_SMALL,
                anchor="w",
                justify="left",
                wraplength=430,
            ).pack(fill="x", padx=12, pady=9)

        graph_shell = ctk.CTkFrame(
            content,
            fg_color=GRAPH_BG,
            corner_radius=14,
            border_width=1,
            border_color=LINE,
        )
        graph_shell.pack(fill="x", pady=(14, 0))
        graph_bar = ctk.CTkFrame(graph_shell, fg_color="transparent")
        graph_bar.pack(fill="x", padx=12, pady=(10, 4))
        graph_bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            graph_bar,
            text="STATE FLOW",
            text_color=CYAN,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            graph_bar,
            text="drag to pan  ·  scroll to zoom  ·  click to inspect",
            text_color=DIM,
            font=FONT_MICRO,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        controls = ctk.CTkFrame(graph_bar, fg_color="transparent")
        controls.grid(row=0, column=1, rowspan=2, sticky="e")
        self.zoom_out_btn = ctk.CTkButton(
            controls,
            text="−",
            width=31,
            height=28,
            fg_color=CARD_SOFT,
            hover_color=NAV_HOVER,
            text_color=MUTED,
            font=("Segoe UI Semibold", 15),
            corner_radius=7,
            command=lambda: self._zoom(0.83),
        )
        self.zoom_out_btn.pack(side="right", padx=(4, 0))
        self.zoom_in_btn = ctk.CTkButton(
            controls,
            text="+",
            width=31,
            height=28,
            fg_color=CARD_SOFT,
            hover_color=NAV_HOVER,
            text_color=MUTED,
            font=("Segoe UI Semibold", 14),
            corner_radius=7,
            command=lambda: self._zoom(1.2),
        )
        self.zoom_in_btn.pack(side="right", padx=(4, 0))
        self.fit_btn = ctk.CTkButton(
            controls,
            text="Fit",
            width=42,
            height=28,
            fg_color=CARD_SOFT,
            hover_color=NAV_HOVER,
            text_color=MUTED,
            font=FONT_TINY,
            corner_radius=7,
            command=self.fit_graph,
        )
        self.fit_btn.pack(side="right", padx=(8, 0))
        self.png_btn = ctk.CTkButton(
            controls,
            text="PNG",
            width=44,
            height=28,
            fg_color="transparent",
            hover_color=NAV_HOVER,
            text_color=MUTED,
            border_width=1,
            border_color=LINE,
            font=FONT_TINY,
            corner_radius=7,
            command=lambda: self.export_graph("png"),
        )
        self.png_btn.pack(side="right", padx=(4, 0))
        self.svg_btn = ctk.CTkButton(
            controls,
            text="SVG",
            width=44,
            height=28,
            fg_color="transparent",
            hover_color=NAV_HOVER,
            text_color=MUTED,
            border_width=1,
            border_color=LINE,
            font=FONT_TINY,
            corner_radius=7,
            command=lambda: self.export_graph("svg"),
        )
        self.svg_btn.pack(side="right", padx=(4, 0))

        self.canvas = tk.Canvas(
            graph_shell,
            height=380,
            bg=GRAPH_BG,
            bd=0,
            highlightthickness=0,
        )
        self.canvas.pack(fill="x", padx=10, pady=(0, 10))
        self.canvas._mission_graph = False  # type: ignore[attr-defined]
        self._graph_buttons = (
            self.zoom_in_btn,
            self.zoom_out_btn,
            self.fit_btn,
            self.svg_btn,
            self.png_btn,
        )

        self.tabs = ctk.CTkTabview(
            content,
            height=278,
            fg_color=CARD,
            segmented_button_fg_color=CARD_DEEP,
            segmented_button_selected_color=CYAN_SOFT,
            segmented_button_selected_hover_color=CYAN_SOFT,
            segmented_button_unselected_color=CARD_DEEP,
            segmented_button_unselected_hover_color=NAV_HOVER,
            text_color=TEXT,
            text_color_disabled=DIM,
            corner_radius=12,
            border_width=1,
            border_color=LINE,
        )
        self.tabs.pack(fill="x", pady=(14, 0))
        states_tab = self.tabs.add("States")
        transitions_tab = self.tabs.add("Transitions")
        self.states_tab = "States"
        self.transitions_tab = "Transitions"

        self.state_tree = self._build_table(
            states_tab,
            columns=("state", "params", "flags"),
            headings=("State", "Parameters", "Signals"),
            widths=(120, 190, 145),
            height=min(max(len(self.map_data["nodes"]), 3), 6),
        )
        for node in self.map_data["nodes"]:
            tags = ("node",)
            if node.get("configured_start"):
                tags = ("start",)
            elif node.get("configured_stop"):
                tags = ("stop",)
            elif node.get("inferred_entry"):
                tags = ("entry",)
            elif node.get("inferred_terminal"):
                tags = ("terminal",)
            self.state_tree.insert(
                "",
                "end",
                iid=node["id"],
                values=(
                    node["short"],
                    format_params(node.get("params", [])),
                    ", ".join(node_flags(node)),
                ),
                tags=tags,
            )
        self.state_tree.tag_configure("start", foreground=GREEN)
        self.state_tree.tag_configure("stop", foreground=RED)
        self.state_tree.tag_configure("entry", foreground=CYAN)
        self.state_tree.tag_configure("terminal", foreground=AMBER)
        self.state_tree.bind("<<TreeviewSelect>>", self._state_selected)

        self.edge_tree = self._build_table(
            transitions_tab,
            columns=("from", "outcome", "to", "flags"),
            headings=("From", "Outcome", "To", "Type"),
            widths=(95, 175, 95, 90),
            height=min(max(len(self.map_data["edges"]), 3), 6),
        )
        for edge in self.map_data["edges"]:
            tags = (
                ("soft",)
                if edge.get("soft_stop")
                else (("declared",) if edge.get("declared_only") else ())
            )
            self.edge_tree.insert(
                "",
                "end",
                iid=edge["id"],
                values=(
                    short_node_name(self.map_data, edge["source"]),
                    edge["outcome"],
                    short_node_name(self.map_data, edge["target"]),
                    ", ".join(edge_flags(edge)),
                ),
                tags=tags,
            )
        self.edge_tree.tag_configure("soft", foreground=RED)
        self.edge_tree.tag_configure("declared", foreground=DIM)
        self.edge_tree.bind("<<TreeviewSelect>>", self._edge_selected)

        if self.map_data.get("warnings"):
            warn_tab = self.tabs.add("Warnings")
            ctk.CTkLabel(
                warn_tab,
                text="\n\n".join(self.map_data["warnings"]),
                text_color=AMBER,
                font=FONT_SMALL,
                anchor="w",
                justify="left",
                wraplength=420,
            ).pack(fill="x", padx=12, pady=12)

        ctk.CTkLabel(
            content,
            text="Static possibilities, not a runtime reachability proof.",
            text_color=DIM,
            font=FONT_MICRO,
            anchor="w",
        ).pack(fill="x", pady=(9, 0))

        self.bind("<Configure>", self._on_card_resize)

    def _on_card_resize(self, event: tk.Event[Any]) -> None:
        if event.widget is self:
            self.title_label.configure(wraplength=max(340, event.width - 56))

    def _build_table(
        self,
        tab: Any,
        *,
        columns: tuple[str, ...],
        headings: tuple[str, ...],
        widths: tuple[int, ...],
        height: int,
    ) -> ttk.Treeview:
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        tree = ttk.Treeview(
            tab,
            columns=columns,
            show="headings",
            height=height,
            selectmode="browse",
        )
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=90, stretch=True, anchor="w")
        scroll = ctk.CTkScrollbar(
            tab,
            command=tree.yview,
            width=12,
            fg_color="transparent",
            button_color=SCROLL,
            button_hover_color=CYAN,
        )
        xscroll = ctk.CTkScrollbar(
            tab,
            command=tree.xview,
            orientation="horizontal",
            height=10,
            fg_color="transparent",
            button_color=SCROLL,
            button_hover_color=CYAN,
        )
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns", padx=(5, 0))
        xscroll.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        return tree

    def reload_graph(self) -> None:
        if self.graph_view:
            self.graph_view.dispose()
        self.graph = None
        self.layout = None
        self.graph_view = None
        self.canvas._mission_graph = False  # type: ignore[attr-defined]
        for sequence in (
            "<Configure>",
            "<ButtonPress-1>",
            "<B1-Motion>",
            "<ButtonRelease-1>",
            "<Motion>",
            "<MouseWheel>",
            "<Button-4>",
            "<Button-5>",
            "<KeyPress-0>",
            "<KeyPress-plus>",
            "<KeyPress-equal>",
            "<KeyPress-minus>",
        ):
            self.canvas.unbind(sequence)
        self.canvas.delete("all")
        for button in self._graph_buttons:
            button.configure(state="disabled")
        if not graphviz_available():
            self._canvas_message(
                "Visualization paused",
                "Graphviz is not available.\n\n"
                "Install the Python graphviz package and put dot on PATH to draw "
                "diagrams. The state and transition tables below remain complete.",
            )
            return
        try:
            graph, layout = build_map_graph(
                self.app.scan.repository if self.app.scan else Path("."),
                self.machine,
                self.map_data["id"],
                rankdir=self.app.direction,
            )
            self.graph = graph
            self.layout = layout
            self.graph_view = GraphView(self.canvas, self, layout)
            if self._selected:
                kind, item_id = self._selected
                if (kind == "node" and item_id in layout.nodes) or (
                    kind == "edge" and item_id in layout.edges
                ):
                    self.graph_view.set_selected(kind, item_id)
            self.canvas._mission_graph = True  # type: ignore[attr-defined]
            for button in self._graph_buttons:
                button.configure(state="normal")
        except GraphvizUnavailable as exc:
            self._canvas_message("Visualization paused", str(exc))
        except Exception as exc:
            self._canvas_message("Could not draw this map", str(exc))

    def _canvas_message(self, title: str, message: str) -> None:
        self.canvas.create_text(
            24,
            22,
            anchor="nw",
            text=title,
            fill=CYAN,
            font=("Segoe UI Semibold", 13),
        )
        self.canvas.create_text(
            24,
            54,
            anchor="nw",
            text=message,
            fill=MUTED,
            width=max(340, self.canvas.winfo_width() - 48),
            font=FONT_SMALL,
            justify="left",
        )

    def fit_graph(self) -> None:
        if self.graph_view:
            self.graph_view.fit()

    def destroy(self) -> None:
        if self.graph_view:
            self.graph_view.dispose()
        super().destroy()

    def _zoom(self, factor: float) -> None:
        if self.graph_view:
            self.graph_view.zoom(factor)

    def _state_selected(self, _event: tk.Event[Any]) -> None:
        if self._syncing:
            return
        selection = self.state_tree.selection()
        if selection and self._selected != ("node", selection[0]):
            self.app.select_node(self, selection[0])

    def _edge_selected(self, _event: tk.Event[Any]) -> None:
        if self._syncing:
            return
        selection = self.edge_tree.selection()
        if selection and self._selected != ("edge", selection[0]):
            self.app.select_edge(self, selection[0])

    def select_in_view(self, kind: str, item_id: str) -> None:
        self._selected = (kind, item_id)
        self._syncing = True
        try:
            if kind == "node":
                self.tabs.set(self.states_tab)
                self.state_tree.selection_set(item_id)
                self.state_tree.focus(item_id)
                self.state_tree.see(item_id)
                other = self.edge_tree.selection()
                if other:
                    self.edge_tree.selection_remove(*other)
            elif kind == "edge":
                self.tabs.set(self.transitions_tab)
                self.edge_tree.selection_set(item_id)
                self.edge_tree.focus(item_id)
                self.edge_tree.see(item_id)
                other = self.state_tree.selection()
                if other:
                    self.state_tree.selection_remove(*other)
        finally:
            self._syncing = False
        self.configure(border_color=CYAN)
        if self.graph_view:
            self.graph_view.set_selected(kind, item_id)

    def clear_selection(self) -> None:
        if self._selected is None:
            return
        self._selected = None
        self._syncing = True
        try:
            for tree in (self.state_tree, self.edge_tree):
                selected = tree.selection()
                if selected:
                    tree.selection_remove(*selected)
        finally:
            self._syncing = False
        self.configure(border_color=LINE)
        if self.graph_view:
            self.graph_view.set_selected(None, None)

    def export_graph(self, format_: str) -> None:
        if self.graph is None:
            messagebox.showwarning(
                "Nothing to export",
                "This map has not been rendered yet.",
                parent=self.app.root,
            )
            return
        suffix = "." + format_
        path = filedialog.asksaveasfilename(
            parent=self.app.root,
            title=f"Export {format_.upper()}",
            initialfile=short_map_name(self.map_data).replace(".", "-") + suffix,
            initialdir=str(Path.home()),
            defaultextension=suffix,
            filetypes=[(f"{format_.upper()} files", f"*{suffix}"), ("All files", "*")],
        )
        if not path:
            return
        output = Path(path).resolve()
        if self.app.scan and output.is_relative_to(self.app.scan.root):
            messagebox.showerror(
                "Choose another location",
                "Export outside the scanned repository to keep the checkout unchanged.",
                parent=self.app.root,
            )
            return
        try:
            output.write_bytes(render_graph_bytes(self.graph, format_))
        except (OSError, GraphvizUnavailable) as exc:
            messagebox.showerror("Export failed", str(exc), parent=self.app.root)
            return
        messagebox.showinfo("Export complete", f"Wrote\n{output}", parent=self.app.root)


class MissionMapApp:
    """Main mission-control application."""

    def __init__(self, root: ctk.CTk, repository: Path | None = None, focus: str = ""):
        ctk.set_appearance_mode("dark")
        self.root = root
        self.default_repository = (repository or default_root()).expanduser().resolve()
        self.scan: ScanState | None = None
        self.scanning = False
        self.cards: list[MapCard] = []
        self.nav_buttons: dict[int, ctk.CTkButton] = {}
        self.direction = "TB"
        self._updating_focus = False
        self._active_nav: int | None = None
        self._build_style()
        self._build_window(focus)
        self.root.after(180, self.start_scan)

    def _build_style(self) -> None:
        self.root.configure(fg_color=BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=TEXT,
            borderwidth=0,
            relief="flat",
            rowheight=30,
            font=("Segoe UI", 10),
        )
        style.map(
            "Treeview",
            background=[("selected", CYAN_SOFT)],
            foreground=[("selected", CYAN)],
        )
        style.configure(
            "Treeview.Heading",
            background=CARD_DEEP,
            foreground=MUTED,
            borderwidth=0,
            relief="flat",
            padding=(8, 8),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "Treeview.Heading",
            background=[("active", CARD_SOFT)],
            foreground=[("active", TEXT)],
        )
        style.configure("TScrollbar", background=CARD, troughcolor=CARD_DEEP, borderwidth=0)

    def _build_window(self, focus: str) -> None:
        self.initial_focus = focus
        self.root.title("Mission Map Visualizer · Michigan RoboSub")
        # CustomTkinter applies the OS display scale to geometry requests. Size
        # the shell against the usable screen so it never opens beyond the
        # desktop on high-DPI laptops or projectors.
        try:
            window_scale = ctk.ScalingTracker.get_window_scaling(self.root) or 1.0
        except Exception:
            window_scale = 1.0
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = int(min(1460, max(1160, (screen_w - 80) / window_scale)))
        height = int(min(900, max(700, (screen_h - 90) / window_scale)))
        x = max(0, int((screen_w - width * window_scale) / 2))
        y = max(0, int((screen_h - height * window_scale) / 2))
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(1150, width), min(700, height))
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self.root, fg_color=HEADER, corner_radius=0, height=76)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)

        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="w", padx=22, pady=16)
        logo = ctk.CTkLabel(
            brand,
            text="M",
            width=42,
            height=42,
            fg_color=MAIZE,
            text_color=HEADER,
            corner_radius=12,
            font=("Segoe UI Semibold", 19),
        )
        logo.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        ctk.CTkLabel(
            brand,
            text="MICHIGAN ROBOSUB",
            text_color=TEXT,
            font=("Segoe UI Semibold", 14),
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(
            brand,
            text="MISSION CONTROL",
            text_color=CYAN,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        ).grid(row=1, column=1, sticky="w", pady=(1, 0))

        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=2, sticky="e", padx=22, pady=18)
        status = ctk.CTkFrame(
            right,
            fg_color=CARD_DEEP,
            corner_radius=18,
            border_width=1,
            border_color=LINE,
        )
        status.pack(side="left")
        ctk.CTkLabel(
            status,
            text="●",
            text_color=GREEN,
            font=("Segoe UI", 13),
            width=16,
        ).pack(side="left", padx=(11, 5), pady=4)
        ctk.CTkLabel(
            status,
            text="STATIC ANALYSIS",
            text_color=MUTED,
            font=("Segoe UI Semibold", 8),
        ).pack(side="left", pady=4)
        ctk.CTkLabel(
            status,
            text="READ-ONLY",
            text_color=CYAN,
            font=("Segoe UI Semibold", 8),
        ).pack(side="left", padx=(10, 11), pady=4)

        body = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew", padx=20, pady=18)
        body.grid_columnconfigure(0, minsize=240)
        body.grid_columnconfigure(1, weight=1, minsize=520)
        body.grid_columnconfigure(2, minsize=310)
        body.grid_rowconfigure(0, weight=1)

        self._build_sidebar(body)
        self._build_center(body)
        self.inspector = InspectorPanel(body)
        self.inspector.grid(row=0, column=2, sticky="nsew")
        self.root.bind("<Control-r>", lambda _event: self.start_scan())
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def _build_sidebar(self, parent: ctk.CTkFrame) -> None:
        sidebar = ctk.CTkFrame(
            parent,
            fg_color=SIDEBAR,
            corner_radius=18,
            border_width=1,
            border_color=LINE,
        )
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(
            sidebar,
            text="WORKSPACE",
            text_color=DIM,
            font=("Segoe UI Semibold", 8),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))

        summary = ctk.CTkFrame(
            sidebar,
            fg_color=CARD_DEEP,
            corner_radius=14,
            border_width=1,
            border_color=LINE_SOFT,
        )
        summary.grid(row=1, column=0, sticky="ew", padx=12)
        summary.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            summary,
            text="SCAN OVERVIEW",
            text_color=MUTED,
            font=("Segoe UI Semibold", 8),
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(11, 0))

        count_row = ctk.CTkFrame(summary, fg_color="transparent")
        count_row.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=(3, 0))
        self.summary_maps_value = ctk.CTkLabel(
            count_row,
            text="—",
            text_color=TEXT,
            font=("Segoe UI Semibold", 27),
            anchor="w",
            height=34,
        )
        self.summary_maps_value.pack(side="left")
        ctk.CTkLabel(
            count_row,
            text="mission maps",
            text_color=MUTED,
            font=FONT_SMALL,
            anchor="w",
        ).pack(side="left", padx=(8, 0), pady=(8, 0))

        self.summary_path = ctk.CTkLabel(
            summary,
            text="No repository scanned yet.",
            text_color=DIM,
            font=("Segoe UI", 8),
            anchor="w",
            justify="left",
            wraplength=210,
        )
        self.summary_path.grid(row=2, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 6))

        divider(summary, color=LINE_SOFT, padx=(14, 14), pady=(0, 8), grid_row=3)

        stats = ctk.CTkFrame(summary, fg_color="transparent")
        stats.grid(row=4, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 10))
        self.summary_files_value = ctk.CTkLabel(
            stats,
            text="—",
            text_color=TEXT,
            font=("Segoe UI Semibold", 15),
            anchor="w",
        )
        self.summary_files_value.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            stats,
            text="PY FILES",
            text_color=DIM,
            font=("Segoe UI Semibold", 7),
            anchor="w",
        ).grid(row=0, column=1, sticky="w", padx=(5, 0), pady=(3, 0))
        self.summary_warnings_value = ctk.CTkLabel(
            stats,
            text="—",
            text_color=AMBER,
            font=("Segoe UI Semibold", 15),
            anchor="w",
        )
        self.summary_warnings_value.grid(row=0, column=2, sticky="w", padx=(16, 0))
        ctk.CTkLabel(
            stats,
            text="WARNINGS",
            text_color=DIM,
            font=("Segoe UI Semibold", 7),
            anchor="w",
        ).grid(row=0, column=3, sticky="w", padx=(5, 0), pady=(3, 0))

        nav_head = ctk.CTkFrame(sidebar, fg_color="transparent")
        nav_head.grid(row=2, column=0, sticky="ew", padx=18, pady=(13, 6))
        nav_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            nav_head,
            text="MISSION MAPS",
            text_color=MUTED,
            font=("Segoe UI Semibold", 8),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.nav_count_label = ctk.CTkLabel(
            nav_head,
            text="—",
            text_color=CYAN,
            font=("Segoe UI Semibold", 9),
            anchor="e",
        )
        self.nav_count_label.grid(row=0, column=1, sticky="e")

        self.nav_scroll = MissionScroll(
            sidebar,
            fg_color=SIDEBAR,
            scrollbar_fg_color=SIDEBAR,
            height=140,
        )
        self.nav_scroll.grid(row=3, column=0, sticky="nsew", padx=8)

        legend = ctk.CTkFrame(
            sidebar,
            fg_color=CARD_DEEP,
            corner_radius=14,
            border_width=1,
            border_color=LINE_SOFT,
        )
        legend.grid(row=4, column=0, sticky="ew", padx=12, pady=(14, 12))
        ctk.CTkLabel(
            legend,
            text="MAP KEYS",
            text_color=MUTED,
            font=("Segoe UI Semibold", 8),
            anchor="w",
        ).pack(fill="x", padx=13, pady=(11, 5))
        legend_items = (
            ("●", GREEN, "Configured start"),
            ("◎", RED, "Configured stop"),
            ("◌", CYAN, "Inferred entry"),
            ("◌", AMBER, "Inferred terminal"),
            ("- -", RED, "Soft stop"),
            ("····", DIM, "Declared only"),
        )
        grid = ctk.CTkFrame(legend, fg_color="transparent")
        grid.pack(fill="x", padx=8, pady=(0, 2))
        for index, (symbol, color, text) in enumerate(legend_items):
            row = ctk.CTkFrame(grid, fg_color="transparent")
            row.grid(row=index // 3, column=index % 3, sticky="w", padx=1, pady=1)
            ctk.CTkLabel(
                row,
                text=symbol,
                text_color=color,
                font=("Segoe UI Semibold", 9),
                width=16,
                anchor="w",
            ).pack(side="left")
            ctk.CTkLabel(
                row,
                text=text,
                text_color=MUTED,
                font=("Segoe UI", 7),
                anchor="w",
            ).pack(side="left")
        ctk.CTkLabel(
            legend,
            text="STATIC VIEW",
            text_color=DIM,
            font=("Segoe UI Semibold", 7),
            anchor="w",
        ).pack(fill="x", padx=13, pady=(4, 9))

    def _build_center(self, parent: ctk.CTkFrame) -> None:
        center = ctk.CTkFrame(parent, fg_color="transparent")
        center.grid(row=0, column=1, sticky="nsew", padx=18)
        center.grid_columnconfigure(0, weight=1)
        center.grid_rowconfigure(2, weight=1)

        hero = ctk.CTkFrame(center, fg_color="transparent")
        hero.grid(row=0, column=0, sticky="ew")
        hero.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hero,
            text="FLEET / MISSION PLANNER",
            text_color=CYAN,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            hero,
            text="Mission maps",
            text_color=TEXT,
            font=FONT_DISPLAY,
            anchor="w",
            height=38,
        ).grid(row=1, column=0, sticky="w", pady=(1, 0))
        ctk.CTkLabel(
            hero,
            text="Explore every state and transition without starting ROS or the vehicle.",
            text_color=MUTED,
            font=FONT_SMALL,
            anchor="w",
        ).grid(row=2, column=0, sticky="w")

        status_row = ctk.CTkFrame(hero, fg_color="transparent")
        status_row.grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.status_dot = ctk.CTkLabel(
            status_row,
            text="●",
            text_color=GREEN,
            font=("Segoe UI", 11),
            width=14,
        )
        self.status_dot.pack(side="left")
        self.status_label = ctk.CTkLabel(
            status_row,
            text="Ready to scan the repository.",
            text_color=MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=470,
        )
        self.status_label.pack(side="left", padx=(3, 10))
        self.progress = ctk.CTkProgressBar(
            status_row,
            width=110,
            height=4,
            corner_radius=2,
            fg_color=LINE_SOFT,
            progress_color=CYAN,
            mode="indeterminate",
        )
        self.progress.pack(side="left", pady=(2, 0))
        self.progress.pack_forget()

        self.hero_count = Pill(
            hero,
            "— MAPS",
            fg_color=CARD,
            text_color=CYAN,
            font=("Segoe UI Semibold", 8),
            border_width=1,
            border_color=LINE,
        )
        self.hero_count.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(12, 0))

        controls = ctk.CTkFrame(
            center,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=LINE,
        )
        controls.grid(row=1, column=0, sticky="ew", pady=(13, 12))
        controls.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            controls,
            text="REPOSITORY",
            text_color=MUTED,
            font=("Segoe UI Semibold", 8),
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(15, 8), pady=(11, 0))
        self.repo_var = tk.StringVar(value=str(self.default_repository))
        self.repo_entry = ctk.CTkEntry(
            controls,
            textvariable=self.repo_var,
            height=36,
            fg_color=CARD_DEEP,
            border_color=LINE,
            border_width=1,
            text_color=TEXT,
            placeholder_text_color=DIM,
            font=FONT_SMALL,
            corner_radius=9,
        )
        self.repo_entry.grid(row=1, column=0, sticky="ew", padx=(15, 8), pady=(4, 0))
        self.repo_entry.bind("<Return>", lambda _event: self.start_scan())

        self.browse_btn = ctk.CTkButton(
            controls,
            text="Browse",
            width=82,
            height=36,
            fg_color=CARD_SOFT,
            hover_color=NAV_HOVER,
            text_color=TEXT,
            font=FONT_SMALL,
            corner_radius=9,
            command=self.browse_repository,
        )
        self.browse_btn.grid(row=1, column=1, sticky="e", padx=(0, 7), pady=(4, 0))
        self.scan_btn = ctk.CTkButton(
            controls,
            text="Scan repository",
            width=144,
            height=36,
            fg_color=CYAN,
            hover_color="#8BF3FF",
            text_color="#06202B",
            font=("Segoe UI Semibold", 10),
            corner_radius=9,
            command=self.start_scan,
        )
        self.scan_btn.grid(row=1, column=2, sticky="e", padx=(0, 15), pady=(4, 0))

        divider(controls, color=LINE_SOFT, padx=(15, 15), pady=(11, 0), grid_row=2)

        filters = ctk.CTkFrame(controls, fg_color="transparent")
        filters.grid(row=3, column=0, columnspan=3, sticky="ew", padx=15, pady=(10, 12))
        filters.grid_columnconfigure(0, weight=1)

        focus_field = ctk.CTkFrame(filters, fg_color="transparent")
        focus_field.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        focus_field.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            focus_field,
            text="FOCUS",
            text_color=MUTED,
            font=("Segoe UI Semibold", 8),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.focus_var = tk.StringVar(value=self.initial_focus or FOCUS_ALL)
        self.focus_combo = ctk.CTkComboBox(
            focus_field,
            values=(FOCUS_ALL,),
            variable=self.focus_var,
            height=34,
            fg_color=CARD_DEEP,
            border_color=LINE,
            border_width=1,
            button_color=CARD_SOFT,
            button_hover_color=NAV_HOVER,
            dropdown_fg_color=CARD,
            dropdown_hover_color=NAV_SELECTED,
            dropdown_text_color=TEXT,
            text_color=TEXT,
            font=FONT_SMALL,
            corner_radius=9,
            command=self._focus_changed,
        )
        self.focus_combo.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        direction_field = ctk.CTkFrame(filters, fg_color="transparent")
        direction_field.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            direction_field,
            text="LAYOUT DIRECTION",
            text_color=MUTED,
            font=("Segoe UI Semibold", 8),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.direction_var = tk.StringVar(value="Top to bottom")
        self.direction_combo = ctk.CTkComboBox(
            direction_field,
            values=tuple(DIRECTIONS),
            variable=self.direction_var,
            width=164,
            height=34,
            fg_color=CARD_DEEP,
            border_color=LINE,
            border_width=1,
            button_color=CARD_SOFT,
            button_hover_color=NAV_HOVER,
            dropdown_fg_color=CARD,
            dropdown_hover_color=NAV_SELECTED,
            dropdown_text_color=TEXT,
            text_color=TEXT,
            font=FONT_SMALL,
            corner_radius=9,
            command=self._direction_changed,
        )
        self.direction_combo.grid(row=1, column=0, sticky="e", pady=(4, 0))

        self.map_scroll = MissionScroll(
            center,
            fg_color=BG,
            scrollbar_fg_color=BG,
            scrollbar_button_color=SCROLL,
            scrollbar_button_hover_color=CYAN,
        )
        self.map_scroll.grid(row=2, column=0, sticky="nsew")
        self.notices_holder = ctk.CTkFrame(self.map_scroll, fg_color=BG)
        self.notices_holder.pack(fill="x")
        self.cards_holder = ctk.CTkFrame(self.map_scroll, fg_color=BG)
        self.cards_holder.pack(fill="x")

    def _focus_value(self) -> str:
        value = self.focus_combo.get().strip()
        return "" if value == FOCUS_ALL else value

    def _focus_changed(self, _value: str | None = None) -> None:
        if not self._updating_focus:
            self.start_scan()

    def _direction_changed(self, _value: str | None = None) -> None:
        self.direction = DIRECTIONS.get(self.direction_combo.get(), "TB")
        if self.scan and not self.scanning:
            self._set_status("Re-laying out all maps…", "working")
            self.root.update_idletasks()
            for card in self.cards:
                card.reload_graph()
            self._set_status(
                f"Re-laid out {len(self.cards)} map{'s' if len(self.cards) != 1 else ''}.",
                "normal",
            )

    def browse_repository(self) -> None:
        path = filedialog.askdirectory(
            parent=self.root,
            title="Choose repository",
            initialdir=str(self.default_repository),
        )
        if path:
            self.repo_var.set(path)
            self.start_scan()

    def start_scan(self) -> None:
        if self.scanning:
            return
        text = self.repo_var.get().strip()
        if not text:
            self._set_status("Choose a repository path first.", "error")
            messagebox.showerror(
                "Repository required",
                "Choose a repository path first.",
                parent=self.root,
            )
            self.repo_entry.focus_set()
            return
        root = Path(text).expanduser()
        if not root.is_dir():
            self._set_status(f"Repository directory does not exist: {root}", "error")
            messagebox.showerror(
                "Cannot scan repository",
                f"Directory does not exist:\n{root}",
                parent=self.root,
            )
            return
        focus = self._focus_value()
        self.scanning = True
        self._set_controls(False)
        self.scan_btn.configure(text="Scanning…")
        self._set_status(f"Scanning {root.name} …", "working")
        self.root.update_idletasks()
        threading.Thread(
            target=self._scan_worker,
            args=(root, focus),
            daemon=True,
        ).start()

    def _scan_worker(self, root: Path, focus: str) -> None:
        try:
            result = discover(root, focus)
        except Exception as exc:
            self.root.after(0, lambda message=str(exc): self._scan_failed(message))
            return
        self.root.after(0, lambda value=result: self._scan_finished(value))

    def _scan_finished(self, result: ScanState) -> None:
        self.scan = result
        self.scanning = False
        self._set_controls(True)
        self.scan_btn.configure(text="Scan repository")
        self._populate_focus(result)
        self._render_results(result)
        warning_count = len(result.warnings)
        files_scanned = result.payload["files_scanned"]
        self._set_status(
            f"{files_scanned} file{'s' if files_scanned != 1 else ''} scanned  ·  "
            f"{result.payload['map_count']} map"
            f"{'s' if result.payload['map_count'] != 1 else ''}  ·  "
            f"{warning_count} warning{'s' if warning_count != 1 else ''}",
            "normal",
        )

    def _scan_failed(self, message: str) -> None:
        self.scanning = False
        self._set_controls(True)
        self.scan_btn.configure(text="Scan repository")
        self._set_status(message, "error")
        messagebox.showerror("Scan failed", message, parent=self.root)

    def _set_controls(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        combo_state = "readonly" if enabled else "disabled"
        for widget in (self.repo_entry, self.browse_btn, self.scan_btn):
            widget.configure(state=state)
        self.focus_combo.configure(state=combo_state)
        self.direction_combo.configure(state=combo_state)

    def _set_status(self, text: str, kind: str = "normal") -> None:
        colors = {"normal": GREEN, "working": CYAN, "error": RED}
        self.status_dot.configure(text_color=colors.get(kind, GREEN))
        self.status_label.configure(
            text=text,
            text_color=RED if kind == "error" else MUTED,
        )
        if kind == "working":
            self.progress.pack(side="left", pady=(2, 0))
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.progress.set(0)
        self.root.update_idletasks()

    def _populate_focus(self, result: ScanState) -> None:
        files = list(result.payload.get("files", []))
        self._updating_focus = True
        try:
            self.focus_combo.configure(values=(FOCUS_ALL, *files))
            self.focus_combo.set(result.focus or FOCUS_ALL)
        finally:
            self._updating_focus = False

    def _render_results(self, result: ScanState) -> None:
        for card in self.cards:
            card.destroy()
        self.cards = []
        self.nav_buttons = {}
        for child in self.notices_holder.winfo_children():
            child.destroy()
        for child in self.cards_holder.winfo_children():
            child.destroy()
        self._render_summary(result)
        self._render_notices(result)
        if result.payload["maps"]:
            self._set_status("Rendering map layouts…", "working")
            self.root.update_idletasks()
            for map_data, machine in zip(result.payload["maps"], result.machines):
                card = MapCard(self.cards_holder, self, map_data, machine)
                card.pack(fill="x", pady=(0, 14))
                self.cards.append(card)
        else:
            self._render_empty(result)
        self._render_nav(result)
        self.map_scroll._parent_canvas.yview_moveto(0)
        self.inspector.show_prompt()

    def _render_summary(self, result: ScanState) -> None:
        payload = result.payload
        count = payload["map_count"]
        total = payload["total_map_count"]
        map_text = f"{count} / {total}" if payload["focus"] else str(count)
        self.summary_maps_value.configure(text=map_text)
        self.summary_files_value.configure(text=str(payload["files_scanned"]))
        warnings = len(payload["warnings"])
        self.summary_warnings_value.configure(
            text=str(warnings),
            text_color=AMBER if warnings else GREEN,
        )
        self.summary_path.configure(
            text=f"{Path(payload['root']).name}\n{_shorten(payload['root'], 38)}"
        )
        self.nav_count_label.configure(text=str(count))
        self.hero_count.set_text(f"{count} MAP{'S' if count != 1 else ''}")

    def _render_notices(self, result: ScanState) -> None:
        payload = result.payload
        warnings = list(payload.get("warnings", []))
        unvisualized = list(payload.get("unvisualized", []))
        graphviz_missing = not payload.get("graphviz")
        has_issue = bool(warnings or unvisualized or graphviz_missing)
        box = ctk.CTkFrame(
            self.notices_holder,
            fg_color=AMBER_SOFT if has_issue else CARD_DEEP,
            corner_radius=14,
            border_width=1,
            border_color="#6A5220" if has_issue else LINE_SOFT,
        )
        box.pack(fill="x", pady=(0, 14))
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(11, 0))
        ctk.CTkLabel(
            head,
            text="!  ANALYSIS" if has_issue else "✓  ANALYSIS",
            text_color=AMBER if has_issue else GREEN,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        ).pack(side="left")

        lines: list[str] = []
        if graphviz_missing:
            lines.append(
                "Graphviz is unavailable. State and transition tables remain usable; "
                "install the graphviz package and put dot on PATH to draw maps."
            )
        for warning in warnings:
            lines.append(warning)
        if unvisualized:
            lines.append("Not fully visualized:")
            lines.extend(f"· {item}" for item in unvisualized)
        if not lines:
            lines.append("No analysis warnings. Static scan completed cleanly.")
        ctk.CTkLabel(
            box,
            text="\n".join(lines),
            text_color=AMBER if has_issue else MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=460,
        ).pack(fill="x", padx=14, pady=(6, 8))
        ctk.CTkLabel(
            box,
            text=payload.get("note", ""),
            text_color=DIM,
            font=FONT_MICRO,
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 11))

    def _render_empty(self, result: ScanState) -> None:
        empty = ctk.CTkFrame(
            self.cards_holder,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=LINE,
        )
        empty.pack(fill="x")
        ctk.CTkLabel(
            empty,
            text="No mission maps found",
            text_color=TEXT,
            font=("Segoe UI Semibold", 20),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(20, 0))
        message = (
            "This scanner only shows statically resolvable mission transition maps. "
            "Dynamic maps and unsupported constructs are listed in the analysis notices above."
        )
        if result.payload.get("focus"):
            message = (
                f"No statically resolvable mission maps matched {result.payload['focus']!r}. "
                "Choose another focus or show all maps."
            )
        ctk.CTkLabel(
            empty,
            text=message,
            text_color=MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=460,
        ).pack(fill="x", padx=20, pady=(7, 14))
        if result.payload.get("focus"):
            ctk.CTkButton(
                empty,
                text="Show all mission maps",
                width=164,
                height=34,
                fg_color=CYAN,
                hover_color="#8BF3FF",
                text_color="#06202B",
                font=("Segoe UI Semibold", 10),
                corner_radius=9,
                command=self._show_all,
            ).pack(anchor="w", padx=20, pady=(0, 20))
        else:
            ctk.CTkFrame(empty, height=10, fg_color="transparent").pack()

    def _show_all(self) -> None:
        self._updating_focus = True
        try:
            self.focus_combo.set(FOCUS_ALL)
        finally:
            self._updating_focus = False
        self.start_scan()

    def _render_nav(self, result: ScanState) -> None:
        for child in self.nav_scroll.winfo_children():
            child.destroy()
        self.nav_buttons = {}
        self._active_nav = None
        groups: dict[str, list[dict[str, Any]]] = {}
        for map_data in result.payload["maps"]:
            key = map_data.get("file") or map_data.get("module") or "(unknown file)"
            groups.setdefault(key, []).append(map_data)
        if not groups:
            ctk.CTkLabel(
                self.nav_scroll,
                text="No maps in this scan.",
                text_color=DIM,
                font=FONT_SMALL,
                anchor="w",
            ).pack(fill="x", padx=10, pady=8)
            return
        first: int | None = None
        for file, maps in groups.items():
            ctk.CTkLabel(
                self.nav_scroll,
                text=_shorten(file, 35),
                text_color=DIM,
                font=("Segoe UI", 8),
                anchor="w",
                wraplength=215,
                justify="left",
            ).pack(fill="x", padx=10, pady=(8, 3))
            for map_data in maps:
                index = int(map_data["id"])
                button = ctk.CTkButton(
                    self.nav_scroll,
                    text=f"{index + 1:02d}   {_shorten(short_map_name(map_data), 31)}",
                    anchor="w",
                    height=36,
                    fg_color="transparent",
                    hover_color=NAV_HOVER,
                    text_color=MUTED,
                    font=FONT_SMALL,
                    corner_radius=9,
                    command=lambda value=index: self._nav_click(value),
                )
                button.pack(fill="x", padx=4, pady=2)
                self.nav_buttons[index] = button
                if first is None:
                    first = index
        if first is not None:
            self._set_nav_active(first)

    def _set_nav_active(self, index: int) -> None:
        self._active_nav = index
        for number, button in self.nav_buttons.items():
            if number == index:
                button.configure(
                    fg_color=NAV_SELECTED,
                    hover_color=NAV_SELECTED,
                    text_color=CYAN,
                    border_width=1,
                    border_color=CYAN_DARK,
                )
            else:
                button.configure(
                    fg_color="transparent",
                    hover_color=NAV_HOVER,
                    text_color=MUTED,
                    border_width=0,
                )

    def _nav_click(self, index: int) -> None:
        self._set_nav_active(index)
        self.scroll_to_card(index)

    def scroll_to_card(self, index: int) -> None:
        if not (0 <= index < len(self.cards)):
            return
        self.map_scroll.update_idletasks()
        card = self.cards[index]
        offset = card.winfo_y() + self.cards_holder.winfo_y()
        total = max(self.map_scroll.winfo_height(), 1)
        self.map_scroll._parent_canvas.yview_moveto(
            max(0.0, min(1.0, offset / total))
        )

    def select_node(self, card: MapCard, node_id: str) -> None:
        node = next((item for item in card.map_data["nodes"] if item["id"] == node_id), None)
        if node is None:
            return
        for other in self.cards:
            if other is not card:
                other.clear_selection()
        self.inspector.show_node(card.map_data, node)
        card.select_in_view("node", node_id)

    def select_edge(self, card: MapCard, edge_id: str) -> None:
        edge = next((item for item in card.map_data["edges"] if item["id"] == edge_id), None)
        if edge is None:
            return
        for other in self.cards:
            if other is not card:
                other.clear_selection()
        self.inspector.show_edge(card.map_data, edge)
        card.select_in_view("edge", edge_id)


def launch_gui(repository: Path | None = None, focus: str = "") -> int:
    """Open the mission-control window and block until it is closed."""

    # Keep the three-column layout inside the physical desktop bounds. CTk's
    # automatic DPI scaling can otherwise turn a 1200px request into 1800px on
    # a projector or high-DPI laptop. This must be set before creating the root.
    ctk.deactivate_automatic_dpi_awareness()
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    app = MissionMapApp(root, repository=repository, focus=focus)
    root.mainloop()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Static mission-map visualizer with a CustomTkinter desktop window.",
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
    return launch_gui(root, focus=args.focus)


if __name__ == "__main__":
    raise SystemExit(main())
