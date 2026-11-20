# runcomposer — self-hosted image (DESIGN.md §14 quickstart).
# The UI is pre-bundled in the repo (src/runcomposer/ui_dist), so this build
# needs no Node toolchain — pip install is the whole build.
FROM python:3.13-slim

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

ENTRYPOINT ["runcomposer"]
CMD ["serve", "--host", "0.0.0.0"]
