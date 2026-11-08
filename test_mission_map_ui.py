"""Focused tests for the desktop mission map visualizer.

Run with: python -m unittest -v test_mission_map_ui
"""
from __future__ import annotations

import hashlib
import tempfile
import textwrap
import unittest
from pathlib import Path

from mission_map_ui import (
    build_map_graph,
    discover,
    graphviz_available,
    parse_graph_layout,
    render_graph_bytes,
)


class MissionMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.actual = Path(__file__).parent / "mrobosub"

    def write(self, name: str, source: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")

    def fixture(self) -> None:
        self.write("robot/robot/__init__.py", "")
        self.write(
            "robot/robot/framework.py",
            """
            class Outcome: pass
            class State: pass
            """,
        )
        self.write(
            "robot/robot/mission.py",
            """
            from .framework import Outcome, State

            class Start(State):
                class Complete(Outcome): pass

            class Dive(State):
                class Done(Outcome): pass

            class End(State): pass

            transitions = {Start.Complete: Dive, Dive.Done: End}
            """,
        )

    def test_actual_mrobosub_maps_and_metadata(self):
        if not self.actual.is_dir():
            self.skipTest("mrobosub checkout not present")
        scan = discover(self.actual)
        self.assertEqual(scan.payload["map_count"], 2)
        self.assertEqual(scan.payload["total_map_count"], 2)
        standard = next(m for m in scan.payload["maps"] if m["name"].endswith("standard_run.transitions"))
        testing = next(m for m in scan.payload["maps"] if m["name"].endswith("testing_run.transitions"))

        self.assertEqual(standard["stats"], {"states": 3, "transitions": 4, "soft_stops": 2})
        self.assertEqual(standard["configured_stops"], ["Stop"])
        self.assertTrue(any("declared outgoing transition" in w for w in standard["warnings"]))
        surfaced = next(edge for edge in standard["edges"] if edge["outcome"] == "Surfaced")
        self.assertTrue(surfaced["declared_only"])
        self.assertIn("declared only", surfaced["graph_label"])
        self.assertEqual(surfaced["target_name"], "mrobosub_planning.common_states.Stop")

        self.assertEqual(testing["stats"], {"states": 5, "transitions": 7, "soft_stops": 0})
        self.assertEqual(
            {node["short"] for node in testing["nodes"]},
            {"Start", "Stop", "Submerge", "ComeToSurface", "Forward5Seconds"},
        )
        self.assertEqual(
            {edge["outcome"] for edge in testing["edges"]},
            {"Complete", "Submerged", "TimedOut", "Reached", "Unreached", "Surfaced", "Failed"},
        )
        self.assertTrue(all(node["id"].startswith(f"map{testing['id']}_node") for node in testing["nodes"]))
        self.assertTrue(all(edge["id"].startswith(f"map{testing['id']}_edge") for edge in testing["edges"]))
        self.assertEqual(testing["note"], "Static possibilities; not a runtime reachability proof.")
        self.assertTrue(all(edge["source"].startswith(f"map{testing['id']}_node") for edge in testing["edges"]))
        self.assertTrue(all(edge["target"].startswith(f"map{testing['id']}_node") for edge in testing["edges"]))
        self.assertTrue(all(edge["source_location"]["file"] for edge in testing["edges"]))
        self.assertIn("STOP", next(node["graph_label"] for node in standard["nodes"] if node["short"] == "Stop"))

    def test_focus_filter_keeps_all_file_choices(self):
        if not self.actual.is_dir():
            self.skipTest("mrobosub checkout not present")
        filtered = discover(self.actual, "testing_run.py")
        self.assertEqual(filtered.payload["map_count"], 1)
        self.assertEqual(filtered.payload["total_map_count"], 2)
        self.assertEqual(len(filtered.payload["files"]), 2)
        self.assertTrue(filtered.payload["maps"][0]["name"].endswith("testing_run.transitions"))

        missing = discover(self.actual, "does-not-exist")
        self.assertEqual(missing.payload["map_count"], 0)
        self.assertEqual(missing.payload["total_map_count"], 2)

    def test_graphviz_layout_matches_payload(self):
        if not self.actual.is_dir():
            self.skipTest("mrobosub checkout not present")
        if not graphviz_available():
            self.skipTest("Graphviz dot not installed")
        scan = discover(self.actual)
        for map_data, machine in zip(scan.payload["maps"], scan.machines):
            graph, layout = build_map_graph(scan.repository, machine, map_data["id"])
            self.assertEqual(set(layout.nodes), {node["id"] for node in map_data["nodes"]})
            self.assertEqual(set(layout.edges), {edge["id"] for edge in map_data["edges"]})
            self.assertGreater(layout.width, 0)
            self.assertGreater(layout.height, 0)
            for edge in layout.edges.values():
                self.assertGreaterEqual(len(edge.points), 2)
                self.assertTrue(edge.arrow)
            svg = render_graph_bytes(graph, "svg")
            self.assertIn(b"<svg", svg)
            self.assertIn(map_data["nodes"][0]["id"].encode(), svg)

    def test_parse_layout_keeps_edge_identity_and_arrow(self):
        data = {
            "bb": "0,0,100,80",
            "objects": [
                {"_gvid": 0, "name": "a", "pos": "20,60", "width": "1", "height": "0.5"},
                {"_gvid": 1, "name": "b", "pos": "20,10", "width": "1", "height": "0.5"},
            ],
            "edges": [
                {
                    "_gvid": 0,
                    "id": "map0_edge0",
                    "tail": 0,
                    "head": 1,
                    "pos": "e,20,30 20,60 20,45 20,30",
                    "lp": "35,45",
                    "_hdraw_": [{"op": "P", "points": [[23,30], [20,20], [17,30]]}],
                }
            ],
        }
        layout = parse_graph_layout(data)
        self.assertEqual(layout.width, 100)
        self.assertEqual(layout.height, 80)
        self.assertIn("map0_edge0", layout.edges)
        edge = layout.edges["map0_edge0"]
        self.assertEqual((edge.tail, edge.head), ("a", "b"))
        self.assertEqual(edge.label_pos, (35.0, 45.0))
        self.assertEqual(edge.arrow, [(23.0, 30.0), (20.0, 20.0), (17.0, 30.0)])

    def test_scan_is_static_and_read_only(self):
        self.fixture()
        self.write("danger.py", "raise RuntimeError('must never execute')\n")
        before = {
            path: hashlib.sha256(path.read_bytes()).digest()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        scan = discover(self.root)
        self.assertEqual(scan.payload["map_count"], 1)
        if graphviz_available():
            build_map_graph(scan.repository, scan.machines[0], 0)
        after = {
            path: hashlib.sha256(path.read_bytes()).digest()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_dynamic_map_is_reported_as_not_visualized(self):
        self.fixture()
        self.write("dynamic.py", "transitions: TransitionMap = make_map()\n")
        scan = discover(self.root)
        self.assertTrue(any("no statically resolvable" in item for item in scan.payload["unvisualized"]))

    def test_invalid_repository_is_rejected(self):
        with self.assertRaises(ValueError):
            discover(self.root / "missing")


if __name__ == "__main__":
    unittest.main()
