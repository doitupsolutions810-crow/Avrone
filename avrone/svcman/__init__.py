"""Built-in service supervisor for AVRONE.

Runs Ollama, the AVRONE server, squad lanes, and optional sibling services
without systemd or launchd. Safe as pid 1 and under tini.
"""

__version__ = "1"

from .spec import SpecError, load_config
from .supervisor import Supervisor, SupervisorError

__all__ = ["SpecError", "Supervisor", "SupervisorError", "load_config", "__version__"]
