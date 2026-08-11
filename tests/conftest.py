"""Load pure integration modules without importing Home Assistant."""

import sys
import types
from pathlib import Path

COMPONENT = Path(__file__).parents[1] / "custom_components" / "smart_shopping_route"
package = types.ModuleType("smart_shopping_route")
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("smart_shopping_route", package)
