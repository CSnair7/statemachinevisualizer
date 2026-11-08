#!/usr/bin/env python3
"""Render a mrobosub TransitionMap as a Graphviz state diagram.

Usage:
    python state_viz.py path/to/mission.py -o diagram --format svg
    python state_viz.py path/to/mission.py --mock geometry_msgs --mock nav_msgs

Requires:  pip install graphviz   and the Graphviz binaries (apt install graphviz).
The mission file's imports (mrobosub_planning...) must be importable, e.g. run
from the workspace or set PYTHONPATH.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import graphviz

ROS_PACKAGES = {
    "rclpy": ["rclpy", "rclpy.node", "rclpy.publisher", "rclpy.executors"],
    "std_msgs": ["std_msgs", "std_msgs.msg"],
    "std_srvs": ["std_srvs", "std_srvs.srv"],
}


def mock_missing_ros(extra: list[str]) -> None:
    """Stub out ROS packages that aren't installed so the state files import off-sub."""
    for top, modules in ROS_PACKAGES.items():
        if importlib.util.find_spec(top) is None:
            for m in modules:
                sys.modules[m] = MagicMock()
    for m in extra:
        sys.modules[m] = MagicMock()


def load_transitions(path: Path, var: str) -> dict:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return getattr(module, var)


def params_of(state: type) -> dict[str, Any]:
    return dict(getattr(state, "_param_overrides", {}))


def declared_outcomes(state: type, outcome_base: type) -> list[type]:
    """Outcomes defined on a state (and its parents). Uses vars() to avoid the
    warning StateMeta.__getattribute__ emits on cross-state access."""
    found: list[type] = []
    for klass in state.__mro__:
        for v in vars(klass).values():
            if (
                isinstance(v, type)
                and issubclass(v, outcome_base)
                and v is not outcome_base
                and v not in found
            ):
                found.append(v)
    return found


def build_graph(transitions: dict, title: str, fmt: str) -> graphviz.Digraph:
    from mrobosub_planning.umrsm import Outcome

    edges: list[tuple[str, str, str]] = []
    sources: set[str] = set()
    targets: set[str] = set()
    base_states: dict[str, type] = {}

    for outcome, target in transitions.items():
        src_cls = getattr(outcome, "_state", None)
        if src_cls is None:
            print(f"warning: {outcome} has no owning state, skipping", file=sys.stderr)
            continue
        src, dst = src_cls.__qualname__, target.__qualname__
        label = outcome.__name__
        overrides = params_of(target)
        if overrides:
            label += "\\n[" + ", ".join(f"{k}={v}" for k, v in overrides.items()) + "]"
        edges.append((src, dst, label))
        sources.add(src)
        targets.add(dst)
        base_states[src] = src_cls

    nodes = sources | targets
    start_nodes = sources - targets      # never entered -> entry point(s)
    terminal_nodes = targets - sources   # never left -> end point(s)

    # Lint: outcomes a non-terminal state can return but the map doesn't handle.
    # run_once() would raise KeyError on these.
    for name, cls in base_states.items():
        for oc in declared_outcomes(cls, Outcome):
            if oc not in transitions:
                print(f"warning: {name}.{oc.__name__} is declared but not in the "
                      f"transition map", file=sys.stderr)

    g = graphviz.Digraph(title, format=fmt)
    g.attr(rankdir="TB", fontname="Helvetica", label=title, labelloc="t")
    g.attr("node", shape="box", style="rounded", fontname="Helvetica")
    g.attr("edge", fontname="Helvetica", fontsize="10")

    for n in sorted(nodes):
        if n in start_nodes:
            g.node(n, style="rounded,filled", fillcolor="lightgreen")
        elif n in terminal_nodes:
            g.node(n, peripheries="2", style="rounded,filled", fillcolor="lightcoral")
        else:
            g.node(n)

    for src, dst, label in edges:
        g.edge(src, dst, label=label)
    return g


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file", type=Path, help="mission file containing a TransitionMap")
    ap.add_argument("-o", "--output", default=None, help="output path without extension")
    ap.add_argument("--format", default="svg", help="svg, png, pdf, ... (default svg)")
    ap.add_argument("--var", default="transitions", help="name of the TransitionMap variable")
    ap.add_argument("--mock", action="append", default=[], help="extra module to stub (repeatable)")
    args = ap.parse_args()

    mock_missing_ros(args.mock)
    transitions = load_transitions(args.file, args.var)
    graph = build_graph(transitions, args.file.stem, args.format)
    out = args.output or args.file.stem
    graph.render(out, cleanup=True)
    print(f"wrote {out}.{args.format}")


if __name__ == "__main__":
    main()