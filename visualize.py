import argparse     #could also use sys
import ast
import graphviz
from pathlib import Path

def findFiles(path : Path) -> list[Path]:
    "Returns list oh python files from a file path"
    if path.is_file():
        if path.suffix != ".py":
            raise ValueError(f"Error Expected a Python file : {path}")
        return [path]

    if path.is_dir():
        # use "recursive glob" method to get all .py files, return in sorted order
        return sorted(path.rglob("*.py"))
    
    raise FileNotFoundError(f"Path does not exist: {path}")

def getStateName(node: ast.AST) -> str | None:
    """
    Requires: Python Abstract Syntax Tree Node argument
    Effects: Extracts a state name from a state reference
    """

    if isinstance(node, ast.Name):
        #return variable name
        return node.id
    
    if isinstance (node, ast.Attribute):
        #return name of attribute
        return node.attr
    
    return None

def getDestination(node: ast.AST) -> str | None:
    """Get the destination state, including configured states"""

    # Example: Forward5Seconds
    if isinstance(node, ast.Name):
        return node.id

    # Example: Submerge.with_params(target_heave=0.5)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "with_params":
                #Get the "Submerge" part
                return getStateName(node.func.value)
            
            return node.func.attr

        
        return getDestination(node.func)

    # Example: module.Forward5Seconds
    if isinstance(node, ast.Attribute):
        return node.attr

    return None

def extractTransitions(file_path: Path) -> list[tuple[str, str, str]]:
    """Parse a file and return (source, outcome, destination) tuples."""

    source_code = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source_code, filename=str(file_path))

    extracted = []

    for node in ast.walk(tree):
        value = None

        # Example: transitions: TransitionMap = {...}
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                if node.target.id == "transitions":
                    value = node.value

        # Example: transitions = {...}
        elif isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name)
                and target.id == "transitions"
                for target in node.targets
            ):
                value = node.value

        if not isinstance(value, ast.Dict):
            continue

        for key, destination_node in zip(value.keys, value.values):
            # Example key: Start.Complete
            if not isinstance(key, ast.Attribute):
                continue

            source = getStateName(key.value)
            outcome = key.attr
            destination = getDestination(destination_node)

            if source and destination:
                extracted.append((source, outcome, destination))

    return extracted


def main():
    parser = argparse.ArgumentParser(
        description="Generate a RoboSub state-machine graph."
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Python file or directory to search for transition maps",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="state_machine",
        help="Output filename without extension (default: state_machine)",
    )
    args = parser.parse_args()

    files = findFiles(args.path)
    all_transitions = []

    for file_path in files:
        try:
            transitions = extractTransitions(file_path)
            all_transitions.extend(transitions)

            if transitions:
                print(f"Found {len(transitions)} transitions in {file_path}")

        except (SyntaxError, UnicodeDecodeError, OSError) as error:
            print(f"Skipping {file_path}: {error}")

    if not all_transitions:
        print("No transitions found.")
        return

    graph = graphviz.Digraph("RoboSub State Machine", format="png")
    graph.attr(rankdir="LR")
    graph.attr("node", shape="box", style="rounded,filled",
               fillcolor="lightblue")
    graph.attr("edge", fontsize="10")

    # Add every state as a node.
    states = set()

    for source, outcome, destination in all_transitions:
        states.add(source)
        states.add(destination)

    for state in sorted(states):
        graph.node(state, label=state)

    # Add directed edges labeled with their transition outcomes.
    for source, outcome, destination in all_transitions:
        graph.edge(source, destination, label=outcome)

    output_path = graph.render(
        filename=args.output,
        #directory='test_dir', 
        view=True, 
        cleanup=True)
    
    print(f"Graph saved to: {output_path}")
    print(f"Total transitions: {len(all_transitions)}")


if __name__ == "__main__":
    main()