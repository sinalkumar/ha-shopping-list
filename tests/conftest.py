"""Import the component without a full Home Assistant runtime.

Home Assistant is installed for these unit tests, but the integration modules
are loaded from ``custom_components`` under a short package name and the AI Task
import chain (which needs optional voice dependencies) is stubbed.
"""

import importlib.util
import sys
import types
from enum import IntFlag
from pathlib import Path

COMPONENT = Path(__file__).parents[1] / "custom_components" / "smart_shopping_route"

if "homeassistant.components.ai_task" not in sys.modules:
    ai_task_module = types.ModuleType("homeassistant.components.ai_task")

    class AITaskEntityFeature(IntFlag):
        GENERATE_DATA = 1

    ai_task_module.AITaskEntityFeature = AITaskEntityFeature
    ai_task_module.async_generate_data = None
    sys.modules["homeassistant.components.ai_task"] = ai_task_module

if "smart_shopping_route" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "smart_shopping_route",
        COMPONENT / "__init__.py",
        submodule_search_locations=[str(COMPONENT)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules["smart_shopping_route"] = package
    spec.loader.exec_module(package)
