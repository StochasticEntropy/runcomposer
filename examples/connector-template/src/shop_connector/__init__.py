"""shop_connector — an example runcomposer connector.

Three plugins, each a few dozen lines, importing nothing of runcomposer but
``runcomposer.sdk``:

- ``http-checks`` source (:mod:`.checks_source`) — the catalog: HTTP checks
  described in a YAML file, each one a test item with tags.
- ``http-checks`` runner (:mod:`.http_runner`) — executes the planned checks
  against a web shop with a pool of threads in this process and delivers
  correlated verdicts; streams live status, honours cancellation, probes
  the shop for its health, and lets the person starting a run pick the
  environment.
- ``run-summary`` observer (:mod:`.summary_observer`) — appends one line per
  finished run to a text file.

The settings the source and runner share (environments, a user agent) live
under ``connectors.shop-checks`` in the config file.
"""

__version__ = "0.1.0"
