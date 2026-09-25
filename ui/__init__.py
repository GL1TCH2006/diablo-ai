from .bridge import AgentBridge, AgentBridgeError, CoreEventBusBridge, MockAgentBridge
from .companion import FloatingCompanionApp, create_companion_app, run_companion
from .controller import UIController
from .models import AgentEvent, UIActionResult, UIActivity, UIControllerError, UIStatus

__all__ = [
    "TerminalUI",
    "AgentBridge",
    "AgentBridgeError",
    "CoreEventBusBridge",
    "MockAgentBridge",
    "UIController",
    "UIStatus",
    "AgentEvent",
    "UIActivity",
    "UIActionResult",
    "UIControllerError",
    "FloatingCompanionApp",
    "create_companion_app",
    "run_companion",
]


def __getattr__(name):
    if name == "TerminalUI":
        from .terminal import TerminalUI

        return TerminalUI
    raise AttributeError(name)
