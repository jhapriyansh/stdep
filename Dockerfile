FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

ENV STATICDEPLOY_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8080

CMD ["sh", "-c", "staticdeploy --data-dir ${STATICDEPLOY_DATA_DIR} serve --host 0.0.0.0 --port 8080 --admin-token \"${STATICDEPLOY_ADMIN_TOKEN}\" --preview-suffix \"${STATICDEPLOY_PREVIEW_SUFFIX}\""]
