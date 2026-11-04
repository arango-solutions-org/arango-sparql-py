"""HTTP route registration for :mod:`arango_sparql.service`.

Importing this package registers every FastAPI endpoint handler on
:data:`arango_sparql.service.app.app` via decorator side-effects.
The package init does nothing else — it exists purely so the parent
package's ``__init__.py`` can do a single ``from . import routes`` to
wire the surface, and so reviewers can navigate endpoints by cluster
(``connect``, ``sparql``, ``health``).

Adding a new endpoint = drop a new ``@app.post(...)`` /
``@app.get(...)`` decorator into the appropriate cluster file (or
create a new one and import it here). Cross-cluster shared helpers
live in :mod:`..security` or :mod:`..mapping` so the route modules
stay leaf-position.
"""

from __future__ import annotations

from . import (
    connect,  # noqa: F401
    control_plane,  # noqa: F401
    examples,  # noqa: F401
    health,  # noqa: F401
    mapping,  # noqa: F401
    nl,  # noqa: F401
    protocol,  # noqa: F401
    schema,  # noqa: F401
    sparql,  # noqa: F401
)
