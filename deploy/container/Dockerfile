# PatchTrack service image.
#
# One image serves all five services; the compose file selects behaviour with
# APP_DIR. Keeping a single image means the exact same dependency set is used
# for identity, inventory, compliance, reporting and gateway, so a CVE fix or
# upgrade lands everywhere at once.
#
# Build from the repository root:
#
#   docker build -f deploy/container/Dockerfile -t patchtrack:3.0.4 .
#
# The image runs as an unprivileged user, has no build tools, and contains no
# credentials. It also works unmodified under SELinux on RHEL/Rocky 9: nothing
# writes to the image filesystem at runtime except /tmp, and the mounted
# volumes are labelled by the compose file.

# Pin the base image. `:latest` is republished without notice, so two builds
# of the same commit could ship different interpreters and libraries.
ARG PYTHON_IMAGE=registry.access.redhat.com/ubi9/python-312:1-1762230134

FROM ${PYTHON_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/patchtrack/common:/opt/patchtrack/services

LABEL org.opencontainers.image.title="PatchTrack" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

WORKDIR /opt/patchtrack

# Install from the hashed lock so transitive wheels cannot drift between
# builds. requirements.txt remains the readable list of direct pins; regenerate
# the lock with pip-compile after changing that list (CI checks they agree).
COPY deploy/container/requirements.txt /opt/patchtrack/requirements.txt
COPY deploy/container/requirements.lock.txt /opt/patchtrack/requirements.lock.txt
RUN pip install --no-cache-dir --require-hashes \
        -r /opt/patchtrack/requirements.lock.txt

COPY LICENSE /opt/patchtrack/LICENSE
COPY services/common /opt/patchtrack/common
COPY services /opt/patchtrack/services
COPY deploy/postgres /opt/patchtrack/deploy/postgres
COPY deploy/container/entrypoint.sh /opt/patchtrack/entrypoint.sh

# Red Hat's UBI python image sets USER 1001, unlike the official python image,
# so a RUN here would otherwise execute unprivileged and fail to chmod/chown
# the files just copied (owned by root). Elevated only for this step; the
# image drops back to 1001 below.
USER root
RUN chmod 0755 /opt/patchtrack/entrypoint.sh \
    && mkdir -p /var/lib/patchtrack/reports /var/lib/patchtrack/solaris-inbox \
                 /var/lib/patchtrack/tls \
    && chown -R 1001:0 /var/lib/patchtrack /opt/patchtrack

# UID 1001 is the unprivileged user shipped in the UBI python image. Running
# as a numeric uid keeps OpenShift-compatible random-uid behaviour and avoids
# needing a passwd entry.
USER 1001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,ssl,sys,urllib.request; \
port=os.environ.get('PORT','8000'); \
tls=(os.environ.get('PATCHTRACK_SERVICE')=='gateway' \
     and os.environ.get('PATCHTRACK_TLS_ENABLED','').lower() in ('1','true','yes','on')); \
url=('https' if tls else 'http')+f'://127.0.0.1:{port}/healthz'; \
ctx=ssl._create_unverified_context() if tls else None; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4, context=ctx).status==200 else 1)"

ENTRYPOINT ["/opt/patchtrack/entrypoint.sh"]
