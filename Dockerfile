# runcomposer — self-hosted image (DESIGN.md §14 quickstart).
# The UI is pre-bundled in the repo (src/runcomposer/ui_dist), so this build
# needs no Node toolchain — pip install is the whole build.
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Runtime state (sqlite store, artifacts) lives under /data; mount a volume
# to persist it. The container config is the built-in default unless /data
# has a config.yaml.
WORKDIR /data
EXPOSE 8100

ENTRYPOINT ["runcomposer"]
CMD ["serve", "--host", "0.0.0.0"]
