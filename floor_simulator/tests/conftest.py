import sys
from pathlib import Path

# Make `floor_simulator` importable when pytest runs from this folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
