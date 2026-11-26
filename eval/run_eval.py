"""Run the eval harness without the CLI wrapper: `uv run python eval/run_eval.py [--extra-only]`.

Same as `vet eval`; see vet/evaluate.py and vet/labels.py.
"""

import sys

from vet.cli import app

if __name__ == "__main__":
    sys.exit(app(["eval", *sys.argv[1:]]))
