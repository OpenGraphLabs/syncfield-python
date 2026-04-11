"""Camera and sensor auto-discovery for SyncField.

The discovery package is the bridge between "what hardware is attached
to this machine?" and the :class:`~syncfield.SessionOrchestrator`. Four
public workflows are supported:

1. **One-liner auto-setup** (scripts / prototypes)::

       import syncfield as sf
       import syncfield.discovery

       session = sf.SessionOrchestrator(host_id="rig_01", output_dir="./data")
       sf.discovery.scan_and_add(session)
       session.start()

2. **Inspect first, then curate**::

       report = sf.discovery.scan()
       for device in report.devices:
           print(device.display_name, device.adapter_type, device.device_id)

       # Pick the ones you want
       cam = next(d for d in report.devices if "FaceTime" in d.display_name)
       session.add(cam.construct(id="cam_main", output_dir="./data"))

3. **Viewer-driven GUI** — open an empty session in the bundled viewer
   and click "Discover devices" in the header. The viewer calls
   :func:`scan` on a worker thread, shows results in a modal, and wires
   up :func:`scan_and_add` on user confirmation. Requires the ``viewer``
   extra — see :mod:`syncfield.viewer`.

4. **Explicit construction** — don't use discovery at all. Preferred for
   production where device identity must be reproducible::

       session.add(UVCWebcamStream("cam", device_index=0, output_dir="./data"))

See :doc:`/sdk/discovery` for the full decision tree and per-workflow
examples.

Architecture
------------
Each Stream adapter that supports discovery implements a ``@classmethod
discover(cls, *, timeout)`` returning a list of :class:`DiscoveredDevice`
instances. The :func:`register_discoverer` call in
:mod:`syncfield.adapters.__init__` wires each adapter into the module-
level registry at import time. :func:`scan` walks the registry, fans
out to every adapter in a thread pool, and aggregates the results into
an immutable :class:`DiscoveryReport`.

Third-party adapters register themselves the same way — nothing privileged
about the shipped classes.
"""

from __future__ import annotations

from syncfield.discovery._id_gen import make_stream_id, normalize
from syncfield.discovery.registry import (
    clear_registry,
    iter_discoverers,
    register_discoverer,
    unregister_discoverer,
)
from syncfield.discovery.scanner import (
    clear_scan_cache,
    scan,
    scan_and_add,
)
from syncfield.discovery.types import DiscoveredDevice, DiscoveryReport

__all__ = [
    # Data model
    "DiscoveredDevice",
    "DiscoveryReport",
    # Primitives
    "scan",
    "scan_and_add",
    # Registry (for custom adapters)
    "register_discoverer",
    "unregister_discoverer",
    "iter_discoverers",
    "clear_registry",
    # Id generation (public because users may want collision-free ids
    # outside the scan_and_add path).
    "make_stream_id",
    "normalize",
    # Test hook
    "clear_scan_cache",
]
