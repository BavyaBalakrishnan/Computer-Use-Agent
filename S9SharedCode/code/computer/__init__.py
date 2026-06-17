"""Session 9 Computer-Use skill package.

Desktop counterpart to the `browser` package: drives native + Electron apps
through cua-driver across the same extract → deterministic → a11y → vision
cascade. See skill.py for the cascade wrapper and cua.py for the driver
wrapper.
"""
from .skill import ComputerSkill

__all__ = ["ComputerSkill"]
