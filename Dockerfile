# runcomposer — self-hosted image (DESIGN.md §14 quickstart).
# The UI is pre-bundled in the repo (src/runcomposer/ui_dist), so this build
# needs no Node toolchain — pip install is the whole build.
#
# Pinned: the distribution release of the base image (trixie) and the
# version of the one package installed from it, so a rebuild installs the
# same init.
# Moving to a newer release is a deliberate edit of both lines.
FROM python:3.13-slim-trixie

# An init process as PID 1: it forwards signals to runcomposer and reaps
# the processes a stopped worker leaves behind, which would otherwise stay
# as zombie entries until the container exits.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini=0.19.0-3+b8 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# The process does not run as root. Runtime state (sqlite store, artifacts,
# the demo workspace) lives under /data, owned by that user; mount a volume
# there to persist it. The container config is the built-in default unless
# /data has a config.yaml.
RUN useradd --create-home --uid 10001 --user-group runcomposer \
    && mkdir -p /data \
    && chown runcomposer:runcomposer /data
USER runcomposer
WORKDIR /data
EXPOSE 8100

ENTRYPOINT ["/usr/bin/tini", "--", "runcomposer"]
CMD ["serve", "--host", "0.0.0.0"]
