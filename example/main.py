import sys
import os
import importlib.util

from map import transitions


def load_variable_from_file(file_path: str, variable_name: str, module_name: str = "default_mod"):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load in the file at location {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    spec.loader.exec_module(module)

    try:
        map_var = getattr(module, variable_name)
    except AttributeError:
        raise AttributeError(f"The variable {variable_name} does not exist in the given file {file_path}")

    return map_var    


def main():
    if len(sys.argv) != 3:
        print("USAGE: python main.py <FILE> <TRANSITIONS>")
        sys.exit()

    file = sys.argv[1]
    transition = sys.argv[2]

    print(f"Using file {file}")

    transition_map = load_variable_from_file(file, transition) 

    print(transition_map)   


if __name__ == "__main__":
    main()