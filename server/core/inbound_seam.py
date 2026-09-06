"""AD-2 composition root: the ONE file in ``server/core`` that names `inbound`.

AD-2 / E38-NFR14 (``inbound/__init__.py``, ``inbound/README.md``): *"``server/core``
must never import this package's provider names."* Before this module, seven core
call sites did -- three distinct modules under ``server/inbound/adapters/`` --
spread across ``datastream_setup_observations.py``,
``datastream_preconfiguration_api.py``, ``queue.py`` and ``scheduler.py``.

They were written through ``importlib`` with the package path split across an
f-string. The split changes no runtime behaviour whatsoever; its only effect is
that the AD-2 scanner stops seeing the token. So the boundary read green while
core depended on three provider modules by name. The scanner now resolves
``importlib.import_module`` targets too (rule B bis in
``scripts/check_core_source_agnostic.py``), and this file is one of its named,
adjudicated exceptions -- the composition root, not a loophole.

What core is allowed to know is a CAPABILITY NAME. It never learns which module,
and never a provider, answers it -- see ``inbound/capabilities.py``. That keeps
"the provider is a deploy-time choice" true in code rather than only in the
README.
"""

from __future__ import annotations

import importlib
from typing import Any

#: The only inbound module core may reach, and it carries no provider name.
_CAPABILITY_INDEX = "inbound.capabilities"


def resolve_inbound(capability: str) -> Any:
    """Return the inbound-published callable/class registered under ``capability``.

    Raises ``UnknownInboundCapability`` (a ``KeyError``) for an unpublished name,
    so a typo fails at the call site instead of arriving downstream disguised as
    an adapter that produced no evidence.
    """
    return importlib.import_module(_CAPABILITY_INDEX).resolve(capability)
