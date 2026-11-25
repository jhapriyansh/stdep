FROM python:3.13-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install --no-install-recommends --yes git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 staticdeploy
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .
RUN mkdir /data && chown staticdeploy:staticdeploy /data

ENV STATICDEPLOY_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8080
USER staticdeploy

CMD ["sh", "-c", "staticdeploy --data-dir ${STATICDEPLOY_DATA_DIR} serve --host 0.0.0.0 --port 8080 --admin-token \"${STATICDEPLOY_ADMIN_TOKEN}\" --preview-suffix \"${STATICDEPLOY_PREVIEW_SUFFIX}\" --routing-mode \"${STATICDEPLOY_ROUTING_MODE:-subdomain}\" --secure-cookies"]
