"""The application layer: everything between the core's pure domain and the
transports (HTTP, CLI).

Split by responsibility — ``config``, ``catalog``, ``compose``, ``ledger``
(the one writer of lifecycle state), ``dispatching``, ``ingestion``,
``quarantine``, ``inbox``, ``artifacts``, ``housekeeping``, ``eventbus``,
``recovery`` — with ``service.Service`` as a thin facade over them.

This package is deliberately kept import-free: a worker process that only
needs ``runcomposer.app.livechannel`` must not pull in the rest.
"""
