# A reproducible local Jenkins carrying the thin runcomposer consumer stage
# (DESIGN.md §6.2b). Build from the REPO ROOT so the single-file consumer can
# be vendored straight from the source tree — the §6.2c vendoring story:
#
#   docker build -f ci/jenkins/Dockerfile -t runcomposer-jenkins .
#   docker run --rm -p 127.0.0.1:8080:8080 runcomposer-jenkins
#
# ci/jenkins/e2e.py builds it, starts it and drives the `jenkins` runner
# against it in both completion modes. The instance is intentionally
# unsecured (local demo only — never expose it).
#
# Pinned: the base image by version and digest, every plugin by version
# (plugins.txt). Moving to a newer Jenkins is a deliberate change here.
FROM jenkins/jenkins:2.541.3-lts-jdk17@sha256:cce1eeb79902722f3cf7a8831c41e95c39f4a42b9a2981959630cc66539b3c2d

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 curl \
    && rm -rf /var/lib/apt/lists/*
# Vendor the stdlib-only consumer: one file, no pip, no venv (DESIGN.md §6.2c).
COPY --chown=jenkins:jenkins src/runcomposer_exec.py /opt/runcomposer/runcomposer_exec.py

USER jenkins
COPY --chown=jenkins:jenkins ci/jenkins/plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt
COPY --chown=jenkins:jenkins ci/jenkins/casc.yaml /usr/share/jenkins/ref/casc.yaml
ENV CASC_JENKINS_CONFIG=/usr/share/jenkins/ref/casc.yaml
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"
