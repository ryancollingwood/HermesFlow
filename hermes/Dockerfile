# Thin derived image: bake LAZY_DEPS extras into the otherwise-immutable Hermes
# venv. Installs run as root at BUILD time (root bypasses the venv's a-w bits),
# then the venv is re-locked so the RUNTIME posture is identical to upstream:
# read-only, root-owned venv; the agent (uid 501 hermes) gains no write access.
#
# See hermes/README.md for the why, and hermes/requirements.txt for the packages.
ARG HERMES_VERSION=latest
FROM nousresearch/hermes-agent:${HERMES_VERSION}

USER root

COPY requirements.txt /tmp/hermes-extra-requirements.txt

# uv is on PATH (/usr/local/bin/uv); VIRTUAL_ENV targets the venv exactly as
# lazy_deps._venv_pip_install does. Deps already present in the venv are reused
# (verified via `uv pip install --dry-run`), so only genuinely-new packages are
# added — no version shadowing of the base image's pinned dependencies.
RUN VIRTUAL_ENV=/opt/hermes/.venv \
    uv pip install --no-cache -r /tmp/hermes-extra-requirements.txt && \
    rm -f /tmp/hermes-extra-requirements.txt && \
    # Re-apply the base image's hardening to anything uv created.
    chown -R root:root /opt/hermes/.venv && \
    chmod -R a-w /opt/hermes/.venv

# USER and ENTRYPOINT are inherited from the base image (USER=root, /init drops
# to the hermes user at runtime). Do not override them.
