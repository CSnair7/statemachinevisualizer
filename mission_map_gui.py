"""Tkinter desktop window for the mission-map visualizer.

Run through ``python mission_map_ui.py`` or directly with
``python mission_map_gui.py``. The window is intentionally self-contained:
there is no HTTP server, web build, or network listener.
"""

from __future__ import annotations

import math
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from mission_map_ui import (
    GraphvizUnavailable,
    Machine,
    MapLayout,
    build_map_graph,
    default_root,
    discover,
    graphviz_available,
    render_graph_bytes,
)

BG = "#eef2f7"
SURFACE = "#ffffff"
SOFT = "#f8fafc"
TEXT = "#0f172a"
MUTED = "#64748b"
LINE = "#dbe3ee"
ACCENT = "#0f766e"
ACCENT_DARK = "#115e59"
GREEN = "#166534"
RED = "#991b1b"
CYAN = "#0e7490"
AMBER = "#b45309"
WARN_BG = "#fffbeb"
WARN_FG = "#92400e"
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
    flags: list[str] = []
    if edge.get("soft_stop"):
        flags.append("soft stop")
    elif edge.get("declared_only"):
        flags.append("declared only")
    else:
        flags.append("declared transition")
    return flags


def _descendant(widget: tk.Misc, ancestor: tk.Misc) -> bool:
    current: tk.Misc | None = widget
    while current is not None:
        if current is ancestor:
            return True
        current = current.master
    return False


class InspectorPanel(ttk.Frame):
    """Fixed right-hand details panel for the current state or transition."""

    def __init__(self, parent: tk.Misc):
        super().__init__(parent, style="Card.TFrame", padding=(14, 14))
        self.columnconfigure(0, weight=1)

        self.title_var = tk.StringVar(value="Inspector")
        self.context_var = tk.StringVar(value="Select a state or transition")
        ttk.Label(self, text="INSPECTOR", style="Eyebrow.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(self, textvariable=self.title_var, style="CardTitle.TLabel").grid(
            row=1, column=0, sticky="w", pady=(2, 0)
        )
        ttk.Label(
            self, textvariable=self.context_var, style="Muted.TLabel", wraplength=280
        ).grid(row=2, column=0, sticky="new", pady=(2, 8))

        text_frame = ttk.Frame(self, style="Card.TFrame")
        text_frame.grid(row=3, column=0, sticky="nsew")
        self.rowconfigure(3, weight=1)
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.text = tk.Text(
            text_frame,
            width=30,
            height=18,
            wrap="word",
            bd=0,
            highlightthickness=0,
            padx=2,
            pady=2,
            bg=SURFACE,
            fg=TEXT,
            font=("Segoe UI", 10),
            cursor="arrow",
        )
        scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.tag_configure("eyebrow", foreground=ACCENT, font=("Segoe UI", 8, "bold"))
        self.text.tag_configure("title", foreground=TEXT, font=("Segoe UI", 14, "bold"))
        self.text.tag_configure("heading", foreground=MUTED, font=("Segoe UI", 8, "bold"))
        self.text.tag_configure("code", foreground=TEXT, font=("Consolas", 9))
        self.text.tag_configure("muted", foreground=MUTED, font=("Segoe UI", 9))
        self.text.tag_configure("warn", foreground=WARN_FG, font=("Segoe UI", 9))
        self.text.configure(state="disabled")
        self.show_prompt()

    def _reset(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")

    def _finish(self) -> None:
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def _write(self, value: str, tag: str | None = None) -> None:
        self.text.insert("end", value, tag or ())

    def show_prompt(self) -> None:
        self.title_var.set("Inspector")
        self.context_var.set("Select a state or transition")
        self._reset()
        self._write("Select a node or edge in a map, or choose a row from its tables.", "muted")
        self._finish()

    def show_node(self, map_data: dict[str, Any], node: dict[str, Any]) -> None:
        self.title_var.set(node["short"])
        self.context_var.set(short_map_name(map_data))
        self._reset()
        self._write("STATE\n", "eyebrow")
        self._write(node["name"] + "\n", "title")
        self._write("  ·  ".join(node_flags(node)) + "\n", "warn" if node.get("inferred_entry") else "muted")
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
                    "code",
                )
        else:
            self._write("None\n", "muted")
        self._section("Incoming transitions")
        incoming = [edges[edge_id] for edge_id in node.get("incoming", []) if edge_id in edges]
        if incoming:
            for edge in incoming:
                self._write(
                    f"{short_node_name(map_data, edge['source'])}  →  {edge['outcome']}\n",
                    "code",
                )
        else:
            self._write("None\n", "muted")
        self._finish()

    def show_edge(self, map_data: dict[str, Any], edge: dict[str, Any]) -> None:
        self.title_var.set(edge["outcome"])
        self.context_var.set(short_map_name(map_data))
        self._reset()
        self._write("TRANSITION\n", "eyebrow")
        self._write(edge["outcome"] + "\n", "title")
        self._write("  ·  ".join(edge_flags(edge)) + "\n", "warn" if edge.get("soft_stop") else "muted")
        self._section("Route")
        self._write(
            f"{short_node_name(map_data, edge['source'])}  →  "
            f"{short_node_name(map_data, edge['target'])}\n",
            "code",
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

    def _section(self, title: str) -> None:
        self._write("\n" + title.upper() + "\n", "heading")


class GraphView:
    """Pan/zoom canvas renderer for one Graphviz map layout."""

    MARGIN = 28

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
        ):
            canvas.unbind(sequence)
        canvas.configure(cursor="fleur", takefocus=1)
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
            0, 0, width, height, fill=SOFT, outline="", tags=("graph-background",)
        )
        for x in range(0, width, 28):
            self.canvas.create_line(x, 0, x, height, fill="#eef3f8", tags=("graph-grid",))
        for y in range(0, height, 28):
            self.canvas.create_line(0, y, width, y, fill="#eef3f8", tags=("graph-grid",))

        for edge in self.layout.edges.values():
            self._draw_edge(edge)
        for node in self.layout.nodes.values():
            self._draw_node(node)

    def _draw_edge(self, edge: Any) -> None:
        meta = self.edge_map.get(edge.id, {})
        soft = bool(meta.get("soft_stop"))
        declared = bool(meta.get("declared_only"))
        selected = self.selected == ("edge", edge.id)
        color = RED if soft else ("#94a3b8" if declared else "#64748b")
        label_color = RED if soft else ("#64748b" if declared else "#334155")
        width = 1.8 if soft else 1.3
        dash: tuple[int, int] | None = (6, 4) if soft else ((2, 3) if declared else None)
        if selected:
            color = ACCENT
            label_color = ACCENT_DARK
            width += 1.3
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
                    font=("Segoe UI", max(7, int(8 * self.scale))),
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
            x2, y2, left[0], left[1], right[0], right[1],
            fill=color, outline=color, tags=tags,
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
        if selected:
            outline = ACCENT
            line_width = max(3, line_width)
        tags = (f"node:{node.id}", "node")
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
                left + inset, top + inset, right - inset, bottom - inset,
                fill="", outline=outline, width=max(1, line_width - 1), tags=tags,
            )
        label = meta.get("graph_label") or meta.get("short") or node.id
        self.canvas.create_text(
            center_x,
            center_y,
            text=label,
            fill=TEXT,
            width=max(40, int(width - 10)),
            justify="center",
            font=("Segoe UI", max(7, int(9 * self.scale))),
            tags=tags,
        )

    def _node_style(self, meta: dict[str, Any]) -> tuple[str, str, tuple[int, int] | None, bool]:
        if meta.get("configured_start") and meta.get("configured_stop"):
            return "#fef9c3", "#a16207", None, True
        if meta.get("configured_start"):
            return "#dcfce7", GREEN, None, False
        if meta.get("configured_stop"):
            return "#fee2e2", RED, None, True
        if meta.get("inferred_entry"):
            return "#ecfeff", CYAN, (5, 3), False
        if meta.get("inferred_terminal"):
            return "#fffbeb", AMBER, (5, 3), False
        return SURFACE, MUTED, None, False

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
        for item in self.canvas.find_overlapping(event.x - 2, event.y - 2, event.x + 2, event.y + 2):
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
        for item in self.canvas.find_overlapping(event.x - 2, event.y - 2, event.x + 2, event.y + 2):
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


class MapCard(ttk.Frame):
    """One scrollable result card for a single mission map."""

    def __init__(
        self,
        parent: tk.Misc,
        app: "MissionMapApp",
        map_data: dict[str, Any],
        machine: Machine,
    ):
        super().__init__(parent, style="Card.TFrame", padding=(16, 14))
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
        head = ttk.Frame(self, style="Card.TFrame")
        head.pack(fill="x")
        head.columnconfigure(0, weight=1)
        title_wrap = ttk.Frame(head, style="Card.TFrame")
        title_wrap.grid(row=0, column=0, sticky="w")
        ttk.Label(
            title_wrap,
            text=self.map_data["name"],
            style="CardTitle.TLabel",
            wraplength=560,
        ).pack(anchor="w")
        ttk.Label(
            title_wrap,
            text=self.map_data.get("source") or self.map_data.get("file", ""),
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        stats = ttk.Frame(head, style="Card.TFrame")
        stats.grid(row=0, column=1, sticky="e")
        map_stats = self.map_data["stats"]
        chips = [
            f"{map_stats['states']} states",
            f"{map_stats['transitions']} transitions",
        ]
        if map_stats.get("soft_stops"):
            chips.append(
                f"{map_stats['soft_stops']} soft stop"
                + ("s" if map_stats["soft_stops"] != 1 else "")
            )
        for value in chips:
            ttk.Label(stats, text=value, style="Chip.TLabel").pack(side="left", padx=(4, 0))
        if self.map_data.get("configured_starts"):
            ttk.Label(
                stats,
                text="start: " + ", ".join(self.map_data["configured_starts"]),
                style="ChipStart.TLabel",
            ).pack(side="left", padx=(4, 0))
        if self.map_data.get("configured_stops"):
            ttk.Label(
                stats,
                text="stop: " + ", ".join(self.map_data["configured_stops"]),
                style="ChipStop.TLabel",
            ).pack(side="left", padx=(4, 0))

        if self.map_data.get("warnings"):
            ttk.Label(
                self,
                text="\n".join(self.map_data["warnings"]),
                style="Warning.TLabel",
                wraplength=560,
                justify="left",
            ).pack(fill="x", pady=(10, 0))

        toolbar = ttk.Frame(self, style="Card.TFrame")
        toolbar.pack(fill="x", pady=(10, 6))
        ttk.Label(
            toolbar,
            text="Drag to pan · wheel to zoom · click a node or edge to inspect",
            style="Muted.TLabel",
        ).pack(side="left")
        ttk.Button(toolbar, text="Fit map", command=self.fit_graph).pack(side="right")
        self.zoom_in_btn = ttk.Button(toolbar, text="+", width=3, command=lambda: self._zoom(1.2))
        self.zoom_in_btn.pack(side="right", padx=(4, 0))
        self.zoom_out_btn = ttk.Button(toolbar, text="−", width=3, command=lambda: self._zoom(0.83))
        self.zoom_out_btn.pack(side="right", padx=(4, 0))
        self.svg_btn = ttk.Button(toolbar, text="SVG", command=lambda: self.export_graph("svg"))
        self.svg_btn.pack(side="right", padx=(8, 0))
        self.png_btn = ttk.Button(toolbar, text="PNG", command=lambda: self.export_graph("png"))
        self.png_btn.pack(side="right", padx=(4, 0))

        self.canvas = tk.Canvas(
            self,
            height=400,
            bg=SOFT,
            bd=0,
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=LINE,
        )
        self.canvas.pack(fill="x")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="x", pady=(12, 0))
        self.state_tree = self._build_table(
            self.notebook,
            tab_title=f"States ({len(self.map_data['nodes'])})",
            columns=("state", "params", "flags"),
            headings=("State", "Parameters", "Flags"),
            widths=(130, 220, 160),
            height=min(max(len(self.map_data["nodes"]), 3), 7),
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
            self.notebook,
            tab_title=f"Transitions ({len(self.map_data['edges'])})",
            columns=("from", "outcome", "to", "flags"),
            headings=("From", "Outcome", "To", "Flags"),
            widths=(105, 205, 105, 145),
            height=min(max(len(self.map_data["edges"]), 3), 7),
        )
        for edge in self.map_data["edges"]:
            tags = ("soft",) if edge.get("soft_stop") else (("declared",) if edge.get("declared_only") else ())
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
        self.edge_tree.tag_configure("declared", foreground="#64748b")
        self.edge_tree.bind("<<TreeviewSelect>>", self._edge_selected)

        if self.map_data.get("warnings"):
            warning_tab = ttk.Frame(self.notebook, padding=(10, 8))
            self.notebook.add(warning_tab, text=f"Warnings ({len(self.map_data['warnings'])})")
            ttk.Label(
                warning_tab,
                text="\n".join(self.map_data["warnings"]),
                style="Warning.TLabel",
                wraplength=520,
                justify="left",
            ).pack(fill="x")
        ttk.Label(
            self,
            text="Static possibilities, not a runtime reachability proof.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    def _build_table(
        self,
        notebook: ttk.Notebook,
        tab_title: str,
        columns: tuple[str, ...],
        headings: tuple[str, ...],
        widths: tuple[int, ...],
        height: int,
    ) -> ttk.Treeview:
        tab = ttk.Frame(notebook, padding=(6, 6))
        notebook.add(tab, text=tab_title)
        tab.columnconfigure(0, weight=1)
        tree = ttk.Treeview(
            tab,
            columns=columns,
            show="headings",
            height=height,
            selectmode="browse",
        )
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=90, stretch=True)
        scroll = ttk.Scrollbar(tab, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(tab, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
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
        for button in (self.svg_btn, self.png_btn, self.zoom_in_btn, self.zoom_out_btn):
            button.configure(state="disabled")
        if not graphviz_available():
            self._canvas_message(
                "Graphviz is not available.\n\n"
                "Install the Python graphviz package and put dot on PATH to draw diagrams. "
                "The state and transition tables below remain complete."
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
            for button in (self.svg_btn, self.png_btn, self.zoom_in_btn, self.zoom_out_btn):
                button.configure(state="normal")
        except GraphvizUnavailable as exc:
            self._canvas_message(str(exc))
        except Exception as exc:
            self._canvas_message(f"Could not draw this map:\n\n{exc}")

    def _canvas_message(self, message: str) -> None:
        self.canvas.create_text(
            22,
            20,
            anchor="nw",
            text=message,
            fill=MUTED,
            width=max(320, self.canvas.winfo_width() - 44),
            font=("Segoe UI", 10),
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
                self.notebook.select(0)
                self.state_tree.selection_set(item_id)
                self.state_tree.focus(item_id)
                self.state_tree.see(item_id)
                other = self.edge_tree.selection()
                if other:
                    self.edge_tree.selection_remove(*other)
            elif kind == "edge":
                self.notebook.select(1)
                self.edge_tree.selection_set(item_id)
                self.edge_tree.focus(item_id)
                self.edge_tree.see(item_id)
                other = self.state_tree.selection()
                if other:
                    self.state_tree.selection_remove(*other)
        finally:
            self._syncing = False
        if self.graph_view:
            self.graph_view.set_selected(kind, item_id)

    def export_graph(self, format_: str) -> None:
        if self.graph is None:
            messagebox.showwarning(
                "Nothing to export", "This map has not been rendered yet.", parent=self.app.root
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
    """Main Tkinter application."""

    def __init__(self, root: tk.Tk, repository: Path | None = None, focus: str = ""):
        self.root = root
        self.default_repository = (repository or default_root()).expanduser().resolve()
        self.scan: Any = None
        self.scanning = False
        self.cards: list[MapCard] = []
        self.nav_index: dict[str, int] = {}
        self.direction = "TB"
        self._updating_focus = False
        self._updating_nav = False
        self._build_style()
        self._build_window(focus)
        self.root.after(120, self.start_scan)

    def _build_style(self) -> None:
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Segoe UI", 10), background=BG, foreground=TEXT)
        style.configure("Header.TFrame", background=SURFACE)
        style.configure("App.TFrame", background=BG)
        style.configure("App.TLabel", background=BG, foreground=TEXT)
        style.configure("SummaryMuted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Stat.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 11, "bold"))
        style.configure("Card.TFrame", background=SURFACE, relief="solid", borderwidth=1)
        style.configure("Notice.TFrame", background=SURFACE, relief="solid", borderwidth=1)
        style.configure("Title.TLabel", background=SURFACE, foreground=TEXT, font=("Segoe UI", 17, "bold"))
        style.configure("Subtitle.TLabel", background=SURFACE, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=SURFACE, foreground=MUTED, font=("Segoe UI", 8, "bold"))
        style.configure("CardTitle.TLabel", background=SURFACE, foreground=TEXT, font=("Segoe UI", 13, "bold"))
        style.configure("Card.TLabel", background=SURFACE, foreground=TEXT)
        style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Eyebrow.TLabel", background=SURFACE, foreground=ACCENT, font=("Segoe UI", 8, "bold"))
        style.configure("Chip.TLabel", background="#f1f5f9", foreground="#475569", padding=(8, 3), font=("Segoe UI", 8))
        style.configure("ChipStart.TLabel", background="#dcfce7", foreground=GREEN, padding=(8, 3), font=("Segoe UI", 8))
        style.configure("ChipStop.TLabel", background="#fee2e2", foreground=RED, padding=(8, 3), font=("Segoe UI", 8))
        style.configure("Warning.TLabel", background=WARN_BG, foreground=WARN_FG, padding=(9, 6), font=("Segoe UI", 9))
        style.configure("Status.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Error.TLabel", background=BG, foreground=RED, font=("Segoe UI", 9, "bold"))
        style.configure("Working.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 9, "bold"))
        style.configure("TButton", padding=(11, 6))
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", padding=(14, 7))
        style.map("Accent.TButton", background=[("active", ACCENT_DARK)])
        style.configure("Treeview", rowheight=25, background=SURFACE, fieldbackground=SURFACE, foreground=TEXT)
        style.configure("Treeview.Heading", background="#f1f5f9", foreground="#475569", font=("Segoe UI", 9, "bold"))
        style.configure("TNotebook", background=SURFACE)
        style.configure("TNotebook.Tab", padding=(12, 6))
        style.configure("TEntry", fieldbackground=SURFACE, padding=(6, 5))
        style.configure("TCombobox", fieldbackground=SURFACE, padding=(5, 4))

    def _build_window(self, focus: str) -> None:
        self.root.title("Mission Map Visualizer")
        self.root.geometry("1450x900")
        self.root.minsize(1120, 700)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, style="Header.TFrame", padding=(18, 13, 18, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        brand = ttk.Frame(header, style="Header.TFrame")
        brand.grid(row=0, column=0, sticky="w")
        ttk.Label(brand, text="Mission Map Visualizer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            brand,
            text="Read-only static analysis  ·  not a runtime reachability proof",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        controls = ttk.Frame(header, style="Header.TFrame")
        controls.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        controls.grid_columnconfigure(0, weight=1)

        repo_field = ttk.Frame(controls, style="Header.TFrame")
        repo_field.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        repo_field.grid_columnconfigure(0, weight=1)
        ttk.Label(repo_field, text="REPOSITORY", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        self.repo_var = tk.StringVar(value=str(self.default_repository))
        self.repo_entry = ttk.Entry(repo_field, textvariable=self.repo_var)
        self.repo_entry.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.repo_entry.bind("<Return>", lambda _event: self.start_scan())

        self.browse_btn = ttk.Button(controls, text="Browse…", command=self.browse_repository)
        self.browse_btn.grid(row=0, column=1, rowspan=2, sticky="s", padx=(0, 6), pady=(0, 0))
        self.scan_btn = ttk.Button(controls, text="Scan repository", style="Accent.TButton", command=self.start_scan)
        self.scan_btn.grid(row=0, column=2, rowspan=2, sticky="s")

        focus_field = ttk.Frame(controls, style="Header.TFrame")
        focus_field.grid(row=0, column=3, sticky="ew", padx=(18, 8), pady=(0, 0))
        ttk.Label(focus_field, text="FOCUS", style="Header.TLabel").pack(anchor="w")
        self.focus_var = tk.StringVar(value=focus or FOCUS_ALL)
        self.focus_combo = ttk.Combobox(
            focus_field, textvariable=self.focus_var, state="readonly", width=24
        )
        self.focus_combo.pack(pady=(3, 0))
        self.focus_combo.bind("<<ComboboxSelected>>", self._focus_changed)

        direction_field = ttk.Frame(controls, style="Header.TFrame")
        direction_field.grid(row=0, column=4, sticky="ew", pady=(0, 0))
        ttk.Label(direction_field, text="DIRECTION", style="Header.TLabel").pack(anchor="w")
        self.direction_var = tk.StringVar(value="Top to bottom")
        self.direction_combo = ttk.Combobox(
            direction_field,
            textvariable=self.direction_var,
            values=tuple(DIRECTIONS),
            state="readonly",
            width=15,
        )
        self.direction_combo.pack(pady=(3, 0))
        self.direction_combo.bind("<<ComboboxSelected>>", self._direction_changed)

        self.status_var = tk.StringVar(value="Choose a repository and scan.")
        self.status_label = ttk.Label(
            self.root, textvariable=self.status_var, style="Status.TLabel", padding=(18, 8)
        )
        self.status_label.grid(row=1, column=0, sticky="ew")

        body = ttk.Frame(self.root, style="App.TFrame", padding=(16, 2, 16, 16))
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, minsize=270)
        body.grid_columnconfigure(1, weight=1)
        body.grid_columnconfigure(2, minsize=330)

        self._build_sidebar(body)
        self._build_center(body)
        self.inspector = InspectorPanel(body)
        self.inspector.grid(row=0, column=2, sticky="nsew", padx=(16, 0))
        self.root.bind_all("<MouseWheel>", self._on_outer_wheel, add="+")
        self.root.bind_all("<Button-4>", self._on_outer_wheel, add="+")
        self.root.bind_all("<Button-5>", self._on_outer_wheel, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def _build_sidebar(self, parent: ttk.Frame) -> None:
        sidebar = ttk.Frame(parent, style="App.TFrame", width=270)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(1, weight=1)

        summary = ttk.LabelFrame(sidebar, text="Scan summary", padding=(10, 8))
        summary.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        summary.grid_columnconfigure(1, weight=1)
        self.summary_maps = tk.StringVar(value="—")
        self.summary_files = tk.StringVar(value="—")
        self.summary_warnings = tk.StringVar(value="—")
        rows = (
            ("Mission maps", self.summary_maps),
            ("Python files", self.summary_files),
            ("Warnings", self.summary_warnings),
        )
        for number, (label, variable) in enumerate(rows):
            ttk.Label(summary, text=label, style="App.TLabel").grid(
                row=number, column=0, sticky="w", pady=2
            )
            ttk.Label(summary, textvariable=variable, style="Stat.TLabel").grid(
                row=number, column=1, sticky="e", pady=2
            )
        self.summary_note = tk.StringVar(value="No repository scanned yet.")
        ttk.Label(
            summary,
            textvariable=self.summary_note,
            style="SummaryMuted.TLabel",
            wraplength=230,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(7, 0))

        nav_frame = ttk.LabelFrame(sidebar, text="Mission maps", padding=(7, 7))
        nav_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 14))
        nav_frame.grid_rowconfigure(0, weight=1)
        nav_frame.grid_columnconfigure(0, weight=1)
        self.nav_tree = ttk.Treeview(nav_frame, show="tree", selectmode="browse", height=12)
        nav_scroll = ttk.Scrollbar(nav_frame, orient="vertical", command=self.nav_tree.yview)
        self.nav_tree.configure(yscrollcommand=nav_scroll.set)
        self.nav_tree.grid(row=0, column=0, sticky="nsew")
        nav_scroll.grid(row=0, column=1, sticky="ns")
        self.nav_tree.bind("<<TreeviewSelect>>", self._nav_selected)

        legend = ttk.LabelFrame(sidebar, text="Legend", padding=(10, 8))
        legend.grid(row=2, column=0, sticky="ew")
        legend_items = (
            ("●", GREEN, "Configured start"),
            ("◎", RED, "Configured stop"),
            ("◌", CYAN, "Inferred entry"),
            ("◌", AMBER, "Inferred terminal"),
            ("- -", RED, "Soft stop"),
            ("····", MUTED, "Declared only"),
        )
        for number, (symbol, color, text) in enumerate(legend_items):
            ttk.Label(
                legend,
                text=symbol,
                foreground=color,
                background=BG,
                font=("Segoe UI", 12, "bold"),
                width=3,
            ).grid(row=number, column=0, sticky="w")
            ttk.Label(legend, text=text, style="App.TLabel").grid(
                row=number, column=1, sticky="w", pady=1
            )

    def _build_center(self, parent: ttk.Frame) -> None:
        center = ttk.Frame(parent, style="App.TFrame")
        center.grid(row=0, column=1, sticky="nsew")
        center.grid_rowconfigure(0, weight=1)
        center.grid_columnconfigure(0, weight=1)
        self.map_canvas = tk.Canvas(center, bg=BG, highlightthickness=0, yscrollincrement=24)
        self.map_scroll = ttk.Scrollbar(center, orient="vertical", command=self.map_canvas.yview)
        self.map_canvas.configure(yscrollcommand=self.map_scroll.set)
        self.map_canvas.grid(row=0, column=0, sticky="nsew")
        self.map_scroll.grid(row=0, column=1, sticky="ns")
        self.inner = ttk.Frame(self.map_canvas, style="App.TFrame")
        self.inner_window = self.map_canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._inner_configured)
        self.map_canvas.bind("<Configure>", self._canvas_configured)
        self.notices_holder = ttk.Frame(self.inner, style="App.TFrame")
        self.notices_holder.pack(fill="x")
        self.cards_holder = ttk.Frame(self.inner, style="App.TFrame")
        self.cards_holder.pack(fill="x")

    def _inner_configured(self, _event: tk.Event[Any]) -> None:
        self.map_canvas.configure(scrollregion=self.map_canvas.bbox("all"))

    def _canvas_configured(self, event: tk.Event[Any]) -> None:
        self.map_canvas.itemconfigure(self.inner_window, width=max(event.width, 200))
        self.map_canvas.after_idle(
            lambda: self.map_canvas.configure(scrollregion=self.map_canvas.bbox("all"))
        )

    def _on_outer_wheel(self, event: tk.Event[Any]) -> str | None:
        if not _descendant(event.widget, self.map_canvas):
            return None
        if getattr(event.widget, "_mission_graph", False):
            return None
        if getattr(event, "num", None) in (4, 5):
            step = -1 if event.num == 4 else 1
        else:
            step = -1 if event.delta > 0 else 1
        self.map_canvas.yview_scroll(step, "units")
        return "break"

    def browse_repository(self) -> None:
        path = filedialog.askdirectory(
            parent=self.root,
            title="Choose repository",
            initialdir=str(self.default_repository),
        )
        if path:
            self.repo_var.set(path)
            self.start_scan()

    def _focus_value(self) -> str:
        value = self.focus_var.get()
        return "" if value == FOCUS_ALL else value

    def _focus_changed(self, _event: tk.Event[Any]) -> None:
        if not self._updating_focus:
            self.start_scan()

    def _direction_changed(self, _event: tk.Event[Any]) -> None:
        self.direction = DIRECTIONS.get(self.direction_var.get(), "TB")
        if self.scan and not self.scanning:
            self._set_status("Re-laying out all maps…", "working")
            self.root.update_idletasks()
            for card in self.cards:
                card.reload_graph()
            self._set_status(
                f"Re-laid out {len(self.cards)} map{'s' if len(self.cards) != 1 else ''}.",
                "normal",
            )

    def start_scan(self) -> None:
        if self.scanning:
            return
        text = self.repo_var.get().strip()
        if not text:
            messagebox.showerror("Repository required", "Choose a repository path first.", parent=self.root)
            self.repo_entry.focus_set()
            return
        root = Path(text).expanduser()
        if not root.is_dir():
            self._set_status(f"Repository directory does not exist: {root}", "error")
            messagebox.showerror("Cannot scan repository", f"Directory does not exist:\n{root}", parent=self.root)
            return
        focus = self._focus_value()
        self.scanning = True
        self._set_controls(False)
        self.scan_btn.configure(text="Scanning…")
        self._set_status(f"Scanning {root} …", "working")
        self.root.update_idletasks()
        import threading

        threading.Thread(
            target=self._scan_worker, args=(root, focus), daemon=True
        ).start()

    def _scan_worker(self, root: Path, focus: str) -> None:
        try:
            result = discover(root, focus)
        except Exception as exc:
            self.root.after(0, lambda: self._scan_failed(str(exc)))
            return
        self.root.after(0, lambda: self._scan_finished(result))

    def _scan_finished(self, result: Any) -> None:
        self.scan = result
        self.scanning = False
        self._set_controls(True)
        self.scan_btn.configure(text="Scan repository")
        self._populate_focus(result)
        self._render_results(result)
        warning_count = len(result.warnings)
        self._set_status(
            f"Scanned {result.payload['files_scanned']} Python files; "
            f"found {result.payload['map_count']} mission map"
            f"{'s' if result.payload['map_count'] != 1 else ''}; "
            f"{warning_count} warning{'s' if warning_count != 1 else ''}.",
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
        readonly = "readonly" if enabled else "disabled"
        for widget in (self.repo_entry, self.browse_btn, self.scan_btn):
            widget.configure(state=state)
        self.focus_combo.configure(state=readonly)
        self.direction_combo.configure(state=readonly)

    def _set_status(self, text: str, kind: str = "normal") -> None:
        self.status_var.set(text)
        self.status_label.configure(style={
            "error": "Error.TLabel",
            "working": "Working.TLabel",
        }.get(kind, "Status.TLabel"))
        self.root.update_idletasks()

    def _populate_focus(self, result: Any) -> None:
        files = list(result.payload.get("files", []))
        self._updating_focus = True
        try:
            self.focus_combo.configure(values=(FOCUS_ALL, *files))
            self.focus_var.set(result.focus or FOCUS_ALL)
        finally:
            self._updating_focus = False

    def _render_results(self, result: Any) -> None:
        for card in self.cards:
            card.destroy()
        self.cards = []
        for child in self.notices_holder.winfo_children():
            child.destroy()
        for child in self.cards_holder.winfo_children():
            child.destroy()
        self._render_summary(result)
        self._render_notices(result)
        if not result.payload["maps"]:
            self._render_empty(result)
        else:
            for map_data, machine in zip(result.payload["maps"], result.machines):
                card = MapCard(self.cards_holder, self, map_data, machine)
                card.pack(fill="x", pady=(0, 16))
                self.cards.append(card)
        self._render_nav(result)
        self.inner.update_idletasks()
        self.map_canvas.configure(scrollregion=self.map_canvas.bbox("all"))
        self.inspector.show_prompt()

    def _render_summary(self, result: Any) -> None:
        payload = result.payload
        self.summary_maps.set(
            f"{payload['map_count']} / {payload['total_map_count']}"
            if payload["focus"]
            else str(payload["map_count"])
        )
        self.summary_files.set(str(payload["files_scanned"]))
        self.summary_warnings.set(str(len(payload["warnings"])))
        self.summary_note.set(
            f"{Path(payload['root']).name}\n{payload['note']}"
        )

    def _render_notices(self, result: Any) -> None:
        payload = result.payload
        box = ttk.Frame(self.notices_holder, style="Notice.TFrame", padding=(14, 11))
        box.pack(fill="x", pady=(0, 14))
        ttk.Label(box, text="ANALYSIS", style="Eyebrow.TLabel").pack(anchor="w")
        has_notice = False
        if not payload.get("graphviz"):
            has_notice = True
            ttk.Label(
                box,
                text="Graphviz is not available. State and transition tables remain usable; "
                     "install the graphviz package and dot on PATH to draw maps.",
                style="Warning.TLabel",
                wraplength=560,
                justify="left",
            ).pack(fill="x", pady=(6, 0))
        for warning in payload.get("warnings", []):
            has_notice = True
            ttk.Label(
                box,
                text=warning,
                style="Warning.TLabel",
                wraplength=560,
                justify="left",
            ).pack(fill="x", pady=(6, 0))
        if payload.get("unvisualized"):
            has_notice = True
            ttk.Label(
                box,
                text="Not fully visualized:",
                style="Muted.TLabel",
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", pady=(9, 2))
            for item in payload["unvisualized"]:
                ttk.Label(
                    box,
                    text=item,
                    style="Muted.TLabel",
                    wraplength=560,
                    justify="left",
                ).pack(fill="x", pady=(2, 0))
        if not has_notice:
            ttk.Label(box, text="No analysis warnings.", style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Label(
            box,
            text=payload.get("note", ""),
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    def _render_empty(self, result: Any) -> None:
        empty = ttk.Frame(self.cards_holder, style="Card.TFrame", padding=(24, 22))
        empty.pack(fill="x")
        ttk.Label(empty, text="No mission maps found", style="CardTitle.TLabel").pack(anchor="w")
        message = (
            "This scanner only shows statically resolvable mission transition maps. "
            "Dynamic maps and unsupported constructs are listed in the analysis notices above."
        )
        if result.payload.get("focus"):
            message = (
                f"No statically resolvable mission maps matched {result.payload['focus']!r}. "
                "Choose another focus or show all maps."
            )
        ttk.Label(
            empty, text=message, style="Muted.TLabel", wraplength=560, justify="left"
        ).pack(anchor="w", pady=(6, 12))
        ttk.Button(empty, text="Show all mission maps", command=self._show_all).pack(anchor="w")

    def _show_all(self) -> None:
        self._updating_focus = True
        self.focus_var.set(FOCUS_ALL)
        self._updating_focus = False
        self.start_scan()

    def _render_nav(self, result: Any) -> None:
        self._updating_nav = True
        try:
            for item in self.nav_tree.get_children():
                self.nav_tree.delete(item)
            self.nav_index = {}
            groups: dict[str, list[dict[str, Any]]] = {}
            for map_data in result.payload["maps"]:
                key = map_data.get("file") or map_data.get("module") or "(unknown file)"
                groups.setdefault(key, []).append(map_data)
            first: str | None = None
            for number, (file, maps) in enumerate(groups.items()):
                parent = self.nav_tree.insert("", "end", iid=f"group:{number}", text=file, open=True)
                for map_data in maps:
                    iid = f"map:{map_data['id']}"
                    self.nav_tree.insert(parent, "end", iid=iid, text=short_map_name(map_data))
                    self.nav_index[iid] = map_data["id"]
                    if first is None:
                        first = iid
            if first:
                self.nav_tree.selection_set(first)
                self.nav_tree.focus(first)
        finally:
            self._updating_nav = False

    def _nav_selected(self, _event: tk.Event[Any]) -> None:
        if self._updating_nav:
            return
        selection = self.nav_tree.selection()
        if not selection:
            return
        index = self.nav_index.get(selection[0])
        if index is not None:
            self.scroll_to_card(index)

    def scroll_to_card(self, index: int) -> None:
        if not (0 <= index < len(self.cards)):
            return
        self.map_canvas.update_idletasks()
        card = self.cards[index]
        total = max(self.inner.winfo_height(), 1)
        self.map_canvas.yview_moveto(max(0.0, min(1.0, card.winfo_y() / total)))
        self.root.after_idle(lambda: card.focus_set())

    def select_node(self, card: MapCard, node_id: str) -> None:
        node = next((item for item in card.map_data["nodes"] if item["id"] == node_id), None)
        if node is None:
            return
        self.inspector.show_node(card.map_data, node)
        card.select_in_view("node", node_id)

    def select_edge(self, card: MapCard, edge_id: str) -> None:
        edge = next((item for item in card.map_data["edges"] if item["id"] == edge_id), None)
        if edge is None:
            return
        self.inspector.show_edge(card.map_data, edge)
        card.select_in_view("edge", edge_id)


def launch_gui(repository: Path | None = None, focus: str = "") -> int:
    """Open the desktop window and block until it is closed."""

    root = tk.Tk()
    MissionMapApp(root, repository=repository, focus=focus)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch_gui())
