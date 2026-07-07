# Jenkins demo instance — the §6.2b consumer stage

A reproducible local Jenkins that carries the **thin CI-side consumer**
runcomposer's `ci-trigger` runner drives (DESIGN.md §6.2b): a parameterized
job whose build step runs the vendored single-file `runcomposer-exec` on the
passed spec, and whose post step POSTs the results bundle (marker + per-run
ingest token) back to `/api/v1/runs/{id}/results`.

```bash
# from the repo root
docker build -f ci/jenkins/Dockerfile -t runcomposer-jenkins .
docker run --rm -d -p 8080:8080 --name runcomposer-jenkins runcomposer-jenkins
# job is provisioned by JCasC + job-dsl; ready when this answers:
curl -sf localhost:8080/job/runcomposer-consumer/api/json
```

runcomposer side (`config.yaml`):

```yaml
runners:
  ci-trigger:
    base_url: http://localhost:8080
    job: runcomposer-consumer
    callback_base: http://host.docker.internal:8100   # how the container reaches your serve
    completion: callback        # or "poll" for CI systems that can't call out
```

Then: `runcomposer serve --host 0.0.0.0` (so the container can call back),
compose a spec, and `runcomposer dispatch --runner ci-trigger spec.json`.
Completion arrives via the job's webhook-out POST; with `completion: poll`
runcomposer instead polls the build API and ingests the archived artifacts.

The instance is **unsecured by design** (local demo). Never expose it.
