# Build stage: install the Python packages with the same Python minor version as the runtime image.
FROM python:3.13-slim-trixie AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --target /deps -r /tmp/requirements.txt \
    && find /deps -depth -name '__pycache__' -exec rm -rf {} + \
    && mkdir -p /data

# Runtime: distroless Python (no shell, package manager or system tools), running as a non-root user.
# Fewer packages means fewer CVEs and less for an attacker to use.
FROM gcr.io/distroless/python3-debian13:nonroot

ARG APP_VERSION=dev
ARG APP_COMMIT=
ENV APP_VERSION=${APP_VERSION} \
    APP_COMMIT=${APP_COMMIT} \
    PYTHONPATH=/srv/app/deps \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Chicago \
    # Which proxies may say who the visitor is (X-Forwarded-For/Proto). "*" trusts any; set it to your
    # proxy's address (e.g. 172.18.0.0/16) in .env if port 8080 is reachable without the proxy.
    FORWARDED_ALLOW_IPS=*

WORKDIR /srv/app
COPY --from=build /deps ./deps
COPY app ./app
# uid 10001 matches earlier images, so existing data folders keep working
COPY --from=build --chown=10001:10001 /data /data
USER 10001

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD ["/usr/bin/python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]

ENTRYPOINT ["/usr/bin/python3", "-m", "uvicorn"]
CMD ["app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
