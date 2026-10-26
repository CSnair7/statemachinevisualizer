#!/usr/bin/env python3
"""Statically visualize mrobosub-style Python repositories (Python 3.10+).

Install: python -m pip install graphviz
Rendering also requires Graphviz's ``dot`` executable on PATH.

    python repo_state_vis.py mrobosub --mission testing_run -o output_directory/testing_run --format svg
    python repo_state_vis.py mrobosub --mission testing_run -o output_directory/testing_run --format dot
    python repo_state_vis.py mrobosub --mission mrobosub_planning/mrobosub_planning/testing_run.py:transitions
    python repo_state_vis.py --gui

No target modules are imported or executed. Select one transition map with
--mission. This is a source-level possibility graph, NOT a proof of runtime
reachability. Missions not registered with a captain are included. Inferred entry
nodes are not configured start states.

Supported: local/relative/package imports, aliases, State inheritance, nested
Outcomes, .with_params(), literal maps, map aliases/unpacking/unions/updates,
and enum assignments guarded by equality tests on the same state variable.
Enum detection requires "state" in both the enum and self-attribute names;
its labels are local branch guards, not complete callback preconditions.
Conditions are source expressions, not evaluated sensor values. Straight-line
if/else/return paths and self-method delegation are followed; loops, exceptions,
dynamic dispatch and arbitrary computed maps are not interpreted. Unsupported
state-related constructions produce diagnostics; unknown guards are left blank.
Unmapped State subclasses are listed as templates, not reachable mission states.
Persistent Boolean component flags are shown as independent two-value machines,
not as a combined robot state. C++ support is limited to simple bool declarations,
literal assignments, and literal constructor initializers; it does not parse C++.
Component transitions are conservative possibilities, not proofs of execution.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import os
import re
from pathlib import Path
import sys
import textwrap
import tokenize
from typing import Iterable


SKIP_DIRS = {'.git', '.hg', '.svn', '.venv', 'venv', '__pycache__',
             'node_modules', 'build', 'install', 'log', '.tox', '.mypy_cache'}


def text(node: ast.AST) -> str:
    return ast.unparse(node)


def assignments(node: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


@dataclass
class Module:
    name: str
    path: Path
    tree: ast.Module
    imports: dict[str, str] = field(default_factory=dict)
    stars: list[str] = field(default_factory=list)
    values: dict[str, ast.expr] = field(default_factory=dict)


@dataclass
class ClassInfo:
    name: str
    module: Module
    node: ast.ClassDef


@dataclass(frozen=True)
class StateRef:
    name: str
    params: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        return self.name + ("\n" + ', '.join(f'{k}={v}' for k, v in self.params)
                            if self.params else '')


@dataclass(frozen=True)
class Edge:
    source: StateRef
    target: StateRef
    label: str = ''
    kind: str = 'transition'


@dataclass
class Machine:
    name: str
    nodes: set[StateRef] = field(default_factory=set)
    edges: list[Edge] = field(default_factory=list)
    initial: set[StateRef] = field(default_factory=set)
    stops: set[StateRef] = field(default_factory=set)
    category: str = 'machine'

    def add(self, edge: Edge) -> None:
        self.nodes.update((edge.source, edge.target))
        if edge not in self.edges:
            self.edges.append(edge)


class Repository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.modules: dict[str, Module] = {}
        self.classes: dict[str, ClassInfo] = {}
        self.warnings: list[str] = []
        self.file_count = 0
        self._read()
        self._index()

    def warn(self, module: Module, node: ast.AST, message: str) -> None:
        warning = f'{module.path.relative_to(self.root)}:{getattr(node, "lineno", 1)}: {message}'
        if warning not in self.warnings:
            self.warnings.append(warning)

    def _read(self) -> None:
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                             and not (Path(directory) / d).is_symlink())
            for filename in sorted(files):
                path = Path(directory) / filename
                if path.suffix != '.py' or path.is_symlink():
                    continue
                # Do not follow links/junctions outside the requested repository.
                if not path.resolve().is_relative_to(self.root):
                    continue
                self.file_count += 1
                name = path.stem if path.stem != '__init__' else ''
                parent = path.parent
                if not (parent / '__init__.py').is_file():
                    name = '.'.join(path.relative_to(self.root).with_suffix('').parts)
                while (parent / '__init__.py').is_file() and parent.is_relative_to(self.root):
                    name = parent.name + ('.' + name if name else '')
                    parent = parent.parent
                if not name:
                    name = '.'.join(path.relative_to(self.root).with_suffix('').parts)
                try:
                    with tokenize.open(path) as source:
                        tree = ast.parse(source.read(), filename=str(path))
                except (SyntaxError, UnicodeError, OSError) as exc:
                    self.warnings.append(f'{path.relative_to(self.root)}: cannot parse: {exc}')
                    continue
                if name in self.modules:
                    self.warnings.append(f'{path.relative_to(self.root)}: ambiguous module {name}; skipped')
                    continue
                self.modules[name] = Module(name, path, tree)

    def _index(self) -> None:
        def classes(module: Module, body: list[ast.stmt], prefix: str) -> None:
            for node in body:
                if isinstance(node, ast.ClassDef):
                    name = prefix + '.' + node.name
                    self.classes[name] = ClassInfo(name, module, node)
                    classes(module, node.body, name)

        for module in self.modules.values():
            classes(module, module.tree.body, module.name)
            for node in module.tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module.imports[alias.asname or alias.name.split('.')[0]] = (
                            alias.name if alias.asname else alias.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ''
                    if node.level:
                        package = module.name.split('.')
                        if module.path.name != '__init__.py':
                            package.pop()
                        keep = len(package) - node.level + 1
                        base = '.'.join(package[:max(keep, 0)] + ([base] if base else []))
                    for alias in node.names:
                        if alias.name == '*':
                            module.stars.append(base)
                        else:
                            module.imports[alias.asname or alias.name] = base + '.' + alias.name
                targets, value = assignments(node)
                if value is not None:
                    for target in targets:
                        if isinstance(target, ast.Name):
                            module.values[target.id] = value

    def resolve(self, expression: ast.expr, module: Module,
                seen: frozenset[str] = frozenset()) -> str:
        if isinstance(expression, ast.Name):
            local = module.name + '.' + expression.id
            if local in seen:
                return local
            if local in self.classes:
                return local
            if expression.id in module.imports:
                return self.canonical(module.imports[expression.id], seen | {local})
            value = module.values.get(expression.id)
            if isinstance(value, (ast.Name, ast.Attribute)):
                return self.resolve(value, module, seen | {local})
            candidates = [self.canonical(star + '.' + expression.id, seen | {local})
                          for star in module.stars]
            candidates = [c for c in candidates if c in self.classes or c in self.modules]
            if len(set(candidates)) == 1:
                return candidates[0]
            return local
        if isinstance(expression, ast.Attribute):
            return self.canonical(self.resolve(expression.value, module, seen) + '.' + expression.attr, seen)
        return text(expression)

    def canonical(self, name: str, seen: frozenset[str] = frozenset()) -> str:
        if name in self.classes or name in self.modules or name in seen:
            return name
        parts = name.split('.')
        # Legacy scripts may import a sibling by its bare module name.
        if parts[0] not in self.modules:
            matches = [m for m in self.modules if m.endswith('.' + parts[0])]
            if len(matches) == 1:
                return self.canonical(matches[0] + ('.' + '.'.join(parts[1:]) if len(parts) > 1 else ''), seen | {name})
        for index in range(len(parts) - 1, 0, -1):
            module = self.modules.get('.'.join(parts[:index]))
            if module:
                value = self.resolve(ast.Name(id=parts[index]), module, seen | {name})
                suffix = '.'.join(parts[index + 1:])
                return value + ('.' + suffix if suffix else '')
        return name

    def lineage(self, name: str, seen: frozenset[str] = frozenset()) -> list[str]:
        if name in seen:
            return []
        info = self.classes.get(name)
        result = [name]
        if info:
            for base in info.node.bases:
                result.extend(self.lineage(self.resolve(base, info.module), seen | {name}))
        return list(dict.fromkeys(result))

    def is_state(self, name: str) -> bool:
        return any(base.rsplit('.', 1)[-1] == 'State' for base in self.lineage(name))

    def state(self, expression: ast.expr, module: Module,
              seen: frozenset[str] = frozenset()) -> StateRef | None:
        if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Attribute):
            if expression.func.attr == 'with_params':
                base = self.state(expression.func.value, module, seen)
                if base:
                    if expression.args or any(k.arg is None for k in expression.keywords):
                        self.warn(module, expression, 'dynamic with_params arguments cannot be resolved')
                        return None
                    params = dict(base.params)
                    params.update((k.arg, text(k.value)) for k in expression.keywords if k.arg is not None)
                    return StateRef(base.name, tuple(sorted(params.items())))
        name = self.resolve(expression, module)
        if name in self.classes and self.is_state(name):
            return StateRef(name)
        if name not in seen:
            found = self.value(name)
            if found:
                return self.state(found[1], found[0], seen | {name})
        return None

    def value(self, name: str) -> tuple[Module, ast.expr] | None:
        module_name, _, variable = name.rpartition('.')
        module = self.modules.get(module_name)
        if module and variable in module.values:
            return module, module.values[variable]
        return None

    def outcome(self, expression: ast.expr, module: Module) -> tuple[str, str] | None:
        name = self.resolve(expression, module)
        owner, _, outcome = name.rpartition('.')
        # Resolve inherited outcomes to their actual declaring class (OutcomeMeta._state).
        for base in self.lineage(owner):
            info = self.classes.get(base + '.' + outcome)
            if info and self.is_state(base):
                if any(n.rsplit('.', 1)[-1] == 'Outcome' for n in self.lineage(info.name)):
                    return base, outcome
        return None

    def method(self, state: str, name: str) -> tuple[ClassInfo, ast.FunctionDef | ast.AsyncFunctionDef] | None:
        for base in self.lineage(state):
            info = self.classes.get(base)
            if info:
                for node in info.node.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                        return info, node
        return None

    def conditions(self, state: str, outcome: str) -> str:
        """Follow return paths from handle, including inherited self-method calls.

        Only return labels if every path to this outcome has a known guard.
        A complex control-flow construct makes its labels unknown, not guessed.
        """
        def call(name: str, guards: tuple[str, ...], stack: frozenset[str]) -> list[tuple[str, tuple[str, ...]]]:
            if name in stack:
                return []
            found = self.method(state, name)
            if not found:
                return []
            info, method = found
            # Guard expressions over mutated attributes are not entry predicates.
            written = {text(t) for n in ast.walk(method) for t in assignments(n)[0]}
            tested = {text(n) for branch in ast.walk(method) if isinstance(branch, ast.If)
                      for n in ast.walk(branch.test)}
            if written & tested:
                return []
            if any(isinstance(n, (ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With,
                                  ast.AsyncWith, ast.Match)) for n in ast.walk(method)):
                return []

            def returns(expr: ast.expr | None, path: tuple[str, ...]) -> list[tuple[str, tuple[str, ...]]]:
                if not isinstance(expr, ast.Call):
                    return []
                func = expr.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == 'self':
                    if self.method(state, func.attr):
                        return call(func.attr, path, stack | {name})
                    return [(func.attr, path)]
                resolved = self.outcome(func, info.module)
                return [(resolved[1], path)] if resolved else []

            def block(body: list[ast.stmt], paths: list[tuple[str, ...]]) -> tuple[list[tuple[str, tuple[str, ...]]], list[tuple[str, ...]]]:
                results: list[tuple[str, tuple[str, ...]]] = []
                for stmt in body:
                    if isinstance(stmt, ast.Return):
                        for path in paths:
                            results.extend(returns(stmt.value, path))
                        return results, []
                    if isinstance(stmt, ast.If):
                        condition = text(stmt.test)
                        yes, left = block(stmt.body, [p + (condition,) for p in paths])
                        no, right = block(stmt.orelse, [p + (f'not ({condition})',) for p in paths])
                        results.extend(yes + no)
                        paths = left + right
                return results, paths

            return block(method.body, [guards])[0]

        paths = [guards for found, guards in call('handle', (), frozenset()) if found == outcome]
        if not paths or any(not path for path in paths):
            return ''
        return ' OR '.join(dict.fromkeys(' AND '.join(f'({g})' for g in path) for path in paths))

    def map_items(self, expression: ast.expr, module: Module,
                  seen: frozenset[str] = frozenset()) -> list[tuple[ast.expr, ast.expr, Module]]:
        if isinstance(expression, ast.Dict):
            result = []
            for key, value in zip(expression.keys, expression.values):
                if key is None:
                    result.extend(self.map_items(value, module, seen))
                else:
                    result.append((key, value, module))
            return result
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.BitOr):
            return self.map_items(expression.left, module, seen) + self.map_items(expression.right, module, seen)
        name = self.resolve(expression, module)
        if name not in seen:
            found = self.value(name)
            if found:
                return self.map_items(found[1], found[0], seen | {name})
        return []

    def mission_machines(self) -> list[Machine]:
        machines = []
        for module in self.modules.values():
            for variable, expression in module.values.items():
                items = self.map_items(expression, module)
                # Include simple module-level map mutations in source order.
                for stmt in module.tree.body:
                    if getattr(stmt, 'lineno', 0) <= expression.lineno:
                        continue
                    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                        func = stmt.value.func
                        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == variable:
                            if func.attr == 'update' and len(stmt.value.args) == 1 and not stmt.value.keywords:
                                items.extend(self.map_items(stmt.value.args[0], module))
                            else:
                                if 'transition' in variable.lower():
                                    self.warn(module, stmt, f'unsupported mutation of {variable}')
                    targets, value = assignments(stmt)
                    for target in targets:
                        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id == variable and value is not None:
                            items.append((target.slice, value, module))
                annotated = any(isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                                and stmt.target.id == variable and text(stmt.annotation).rsplit('.', 1)[-1] == 'TransitionMap'
                                for stmt in module.tree.body)
                resolved: dict[tuple[str, str], StateRef] = {}
                for key, value, origin in items:
                    source = self.outcome(key, origin)
                    target = self.state(value, origin)
                    if source and target:
                        resolved[source] = target
                    elif source or annotated:
                        self.warn(origin, key, f'unresolved transition: {text(key)} -> {text(value)}')
                if not resolved:
                    if annotated:
                        self.warn(module, expression, f'no statically resolvable transitions in {variable}')
                    continue
                machine = Machine(module.name + '.' + variable, category='mission')
                targets = set(resolved.values())
                for (owner, outcome), target in resolved.items():
                    # Parameterized subclasses inherit the same outcome classes.
                    sources = {s for s in targets if owner in self.lineage(s.name)} or {StateRef(owner)}
                    for source in sources:
                        condition = self.conditions(source.name, outcome)
                        label = outcome + ('\n' + condition if condition else '')
                        machine.add(Edge(source, target, label))
                self.runtime_stops(machine)
                machines.append(machine)
        return machines

    def runtime_stops(self, machine: Machine) -> None:
        """Recognize explicit StateMachine stop arguments and its soft-stop branch."""
        for module in self.modules.values():
            for node in ast.walk(module.tree):
                if not isinstance(node, ast.Call) or self.resolve(node.func, module).rsplit('.', 1)[-1] != 'StateMachine':
                    continue
                args = dict(zip(('name', 'transitions', 'StartState', 'StopState', 'captainNode'), node.args))
                args.update((k.arg, k.value) for k in node.keywords if k.arg is not None)
                stop_expr = args.get('StopState')
                if stop_expr is None:
                    continue
                stop = self.state(stop_expr, module)
                map_expr = args.get('transitions')
                if stop is None or map_expr is None:
                    continue
                map_names = {self.resolve(map_expr, module)}
                if isinstance(map_expr, ast.Subscript):
                    for _, value, origin in self.map_items(map_expr.value, module):
                        map_names.add(self.resolve(value, origin))
                if machine.name not in map_names:
                    continue
                machine.stops.add(stop)
                machine.nodes.add(stop)
                start_expr = args.get('StartState')
                start = self.state(start_expr, module) if start_expr is not None else None
                if start:
                    machine.initial.add(start)
                    machine.nodes.add(start)
                framework = self.classes.get(self.resolve(node.func, module))
                soft_stop = False
                if framework:
                    for branch in ast.walk(framework.node):
                        if isinstance(branch, ast.If) and text(branch.test) == 'self.stop_signal_recvd':
                            soft_stop = any(isinstance(n, ast.Assign) and isinstance(n.value, ast.Attribute)
                                            and text(n.value) == 'self.StopState'
                                            for stmt in branch.body for n in ast.walk(stmt))
                if soft_stop:
                    for state in sorted(machine.nodes - {stop}, key=lambda s: s.label):
                        machine.add(Edge(state, stop, 'self.stop_signal_recvd (soft stop)', 'soft_stop'))
                for edge in machine.edges:
                    if edge.source == stop and edge.kind != 'soft_stop':
                        self.warn(module, node, f'{machine.name}: declared outgoing transition from configured StopState {stop.name}; run() stops before dispatching it')

    def enum_machines(self) -> list[Machine]:
        machines = []
        for info in self.classes.values():
            methods = [n for n in info.node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            groups: dict[tuple[str, str], Machine] = {}
            for method in methods:
                # Exclude closures; they have a separate execution context.
                def scan(body: list[ast.stmt], guards: tuple[ast.expr, ...] = ()) -> None:
                    for stmt in body:
                        if isinstance(stmt, ast.If):
                            scan(stmt.body, guards + (stmt.test,))
                            scan(stmt.orelse, guards + (ast.UnaryOp(op=ast.Not(), operand=stmt.test),))
                            continue
                        targets, value = assignments(stmt)
                        if value is None or not isinstance(value, ast.Attribute):
                            continue
                        resolved = self.resolve(value, info.module)
                        enum, _, member = resolved.rpartition('.')
                        enum_info = self.classes.get(enum)
                        if not enum_info or not any(n in {'enum.Enum', 'enum.IntEnum', 'enum.StrEnum'} for n in self.lineage(enum)):
                            continue
                        members = {t.id for n in enum_info.node.body for t in assignments(n)[0] if isinstance(t, ast.Name)}
                        if member not in members:
                            continue
                        for target in targets:
                            if not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name) or target.value.id != 'self':
                                continue
                            variable = text(target)
                            # LED selections and protocol enums are not robot states.
                            if 'state' not in target.attr.lower() or 'state' not in enum.rsplit('.', 1)[-1].lower():
                                continue
                            machine = groups.setdefault((variable, enum), Machine(info.name + '.' + variable))
                            machine.nodes.update(StateRef(enum + '.' + m) for m in members)
                            destination = StateRef(resolved)
                            if method.name == '__init__':
                                if not guards:
                                    machine.initial.add(destination)
                                else:
                                    self.warn(info.module, stmt, 'conditional initial enum state; entry not inferred')
                                continue
                            sources = set()
                            for guard in guards:
                                if isinstance(guard, ast.Compare) and len(guard.ops) == 1 and isinstance(guard.ops[0], (ast.Eq, ast.Is)):
                                    left, right = guard.left, guard.comparators[0]
                                    other = right if text(left) == variable else left if text(right) == variable else None
                                    if other is not None:
                                        candidate = self.resolve(other, info.module)
                                        if candidate.rpartition('.')[0] == enum and candidate.rpartition('.')[2] in members:
                                            sources.add(StateRef(candidate))
                            # These are lexical branch guards, not full callback preconditions
                            # (earlier returns, sensor history, and side effects may also matter).
                            label = ('local guard: ' + ' AND '.join(f'({text(g)})' for g in guards)
                                     if guards else '')
                            if sources:
                                for source in sources:
                                    machine.add(Edge(source, destination, label))
                            else:
                                # An unguarded assignment may be made from any enum member.
                                for source in sorted(machine.nodes, key=lambda s: s.name):
                                    machine.add(Edge(source, destination, label))
                scan(method.body)
            machines.extend(m for m in groups.values() if m.edges)
        return machines

    def template_machine(self, missions: list[Machine]) -> list[Machine]:
        used = {node.name for machine in missions for node in machine.nodes}
        candidates = [info for name, info in self.classes.items()
                      if name not in used and name.rsplit('.', 1)[-1] != 'State'
                      and self.is_state(name)]
        if not candidates:
            return []
        machine = Machine('Unmapped planning classes (templates, not configured missions)',
                          category='template')
        for info in candidates:
            machine.nodes.add(StateRef(info.name))
        return [machine]

    def boolean_machines(self) -> list[Machine]:
        """Approximate independent Python bool flags initialized on self.

        Branches and earlier returns narrow possible source values. Assignments
        from non-literal expressions are treated as either Boolean value; they
        are explicitly labeled as assumptions, not evaluated. Loops and try
        blocks are inspected conservatively, without claiming exact conditions.
        """
        machines: list[Machine] = []
        for info in self.classes.values():
            methods = [n for n in info.node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            initial: dict[str, bool] = {}
            for method in methods:
                if method.name != '__init__':
                    continue
                for stmt in method.body:
                    targets, value = assignments(stmt)
                    if isinstance(value, ast.Constant) and type(value.value) is bool:
                        for target in targets:
                            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == 'self':
                                initial[target.attr] = value.value
            # Dataclass fields also initialize per-instance state without an
            # explicit __init__, e.g. a connection's ``live: bool = True``.
            if any(text(decorator).split('(')[0].rsplit('.', 1)[-1] == 'dataclass'
                   for decorator in info.node.decorator_list):
                for stmt in info.node.body:
                    targets, value = assignments(stmt)
                    if isinstance(stmt, ast.AnnAssign) and isinstance(value, ast.Constant) and type(value.value) is bool:
                        for target in targets:
                            if isinstance(target, ast.Name):
                                initial.setdefault(target.id, value.value)
            for variable, initial_value in initial.items():
                # Only flags used as conditions or compared are component modes;
                # this excludes stored data that does not control behavior.
                ref = 'self.' + variable
                tested = any(ref == text(n) for method in methods for branch in ast.walk(method)
                             if isinstance(branch, (ast.If, ast.While, ast.IfExp))
                             for n in ast.walk(branch.test))
                if not tested:
                    continue
                machine = Machine(info.name + '.' + ref + ' (Boolean component mode)',
                                  category='boolean')
                states = {value: StateRef(info.name + '.' + ref + ' = ' + str(value))
                          for value in (False, True)}
                machine.nodes.update(states.values())
                machine.initial.add(states[initial_value])

                def eval_test(node: ast.expr, current: bool) -> bool | None:
                    if text(node) == ref:
                        return current
                    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                        value = eval_test(node.operand, current)
                        return not value if value is not None else None
                    if isinstance(node, ast.Compare) and len(node.ops) == 1:
                        op, right = node.ops[0], node.comparators[0]
                        left = node.left
                        if text(right) == ref:
                            left, right = right, left
                        if text(left) == ref and isinstance(right, ast.Constant) and type(right.value) is bool:
                            if isinstance(op, (ast.Eq, ast.Is)):
                                return current == right.value
                            if isinstance(op, (ast.NotEq, ast.IsNot)):
                                return current != right.value
                    return None

                def possible_values(value: ast.expr, current: bool) -> set[bool]:
                    if isinstance(value, ast.Constant) and type(value.value) is bool:
                        return {value.value}
                    if text(value) == ref:
                        return {current}
                    if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.Not):
                        return {not result for result in possible_values(value.operand, current)}
                    if isinstance(value, ast.BoolOp) and isinstance(value.op, (ast.And, ast.Or)):
                        values = possible_values(value.values[0], current)
                        for operand in value.values[1:]:
                            right = possible_values(operand, current)
                            values = {a and b if isinstance(value.op, ast.And) else a or b
                                      for a in values for b in right}
                        return values
                    return {False, True}

                def scan(body: list[ast.stmt], paths: list[tuple[bool, tuple[str, ...]]], method: str) -> list[tuple[bool, tuple[str, ...]]]:
                    for stmt in body:
                        if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                            return []
                        if isinstance(stmt, ast.If):
                            yes, no = [], []
                            for current, guards in paths:
                                result = eval_test(stmt.test, current)
                                if result is not False:
                                    yes.append((current, guards + ((text(stmt.test),) if result is None else ())))
                                if result is not True:
                                    no.append((current, guards + ((f'not ({text(stmt.test)})',) if result is None else ())))
                            paths = scan(stmt.body, yes, method) + scan(stmt.orelse, no, method)
                            continue
                        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)):
                            # Inspect one pass; repeated iterations and exception
                            # ordering are not fully modeled.
                            branches = [stmt.body]
                            if isinstance(stmt, ast.Try):
                                branches += [handler.body for handler in stmt.handlers]
                                branches += [stmt.orelse, stmt.finalbody]
                            paths = paths + [p for branch in branches for p in scan(branch, paths, method)]
                            continue
                        targets, value = assignments(stmt)
                        if value is None or not any(text(t) == ref for t in targets):
                            continue
                        next_paths = []
                        for current, guards in paths:
                            literal = isinstance(value, ast.Constant) and type(value.value) is bool
                            for dest in sorted(possible_values(value, current)):
                                # Branch-local guards can contradict each other or
                                # be invalidated by assignments/calls. Do not show
                                # them as complete preconditions.
                                label = method
                                if not literal:
                                    label += f'\nassuming {text(value)} is {dest}'
                                if current != dest:
                                    machine.add(Edge(states[current], states[dest], label, 'component'))
                                next_paths.append((dest, guards))
                        paths = next_paths
                    return paths

                for method in methods:
                    if method.name != '__init__':
                        scan(method.body, [(False, ()), (True, ())], method.name)
                if machine.edges:
                    machines.append(machine)
        return machines

    def cpp_boolean_machines(self) -> list[Machine]:
        """Find a conservative subset of C++ bool flags, without a C++ parser."""
        machines: list[Machine] = []
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(directory) / d).is_symlink())
            for filename in sorted(files):
                path = Path(directory) / filename
                if path.suffix.lower() not in {'.cpp', '.cc', '.cxx', '.h', '.hpp'} or path.is_symlink() or not path.resolve().is_relative_to(self.root):
                    continue
                try:
                    source = path.read_text(encoding='utf-8')
                except (OSError, UnicodeError) as exc:
                    self.warnings.append(f'{path.relative_to(self.root)}: cannot read C++: {exc}')
                    continue
                # Preserve line numbers and avoid matching strings and comments.
                stripped = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                                  lambda m: re.sub(r'[^\n]', ' ', m.group()), source)
                declarations = list(re.finditer(r'\bbool\s+([A-Za-z_]\w*)\s*(?:=\s*(true|false))?\s*;', stripped))
                counts: dict[str, int] = {}
                for decl in declarations:
                    counts[decl.group(1)] = counts.get(decl.group(1), 0) + 1
                for decl in declarations:
                    name = decl.group(1)
                    if counts[name] != 1:
                        self.warnings.append(f'{path.relative_to(self.root)}: ambiguous C++ bool {name}; skipped')
                        continue
                    initial = decl.group(2)
                    init_match = re.search(r'\b' + re.escape(name) + r'\s*\(\s*(true|false)\s*\)', stripped)
                    if initial is None and init_match:
                        initial = init_match.group(1)
                    changes = [match for match in re.finditer(r'\b' + re.escape(name) + r'\s*=\s*(true|false)\s*;', stripped)
                               if not (decl.start() <= match.start() < decl.end())]
                    if not changes:
                        continue
                    machine = Machine(f'{path.relative_to(self.root).as_posix()}::{name} (C++ Boolean; lexical approximation)',
                                      category='boolean')
                    states = {value: StateRef(f'{path.relative_to(self.root).as_posix()}::{name} = {value}')
                              for value in ('false', 'true')}
                    machine.nodes.update(states.values())
                    if initial is not None:
                        machine.initial.add(states[initial])
                    for match in changes:
                        dest = match.group(1)
                        line = stripped.count('\n', 0, match.start()) + 1
                        machine.add(Edge(states['false' if dest == 'true' else 'true'], states[dest],
                                         f'literal assignment at line {line}; guard unknown', 'component'))
                    machines.append(machine)
        return machines

    def analyze(self) -> list[Machine]:
        missions = self.mission_machines()
        machines = (missions + self.enum_machines() + self.template_machine(missions)
                    + self.boolean_machines() + self.cpp_boolean_machines())
        if not machines:
            self.warnings.append('No supported state machines found; dynamic Python is not executed.')
        return sorted(machines, key=lambda m: m.name)


def wrap_label(label: str, width: int = 48) -> str:
    """Wrap at whitespace; keep identifiers intact even if they exceed the width."""
    return '\n'.join(
        '\n'.join(textwrap.wrap(line, width=width, break_on_hyphens=False,
                                break_long_words=False) or [''])
        for line in label.split('\n')
    )


def build_graph(machines: Iterable[Machine], title: str = 'Repository state machines',
                labels: bool = True, rankdir: str = 'TB'):
    import graphviz

    graph = graphviz.Digraph('repository_states')
    graph.attr(label=title + '\nStatic possibilities; not a reachability proof',
               labelloc='t', rankdir=rankdir, compound='true', fontname='Helvetica',
               pad='0.4', nodesep='0.6', ranksep='1.0')
    graph.attr('node', shape='box', style='rounded', fontname='Helvetica',
               margin='0.18,0.12')
    graph.attr('edge', fontname='Helvetica', fontsize='10', arrowsize='0.8')
    for index, machine in enumerate(machines):
        with graph.subgraph(name=f'cluster_{index}') as cluster:
            cluster.attr(label=wrap_label(machine.name), margin='24', fontname='Helvetica', fontsize='14')
            nodes = sorted(machine.nodes, key=lambda s: s.label)
            short_names = [node.name.rsplit('.', 1)[-1] for node in nodes]
            ids = {node: f'm{index}_n{number}' for number, node in enumerate(nodes)}
            incoming = {edge.target for edge in machine.edges if edge.kind != 'soft_stop'}
            outgoing = {edge.source for edge in machine.edges if edge.kind != 'soft_stop'}
            for node in nodes:
                attrs = {}
                markers = []
                if machine.category == 'template':
                    attrs.update(style='rounded,dashed', color='gray')
                    markers.append('unmapped template; reachability unknown')
                if node in machine.initial:
                    attrs.update(style='rounded,filled', fillcolor='palegreen')
                    markers.append('configured initial')
                elif node in outgoing - incoming:
                    attrs.update(style='rounded,filled', fillcolor='lightcyan')
                    markers.append('inferred entry')
                if node in machine.stops:
                    attrs.update(peripheries='2', color='firebrick')
                    markers.append('configured stop')
                display = node.label + ('\n[' + ', '.join(markers) + ']' if markers else '')
                if machine.category == 'mission':
                    # Keep parameterized variants and same-named classes distinct.
                    short = node.name.rsplit('.', 1)[-1]
                    display = short if short_names.count(short) == 1 else node.label
                    attrs.pop('style', None)
                    attrs.pop('fillcolor', None)
                    attrs.pop('color', None)
                    attrs.pop('peripheries', None)
                    attrs['shape'] = 'ellipse'
                attrs['tooltip'] = node.label
                cluster.node(ids[node], graphviz.escape(wrap_label(display)), **attrs)
            for edge in machine.edges:
                attrs = {'style': 'dashed', 'color': 'firebrick', 'constraint': 'false'} if edge.kind == 'soft_stop' else {}
                if edge.source in machine.stops:
                    attrs.update(style='dotted', color='gray', tooltip='Declared only: runtime stops before dispatch')
                if labels and edge.label and 'tooltip' not in attrs:
                    attrs['tooltip'] = graphviz.escape(edge.label)
                edge_label = edge.label
                if machine.category == 'mission' and edge.kind == 'transition':
                    edge_label = edge_label.split('\n', 1)[0]
                cluster.edge(ids[edge.source], ids[edge.target],
                             graphviz.escape(wrap_label(edge_label)) if labels else '', **attrs)
    return graph


def select_mission(repository: Repository, missions: list[Machine], selector: str) -> list[Machine]:
    """Select maps by module name, fully qualified name, or repo-relative file[:map]."""
    file_part, separator, variable = selector.partition(':')
    if file_part.lower().endswith('.py'):
        path = Path(file_part)
        if not path.is_absolute():
            path = repository.root / path
        path = path.resolve()
        matches = []
        for machine in missions:
            module_name, _, map_name = machine.name.rpartition('.')
            module = repository.modules.get(module_name)
            if module and module.path.resolve() == path and (not separator or map_name == variable):
                matches.append(machine)
        return matches
    if separator:
        return []
    return [m for m in missions if m.name == selector or
            m.name.removesuffix('.transitions').endswith('.' + selector)]


def launch_gui() -> int:
    """Choose a repository, mission file and every supported rendering option."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title('State machine visualizer')
    root.columnconfigure(1, weight=1)
    repository = tk.StringVar(value=str(Path('mrobosub').resolve()) if Path('mrobosub').is_dir() else '')
    target = tk.StringVar()
    mission_map = tk.StringVar()
    output = tk.StringVar(value=str(Path('output_directory/mission').resolve()))
    format_ = tk.StringVar(value='svg')
    rankdir = tk.StringVar(value='TB')
    no_labels = tk.BooleanVar(value=False)
    status = tk.StringVar(value='Choose a repository and Python file, then load its transition maps.')
    choices: dict[str, str] = {}
    loaded: list[tuple[str, str]] = []

    def row(label: str, variable: tk.StringVar, number: int, browse) -> None:
        ttk.Label(root, text=label).grid(row=number, column=0, sticky='w', padx=8, pady=5)
        ttk.Entry(root, textvariable=variable).grid(row=number, column=1, sticky='ew', padx=8, pady=5)
        ttk.Button(root, text='Browse...', command=browse).grid(row=number, column=2, padx=8, pady=5)

    def choose_repository() -> None:
        path = filedialog.askdirectory(title='Select repository', initialdir=repository.get() or '.')
        if path:
            repository.set(path)
            target.set('')
            mission_map.set('')
            choices.clear()
            loaded.clear()
            combo['values'] = ()

    def choose_target() -> None:
        path = filedialog.askopenfilename(title='Select mission Python file',
                                          initialdir=repository.get() or '.',
                                          filetypes=[('Python files', '*.py'), ('All files', '*')])
        if path:
            target.set(path)
            load_maps()

    def load_maps() -> None:
        choices.clear()
        loaded.clear()
        mission_map.set('')
        combo['values'] = ()
        try:
            repo_path = Path(repository.get()).resolve()
            if not repository.get() or not repo_path.is_dir():
                raise ValueError('Choose a valid repository directory.')
            if not target.get():
                raise ValueError('Choose a mission Python file.')
            path = Path(target.get()).resolve()
            if not path.is_file() or not path.is_relative_to(repo_path):
                raise ValueError('The mission file must be inside the selected repository.')
            repo = Repository(repo_path)
            missions = repo.mission_machines()
            matches = select_mission(repo, missions, str(path))
            if not matches:
                raise ValueError('No supported transition maps found in that file.')
            for machine in matches:
                variable = machine.name.rsplit('.', 1)[-1]
                choices[variable] = machine.name
            combo['values'] = tuple(choices)
            loaded.append((str(repo_path), str(path)))
            mission_map.set(next(iter(choices)))
            status.set(f'Found {len(matches)} map(s). Select one and generate the graph.')
        except (OSError, ValueError) as exc:
            status.set(str(exc))
            messagebox.showerror('Cannot load maps', str(exc), parent=root)

    def choose_output() -> None:
        path = filedialog.asksaveasfilename(title='Choose output prefix', initialfile=Path(output.get()).name)
        if path:
            output.set(path)

    def generate() -> None:
        try:
            if mission_map.get() not in choices:
                raise ValueError('Load and select a transition map first.')
            repo_path = Path(repository.get()).resolve()
            file_path = Path(target.get()).resolve()
            if loaded != [(str(repo_path), str(file_path))]:
                raise ValueError('Repository or mission file changed; load maps again.')
            if not file_path.is_relative_to(repo_path):
                raise ValueError('The mission file must be inside the selected repository.')
            if not output.get().strip():
                raise ValueError('Choose an output prefix.')
            args = [str(repo_path), '--mission', str(file_path) + ':' + mission_map.get(),
                    '--output', output.get(), '--format', format_.get(), '--rankdir', rankdir.get()]
            if no_labels.get():
                args.append('--no-labels')
            if Path(output.get()).resolve().is_relative_to(repo_path):
                raise ValueError('Output must be outside the selected repository.')
            # main emits detailed diagnostics; present them in the window too.
            import contextlib
            import io
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(args)
            details = (stdout.getvalue() + stderr.getvalue()).strip()
            status.set('Graph generated.' if code == 0 else 'Graph generation failed.')
            if code:
                messagebox.showerror('Graph generation failed', details or f'Exit status {code}', parent=root)
            else:
                messagebox.showinfo('Graph generated', details, parent=root)
        except (OSError, ValueError, SystemExit) as exc:
            status.set('Graph generation failed.')
            messagebox.showerror('Graph generation failed', str(exc), parent=root)

    row('Repository', repository, 0, choose_repository)
    row('Target Python file', target, 1, choose_target)
    ttk.Label(root, text='Transition map').grid(row=2, column=0, sticky='w', padx=8, pady=5)
    combo = ttk.Combobox(root, textvariable=mission_map, state='readonly')
    combo.grid(row=2, column=1, sticky='ew', padx=8, pady=5)
    ttk.Button(root, text='Load maps', command=load_maps).grid(row=2, column=2, padx=8, pady=5)
    row('Output prefix', output, 3, choose_output)
    ttk.Label(root, text='Format').grid(row=4, column=0, sticky='w', padx=8, pady=5)
    ttk.Combobox(root, textvariable=format_, values=('svg', 'png', 'pdf', 'dot'), state='readonly').grid(row=4, column=1, sticky='ew', padx=8, pady=5)
    ttk.Label(root, text='Direction').grid(row=5, column=0, sticky='w', padx=8, pady=5)
    ttk.Combobox(root, textvariable=rankdir, values=('TB', 'LR', 'RL', 'BT'), state='readonly').grid(row=5, column=1, sticky='ew', padx=8, pady=5)
    ttk.Checkbutton(root, text='Leave edge labels blank', variable=no_labels).grid(row=6, column=1, sticky='w', padx=8, pady=5)
    ttk.Button(root, text='Generate graph', command=generate).grid(row=7, column=1, sticky='ew', padx=8, pady=8)
    ttk.Label(root, textvariable=status, wraplength=550).grid(row=8, column=0, columnspan=3, sticky='w', padx=8, pady=8)
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('repository', type=Path, nargs='?', help='repository root (read only)')
    parser.add_argument('--gui', action='store_true', help='choose options in a Tkinter window')
    parser.add_argument('-o', '--output', type=Path, default=Path('output_directory/repository_states'), help='output prefix, outside the scanned repository')
    parser.add_argument('--format', choices=('svg', 'png', 'pdf', 'dot'), default='svg')
    parser.add_argument('--no-labels', action='store_true', help='leave all edge labels blank')
    parser.add_argument('--mission', help='mission module, fully qualified map, or repository-relative file.py[:map]')
    parser.add_argument('--rankdir', choices=('LR', 'TB', 'RL', 'BT'), default='TB',
                        help='graph direction (default: TB, top to bottom)')
    args = parser.parse_args(argv)
    if args.gui:
        if args.repository is not None or args.mission is not None:
            parser.error('--gui cannot be combined with repository or --mission; choose them in the window')
        try:
            return launch_gui()
        except ImportError:
            print('error: Tkinter is not available in this Python installation', file=sys.stderr)
            return 2
    if args.repository is None or args.mission is None:
        parser.error('repository and --mission are required unless --gui is used')
    root = args.repository.resolve()
    if not root.is_dir():
        parser.error(f'not a directory: {root}')
    output = args.output.resolve()
    if output.is_relative_to(root):
        parser.error('output must be outside the scanned repository to keep it unchanged')
    repository = Repository(root)
    machines = repository.analyze()
    for warning in repository.warnings:
        print(f'warning: {warning}', file=sys.stderr)
    if not machines:
        return 1
    missions = [m for m in machines if m.category == 'mission']
    matches = select_mission(repository, missions, args.mission)
    if len(matches) != 1:
        print(f'error: mission {args.mission!r} matched {len(matches)} maps; available: '
              + ', '.join(m.name for m in missions), file=sys.stderr)
        return 2
    machines = matches
    try:
        graph = build_graph(machines, root.name, not args.no_labels, args.rankdir)
    except ImportError:
        print('error: install the Python package with: python -m pip install graphviz', file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    dot_path = Path(str(output) + '.dot')
    graph.save(str(dot_path))
    print(f'Scanned {repository.file_count} Python files and supported C++ files; '
          f'selected {len(machines)} mission map, '
          f'{sum(len(m.nodes) for m in machines)} state nodes, '
          f'{sum(len(m.edges) for m in machines)} edges (including soft stops).')
    print(f'Wrote {dot_path}')
    if args.format != 'dot':
        import graphviz
        try:
            rendered = graph.render(filename=str(dot_path), outfile=str(output) + '.' + args.format,
                                    format=args.format, cleanup=False)
        except graphviz.backend.ExecutableNotFound:
            print('error: DOT was saved, but rendering requires Graphviz binaries. '
                  'Install Graphviz and put dot on PATH, or use --format dot.', file=sys.stderr)
            return 2
        except graphviz.backend.CalledProcessError as exc:
            print(f'error: Graphviz rendering failed: {exc}', file=sys.stderr)
            return 2
        print(f'Wrote {rendered}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
