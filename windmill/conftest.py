# Not a Windmill script (outside f/, outside wmill.yaml's includes: — never synced
# to the server). Makes `f.<folder>.<module>` importable from tests the same way
# Windmill's own runtime resolves those relative imports between scripts.
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
