# Mission Map Visualizer — UI Specification

## Purpose

Provide a local, read-only desktop window that scans a selected checkout of `mrobosub`, discovers each statically understandable mission `TransitionMap`, and renders one navigable state graph per map. Users may select one mission file/map for focused inspection or scan the whole repository and scroll through every discovered mission map.

The visualizer explains declared control-flow possibilities; it does not run a mission, start ROS, or prove that a path is reachable at runtime.

## Repository findings this design accounts for

- `mrobosub` is a ROS 2 workspace with packages for planning, bringup, control, hardware, simulation, perception, localization, messages, and tests. Mission definitions are concentrated in `mrobosub_planning/mrobosub_planning/`.
- A transition is an `Outcome` class mapped to a target `State` class. States can inherit behavior/outcomes, and targets can be parameterized with `State.with_params(...)`.
- The current checkout contains two concrete maps: `standard_run.transitions` and `testing_run.transitions`. The latter has five states and seven declared outcome transitions. `standard_run` has three declared states; its Captain configuration identifies the stop state and the code also declares a transition from `Stop`, though `StateMachine.run()` exits before dispatching outcomes from the configured stop state. `testing_run` is defined but is not currently registered in Captain's `transition_maps`.
- Reusable planning state classes in `abstract_states.py` are templates, not separate mission maps. Other repository state-like constructs (such as enum lifecycle or Boolean component flags) must not be mixed into the mission-map list.
- `state_vis.py` loads and executes a selected Python module and draws a Graphviz graph. The repository also has `repo_state_vis.py`, which statically scans source and already understands several Python map patterns. Prefer its static-analysis approach for repository browsing: target files may import ROS modules or have unrelated module-level side effects.
- Tkinter 8.6 is available with the project's Python. `customtkinter`, Pillow, and CairoSVG are not installed, so the UI uses standard `tkinter`/`ttk` and renders Graphviz's JSON layout directly on a `tk.Canvas`.

## Goals

1. Make it obvious how to choose a repository, a specific mission file/map, or all mission maps.
2. Show each discovered map as a distinct graph with readable states, outcome-labelled edges, and target parameter overrides.
3. Make a repository-wide set of maps easy to browse in one vertically scrollable window, with quick navigation between maps.
4. Clearly distinguish configured entry/stop information from inferred graph structure, and surface incomplete or unsupported analysis.
5. Leave the selected repository unchanged and require no ROS installation, running vehicle, simulator, browser, or HTTP server.

## Out of scope

- Editing mission code or transition maps.
- Running ROS nodes, subscribing to telemetry, simulating state execution, or asserting runtime reachability.
- Treating reusable state templates, arbitrary Boolean flags, or component lifecycle diagrams as missions.
- Automatically changing or generating files inside the scanned checkout.
- Hosting a web page, starting a local socket listener, or requiring a JavaScript/web build toolchain.

## Primary user flow

1. Launch the desktop window. The repository field defaults to the adjacent `mrobosub/` directory when present.
2. Choose **Scan** to discover all mission maps, or choose a Python file in the **Focus** menu. In focused mode, if multiple maps are declared in the file, each gets a map card. A repository-relative file path and map variable are always displayed to remove ambiguity.
3. Watch the status line while scanning runs in a background thread, then read the summary: Python files inspected, mission maps found, warnings, and unsupported/dynamic declarations.
4. Browse map cards in a single vertical canvas. The left navigation list contains one entry per map, grouped by package/file; selecting an entry scrolls to its card. Each card has its own graph viewport so a large diagram can be panned/zoomed without losing the page's vertical scroll position.
5. Click a state or edge in a graph, or choose a row in the card's **States** or **Transitions** table, to inspect its fully qualified source name, source file/line when available, target parameters, outcome, and any known condition. Use **Fit** to reset that card's viewport.

## Main screen layout

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Mission Map Visualizer                              [Repository path …] [Browse] [Scan] │
│ Read-only static analysis · no repository execution · no web server         │
├───────────────────┬──────────────────────────────────────────┬───────────────┤
│ MAPS              │ ANALYSIS                                  │ INSPECTOR     │
│ Planning          │ 1 warning · 2 maps · 100 files            │ Select a     │
│  standard_run     │ ┌──────────────────────────────────────┐ │ state or     │
│  testing_run      │ │ standard_run.transitions             │ │ transition   │
│                   │ │ [graph canvas: pan / zoom / fit]     │ │              │
│ LEGEND            │ │ States | Transitions                 │ │              │
│ start / stop      │ └──────────────────────────────────────┘ │              │
│ soft stop         │ ┌──────────────────────────────────────┐ │              │
│                   │ │ testing_run.transitions              │ │              │
│                   │ │ [graph canvas: pan / zoom / fit]     │ │              │
│                   │ └──────────────────────────────────────┘ │              │
└───────────────────┴──────────────────────────────────────────┴───────────────┘
```

The window opens at 1450×900 with a 1120×700 minimum. The left navigation and right inspector stay fixed while the center column scrolls. The light slate/teal theme is defined in one place in `mission_map_gui.py`; no external stylesheet is used.

## Map and graph behavior

- **Map discovery:** enumerate source files across the selected repository, excluding version-control, generated/build, cache, virtual-environment, and vendored dependency directories. Find map declarations using static Python analysis (annotations such as `TransitionMap`, resolvable map literals/aliases/unpacking/updates, and related state-machine configuration). Never import or execute scanned mission modules.
- **One card per map:** display module, variable name, package-relative file path, counts of states/edges, configured start/stop if confidently resolved, and analysis warnings. If a single file defines several maps, each gets a separate card.
- **Graph drawing:** use Graphviz `dot` only for layout (JSON output), then draw nodes, arrows, labels, and arrowheads on the card's Tk canvas. Graph interaction does not depend on an image or browser. If Graphviz is unavailable, the card shows a clear message and the state/transition tables remain complete.
- **Nodes:** show concise state class names with `with_params` overrides on the destination node or transition details. Parameterized versions of the same class remain distinct where they represent distinct targets. Configured starts are green, configured stops are red with a double outline, inferred entries/terminals are marked with dashed outlines, and the rest are neutral.
- **Edges:** directed arrows are labelled with the outcome name, matching `state_vis.py`. Declared transitions are teal; soft stops are dashed amber; declared-only transitions from a configured stop are dashed red and labelled `declared only`. Parallel outcome edges stay separate.
- **Entry/stop semantics:** when the associated `StateMachine` configuration provides a start/stop, mark these as **Configured start** and **Configured stop**. Otherwise, identify source-only/target-only nodes as **Inferred entry**/**Inferred terminal**; never present these as configured runtime choices. If an edge leaves a configured stop state, keep it visible but mark it as declared-only/unreachable by the current `run()` stop behavior, and show the analyzer warning.
- **Layout:** default top-to-bottom, with a toolbar option for left-to-right, right-to-left, and bottom-to-top. Each card offers fit, zoom in/out, and mouse-wheel zoom. Drag the canvas to pan. The overall window scrolls through map cards; the graph canvas consumes wheel events only while the pointer is over it.
- **Legibility:** use shapes plus text labels, not color alone. The persistent left legend explains start/stop, inferred, declared-only, and soft-stop styling. Graph nodes and edges are clickable; keyboard focus on a canvas supports `0` (fit), `+`/`=` (zoom in), and `-` (zoom out).
- **Download/export:** offer SVG and PNG export per map. The save dialog starts outside the scanned checkout; the UI rejects a destination inside that checkout and never silently writes into `mrobosub/`.

## Analysis status and error states

- Show warnings in the top analysis box and on the affected map card, with file and line when available. Examples: unresolved outcome/target, unsupported dynamic map construction, missing outcome mapping, duplicate/ambiguous definitions, or a configured stop inconsistency.
- If some maps are understood and others are not, show the understood maps and a separate **Not fully visualized** list; do not fail the whole scan.
- If no maps are found, explain that the scanner only identifies statically resolvable mission maps and offer a repository/file rescan. Do not silently substitute templates or component state machines.
- A rescan replaces the displayed result set and clears stale selection and inspector contents. Show an error if the selected root is invalid or unreadable. Scanning and graph layout must not modify source files.
- Disable repository, focus, and direction controls while a scan is running; the window remains responsive.

## Implementation

Implemented as a local Tkinter/ttk desktop application:

- `mission_map_ui.py` — static discovery, payload construction, Graphviz graph/layout adapter, layout parsing, and SVG/PNG byte export. No HTTP server or network listener.
- `mission_map_gui.py` — the desktop window: header controls, background scan, scrollable map cards, grouped map navigation, Graphviz canvas with pan/zoom, state/transition tables, inspector, notices, legend, and export dialogs.
- `test_mission_map_ui.py` — focused checks for discovery, focus filtering, metadata, Graphviz layout/IDs, layout parsing, read-only scanning, and invalid roots.

Run it with:

```sh
python mission_map_ui.py --root mrobosub
# or, when ./mrobosub is present:
python mission_map_ui.py

# optional focused start (repository-relative file, map name, or map variable):
python mission_map_ui.py --root mrobosub --focus testing_run.py
```

`mission_map_ui.py` is the entry point; it imports `mission_map_gui` only when opening the window. The scanner reuses `repo_state_vis.Repository` for static analysis. Graphviz's `dot` provides coordinates and edge geometry; if it is unavailable, the application still lists maps and offers complete state/transition tables. No React, npm, browser, or web server is required.

## Acceptance criteria

- A user can select the local `mrobosub/` checkout and get one distinct, labelled card for `standard_run.transitions` and one for `testing_run.transitions` in a native popup window.
- The `testing_run` graph contains the five expected state nodes and seven outcome edges; `Start.Complete` and the `Submerge`, `Forward5Seconds`, and `ComeToSurface` outcomes are individually inspectable by clicking either the graph or the tables.
- `standard_run` target parameters and the configured-stop diagnostic are visible without presenting the declared outgoing `Stop` edge as a runtime transition.
- Map navigation reaches every card, and the center view scrolls through all discovered maps while individual graph canvases remain independently pannable and zoomable.
- Scanning never imports/executes arbitrary repository modules, writes into the scanned repository, starts ROS, starts an HTTP server, or labels an inferred entry as a configured start.
- Unsupported/dynamic maps and parse warnings are visible without hiding successfully analyzed maps.
