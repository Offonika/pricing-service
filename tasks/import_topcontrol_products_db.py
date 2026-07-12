"""Deprecated compatibility entrypoint for the direct 1C catalog sync.

TopControl is no longer an active source.  Keep this module for one release so
installed schedules using the old module name continue to work while emitting a
clear migration warning.
"""

from __future__ import annotations

import logging
import warnings

from tasks.sync_onec_product_catalog import *  # noqa: F403
from tasks.sync_onec_product_catalog import main as _sync_onec_main


def main() -> None:
    message = (
        "tasks.import_topcontrol_products_db is deprecated; "
        "use tasks.sync_onec_product_catalog"
    )
    warnings.warn(message, DeprecationWarning, stacklevel=2)
    logging.getLogger(__name__).warning(message)
    _sync_onec_main()


if __name__ == "__main__":
    main()
