# A reproducible local Jenkins carrying the thin runcomposer consumer stage
# (DESIGN.md §6.2b). Build from the REPO ROOT so the single-file consumer can
# be vendored straight from the source tree — the §6.2c vendoring story:
#
#   docker build -f ci/jenkins/Dockerfile -t runcomposer-jenkins .
#   docker run --rm -p 8080:8080 runcomposer-jenkins
#
# The instance is intentionally unsecured (local demo only — never expose it).
FROM jenkins/jenkins:lts-jdk17

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 curl \
    && rm -rf /var/lib/apt/lists/*
# Vendor the stdlib-only consumer: one file, no pip, no venv (DESIGN.md §6.2c).
COPY --chown=jenkins:jenkins src/runcomposer_exec.py /opt/runcomposer/runcomposer_exec.py

USER jenkins
RUN jenkins-plugin-cli --plugins "configuration-as-code job-dsl"
COPY --chown=jenkins:jenkins ci/jenkins/casc.yaml /usr/share/jenkins/ref/casc.yaml
ENV CASC_JENKINS_CONFIG=/usr/share/jenkins/ref/casc.yaml
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"
