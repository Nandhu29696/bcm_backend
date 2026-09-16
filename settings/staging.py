"""
Staging settings.

Production-shaped, with two deliberate differences: call-tree providers run in
simulation, and outbound email is restricted to internal recipients.
"""

from .base import env
from .production import *  # noqa: F403

# Never dial real numbers from staging.
TWILIO_ENABLED = False
TEAMS_ENABLED = False
CALL_TREE_SIMULATION_MODE = True

ENABLE_API_DOCS = env.bool("ENABLE_API_DOCS", default=True)
