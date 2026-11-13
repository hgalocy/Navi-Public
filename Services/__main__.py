import argparse
import os
from importlib import import_module

from .ipc import COMPONENTS

COMPONENT_MODULES = {"listener": "Listener.service", "speaker": "Audio.service",
                     "coordinator": "Coordinator.service", "face": "Face.service"}

def main():
    parser = argparse.ArgumentParser(description="Run one supervised Navi component")
    parser.add_argument("component", choices=COMPONENTS)
    args = parser.parse_args()
    if not os.environ.get("INVOCATION_ID") or not os.environ.get("NOTIFY_SOCKET"):
        parser.error("Start Navi with systemctl --user start navi.target (or one navi-<component>.service)")
    # Set identity before importing component logging and hardware modules.
    os.environ["NAVI_COMPONENT"] = args.component
    import_module(COMPONENT_MODULES[args.component]).run()


if __name__ == "__main__":
    main()
