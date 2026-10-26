"""Run with: python -m unittest -v test_repo_state_vis"""
import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import textwrap
import unittest

from repo_state_vis import Edge, Machine, Repository, StateRef, build_graph, main, select_mission, wrap_label


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        # All fixtures stay inside this project, never in the inspected repository.
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding='utf-8')

    def fixture(self):
        self.write('robot/robot/__init__.py', '')
        self.write('robot/robot/framework.py', '''
            class Outcome: pass
            class State: pass
        ''')
        self.write('robot/robot/states.py', '''
            from .framework import State, Outcome
            class Timed(State):
                def handle(self):
                    if self.elapsed >= self.timeout:
                        return self.on_timeout()
                    return self.on_ready()
            class Dive(Timed):
                class Done(Outcome): pass
                class Timeout(Outcome): pass
                def on_ready(self): return self.Done()
                def on_timeout(self): return self.Timeout()
            class End(State): pass
            Surface = End
        ''')
        self.write('robot/robot/mission.py', '''
            from .states import Dive as D, Surface
            transitions = {D.Done: Surface, D.Timeout: D.with_params(depth=2)}
        ''')

    def test_alias_inheritance_parameters_and_conditions(self):
        self.fixture()
        repository = Repository(self.root)
        machines = repository.analyze()
        machine = next(m for m in machines if m.name == 'robot.mission.transitions')
        self.assertEqual(len(machine.edges), 2)
        self.assertEqual({s.name for s in machine.nodes}, {'robot.states.Dive', 'robot.states.End'})
        self.assertIn(('depth', '2'), next(s for s in machine.nodes if s.params).params)
        self.assertTrue(all(e.source.params for e in machine.edges))
        done = next(e for e in machine.edges if e.label.startswith('Done'))
        self.assertIn('not (self.elapsed >= self.timeout)', done.label)
        self.assertFalse(repository.warnings, repository.warnings)

    def test_repository_is_not_executed_or_modified(self):
        self.fixture()
        self.write('danger.py', "raise RuntimeError('must never execute')")
        before = {p: hashlib.sha256(p.read_bytes()).digest() for p in self.root.rglob('*') if p.is_file()}
        Repository(self.root).analyze()
        after = {p: hashlib.sha256(p.read_bytes()).digest() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_duplicate_class_names_remain_separate(self):
        self.fixture()
        self.write('other.py', '''
            from robot.framework import State, Outcome
            class Dive(State):
                class Done(Outcome): pass
            transitions = {Dive.Done: Dive}
        ''')
        machines = [m for m in Repository(self.root).analyze() if m.category == 'mission']
        self.assertEqual(len(machines), 2)
        self.assertEqual(len({s.name for m in machines for s in m.nodes}), 3)

    def test_map_unpacking_updates_and_overrides(self):
        self.fixture()
        self.write('robot/robot/mission.py', '''
            from .states import Dive, Surface
            base = {Dive.Done: Surface}
            transitions = {**base, Dive.Timeout: Surface}
            transitions.update({Dive.Done: Dive.with_params(depth=3)})
            transitions[Dive.Timeout] = Dive.with_params(depth=4)
        ''')
        machines = Repository(self.root).analyze()
        mission = next(m for m in machines if m.name.endswith('.transitions'))
        self.assertEqual({e.target.params for e in mission.edges}, {(('depth', '3'),), (('depth', '4'),)})
        self.assertEqual(len(mission.edges), 4)

    def test_enum_lifecycle_excludes_non_state_enums(self):
        self.write('lifecycle.py', '''
            from enum import Enum
            class RobotState(Enum):
                IDLE = 0
                RUNNING = 1
            class Led(Enum):
                ON = 1
                OFF = 0
            class Controller:
                def __init__(self): self.current_state = RobotState.IDLE
                def tick(self):
                    if self.current_state == RobotState.IDLE:
                        if self.enabled:
                            self.current_state = RobotState.RUNNING
                        self.led = Led.ON
                    elif self.current_state == RobotState.RUNNING:
                        self.current_state = RobotState.IDLE
        ''')
        machines = Repository(self.root).analyze()
        self.assertEqual(len(machines), 1)
        self.assertEqual(len(machines[0].edges), 2)
        self.assertEqual({s.name for s in machines[0].initial}, {'lifecycle.RobotState.IDLE'})
        self.assertTrue(any('self.enabled' in e.label for e in machines[0].edges))

    def test_dynamic_maps_and_syntax_errors_warn(self):
        self.fixture()
        self.write('dynamic.py', 'transitions: TransitionMap = make_map()')
        self.write('broken.py', 'class !!!')
        repository = Repository(self.root)
        repository.analyze()
        self.assertTrue(any('cannot parse' in w for w in repository.warnings))
        self.assertTrue(any('no statically resolvable' in w for w in repository.warnings))

    def test_mutated_guard_does_not_get_misleading_label(self):
        self.fixture()
        self.write('robot/robot/states.py', '''
            from .framework import State, Outcome
            class Dive(State):
                class Done(Outcome): pass
                class Timeout(Outcome): pass
                def handle(self):
                    if self.ready:
                        self.ready = False
                        return self.Done()
            class End(State): pass
            Surface = End
        ''')
        machine = Repository(self.root).analyze()[0]
        self.assertEqual(next(e.label for e in machine.edges if e.label.startswith('Done')), 'Done')

    def test_output_inside_repository_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main([str(self.root), '--mission', 'example', '-o', str(self.root / 'out'), '--format', 'dot'])
        self.assertEqual(error.exception.code, 2)

    def test_graphviz_clusters_labels_and_dot_cli(self):
        try:
            import graphviz
        except ImportError:
            self.skipTest('Python graphviz not installed')
        self.fixture()
        machines = Repository(self.root).analyze()
        graph = build_graph(machines)
        self.assertIsInstance(graph, graphviz.Digraph)
        self.assertIn('cluster_0', graph.source)
        self.assertIn('rankdir=TB', graph.source)
        self.assertIn('depth=2', graph.source)
        self.assertIn('rankdir=LR', build_graph(machines, rankdir='LR').source)
        no_labels = build_graph(machines, labels=False)
        self.assertNotIn('label=Done', no_labels.source)
        # Sibling output is outside the scanned fixture repository.
        output = self.root / 'graph'
        repo = self.root / 'robot'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(repo), '--mission', 'mission', '-o', str(output), '--format', 'dot']), 0)
        self.assertTrue(Path(str(output) + '.dot').read_text(encoding='utf-8').startswith('digraph'))

    def test_graph_layout_wraps_labels_and_preserves_parallel_edges(self):
        try:
            import graphviz
        except ImportError:
            self.skipTest('Python graphviz not installed')
        a, b = StateRef('package.' + 'LongStateName' * 6), StateRef('package.End')
        condition = 'first outcome with a very long condition ' * 4
        machine = Machine('test.machine')
        machine.add(Edge(a, b, condition))
        machine.add(Edge(a, b, 'second outcome'))
        machine.add(Edge(a, b, 'soft stop', 'soft_stop'))
        graph = build_graph([machine])
        self.assertEqual(graph.source.count('m0_n0 -> m0_n1'), 0)  # nodes sort by name
        self.assertEqual(graph.source.count('m0_n1 -> m0_n0'), 3)
        self.assertIn('constraint=false', graph.source)
        self.assertIn('tooltip=', graph.source)
        self.assertTrue(all(len(line) <= 48 for line in wrap_label(condition).splitlines()))
        self.assertEqual(' '.join(wrap_label(condition).split()), ' '.join(condition.split()))
        self.assertEqual(wrap_label('RobotState.AMBIENT', width=10), 'RobotState.AMBIENT')
        self.assertIn('second outcome', graph.source)

    def test_boolean_modes_assignments_conditions_and_dataclasses(self):
        self.write('modes.py', '''
            from dataclasses import dataclass
            class Camera:
                def __init__(self): self.on = False
                def enable(self, req):
                    if req.data == self.on: return
                    self.on = req.data
                def loop(self):
                    if self.on: pass
            @dataclass
            class Client:
                live: bool = True
                def run(self):
                    while self.live:
                        self.live = self.live and send()
                def stop(self): self.live = False
            class Data:
                def __init__(self): self.saved = False
                def update(self, req): self.saved = req.data
        ''')
        machines = Repository(self.root).analyze()
        self.assertEqual(len(machines), 2)
        camera = next(m for m in machines if '.Camera.' in m.name)
        self.assertEqual(len(camera.edges), 2)
        self.assertEqual({s.name.rsplit(' = ', 1)[-1] for s in camera.initial}, {'False'})
        client = next(m for m in machines if '.Client.' in m.name)
        self.assertTrue(all(e.source.name.endswith('True') and e.target.name.endswith('False') for e in client.edges))
        self.assertTrue(any('assuming' in e.label for e in client.edges))

    def test_cpp_boolean_flags_and_comment_handling(self):
        self.write('modes.cpp', '''
            // bool fake = false; fake = true;
            bool enabled = false;
            const char* s = "enabled = true;";
            void set() { enabled = true; }
            void reset() { enabled = false; }
            class Clock {
                bool started;
            public:
                Clock() : started(false) {}
                void tick() { started = true; }
            };
        ''')
        machines = Repository(self.root).analyze()
        self.assertEqual(len(machines), 2)
        enabled = next(m for m in machines if '::enabled' in m.name)
        self.assertEqual(len(enabled.edges), 2)
        self.assertEqual({s.name.rsplit(' = ', 1)[-1] for s in enabled.initial}, {'false'})
        started = next(m for m in machines if '::started' in m.name)
        self.assertEqual(len(started.edges), 1)
        self.assertEqual({s.name.rsplit(' = ', 1)[-1] for s in started.initial}, {'false'})
        self.assertIn('guard unknown', started.edges[0].label)

    def test_unmapped_templates_are_not_mission_edges(self):
        self.fixture()
        self.write('robot/robot/unused.py', '''
            from .framework import State
            class Template(State): pass
        ''')
        machines = Repository(self.root).analyze()
        template = next(m for m in machines if m.category == 'template')
        self.assertEqual({s.name for s in template.nodes}, {'robot.unused.Template', 'robot.states.Timed'})
        self.assertEqual(template.edges, [])

    def test_testing_run_flow_and_cli_output(self):
        root = Path(__file__).parent / 'mrobosub'
        if not root.is_dir():
            self.skipTest('mrobosub checkout not present')
        testing = next(m for m in Repository(root).analyze() if m.name.endswith('.testing_run.transitions'))
        expected = {
            ('Start', 'Submerge', 'Complete'),
            ('Submerge', 'Forward5Seconds', 'Submerged'),
            ('Submerge', 'Forward5Seconds', 'TimedOut'),
            ('Forward5Seconds', 'ComeToSurface', 'Reached'),
            ('Forward5Seconds', 'ComeToSurface', 'Unreached'),
            ('ComeToSurface', 'Stop', 'Surfaced'),
            ('ComeToSurface', 'Stop', 'Failed'),
        }
        actual = {(e.source.name.rsplit('.', 1)[-1], e.target.name.rsplit('.', 1)[-1],
                   e.label.split('\n', 1)[0]) for e in testing.edges}
        self.assertEqual(actual, expected)
        try:
            import graphviz
        except ImportError:
            self.skipTest('Python graphviz not installed')
        graph = build_graph([testing])
        for node in ('Start', 'Submerge', 'Forward5Seconds', 'ComeToSurface', 'Stop'):
            self.assertIn('label=' + node, graph.source)
        for outcome in ('Complete', 'Submerged', 'TimedOut', 'Reached', 'Unreached', 'Surfaced', 'Failed'):
            self.assertIn('label=' + outcome, graph.source)
        self.assertEqual(graph.source.count('shape=ellipse'), 5)
        output = self.root / 'selected'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(root), '--mission', 'testing_run', '-o', str(output), '--format', 'dot']), 0)
        source = Path(str(output) + '.dot').read_text(encoding='utf-8')
        self.assertEqual(source.count('subgraph cluster_'), 1)
        self.assertEqual(source.count(' -> '), 7)
        self.assertNotIn('label="Submerged\n', source)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(root), '--mission', 'missing', '-o', str(output), '--format', 'dot']), 2)

    def test_file_and_variable_mission_selectors(self):
        self.fixture()
        self.write('robot/robot/mission.py', '''
            from .states import Dive, Surface
            transitions = {Dive.Done: Surface}
            alternative = {Dive.Timeout: Surface}
        ''')
        repo = Repository(self.root)
        missions = repo.mission_machines()
        path = 'robot/robot/mission.py'
        self.assertEqual(len(select_mission(repo, missions, path)), 2)
        selected = select_mission(repo, missions, path + ':alternative')
        self.assertEqual([m.name for m in selected], ['robot.mission.alternative'])
        self.assertEqual(select_mission(repo, missions, str(self.root / path) + ':missing'), [])
        self.assertEqual([m.name for m in select_mission(repo, missions, 'mission')], ['robot.mission.transitions'])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.root), '--mission', path, '-o', str(self.root.parent / 'graph'), '--format', 'dot']), 2)

    def test_gui_option_dispatches_without_repository(self):
        from unittest.mock import patch
        with patch('repo_state_vis.launch_gui', return_value=0) as launch:
            self.assertEqual(main(['--gui']), 0)
            launch.assert_called_once_with()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(['--gui', str(self.root)])

    def test_mission_selection_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main([str(self.root)])
        self.assertEqual(error.exception.code, 2)

    def test_actual_mrobosub(self):
        root = Path(__file__).parent / 'mrobosub'
        if not root.is_dir():
            self.skipTest('mrobosub checkout not present')
        repository = Repository(root)
        machines = repository.analyze()
        lifecycle = next(m for m in machines if 'Quartermaster' in m.name)
        self.assertEqual(len(lifecycle.nodes), 3)
        self.assertEqual(len(lifecycle.edges), 3)
        standard = next(m for m in machines if 'standard_run' in m.name)
        self.assertEqual(len(standard.nodes), 3)
        self.assertEqual(len(standard.stops), 1)
        self.assertEqual(sum(e.kind == 'soft_stop' for e in standard.edges), 2)
        self.assertTrue(any('configured StopState' in w for w in repository.warnings))
        testing = next(m for m in machines if 'testing_run' in m.name)
        self.assertEqual(len(testing.nodes), 5)
        self.assertEqual(len(testing.edges), 7)
        templates = next(m for m in machines if m.category == 'template')
        self.assertEqual({s.name.rsplit('.', 1)[-1] for s in templates.nodes}, {
            'TimedState', 'ForwardAndWait', 'DoubleTimedState', 'TurnToYaw',
            'AlignPathmarker', 'CenterOnPathmarker'})
        bool_names = {m.name for m in machines if m.category == 'boolean'}
        for suffix in ('.Botcam.self.on', '.Zed.self.on', '.ThrusterController.self.emergency_stop',
                       '.ThrusterMixing.self.enabled', '.PidDofControlNode.self.pid_enabled',
                       '.PidDofControlNode.self.prev_valid', '.ForwardAndWait.self.waiting',
                       '.DoubleTimedState.self.timed_out_first', '.StateMachine.self.stop_signal_recvd',
                       '.Server.self.shutdown', '.SimHal.Client.self.live',
                       '.MotorTestAll.self.on', '.ImageSaverNode.self.save_flag',
                       '::initialized_rel', '::got_first_message_', '::has_pimu'):
            self.assertTrue(any(suffix in name for name in bool_names), suffix)


if __name__ == '__main__':
    unittest.main()
