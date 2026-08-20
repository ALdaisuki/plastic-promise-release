# syntax=docker/dockerfile:1
# This image contains application code only. SQLite, LanceDB, models, logs,
# environment files, and all credentials are runtime-mounted operator state.
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS server-package
ARG PACKAGE_VERSION

WORKDIR /source
COPY pyproject.toml README.md LICENSE /source/
COPY plastic_promise /source/plastic_promise
RUN python -m plastic_promise.role_package \
        --role pp-server-backend \
        --source-root /source \
        --output-root /role-package \
        --version "$PACKAGE_VERSION"

FROM ${BASE_IMAGE}

ARG BASE_IMAGE
ARG BASE_IMAGE_DIGEST
ARG SOURCE_REPOSITORY=https://github.com/ALdaisuki/plastic-promise
ARG SOURCE_REVISION
ARG PACKAGE_VERSION
ARG BUILD_POLICY_DIGEST
ARG RECIPE_POLICY_DIGEST

RUN test -n "$SOURCE_REVISION" \
    && test "$SOURCE_REVISION" != "unknown" \
    && test -n "$PACKAGE_VERSION" \
    && test "$PACKAGE_VERSION" != "unknown" \
    && test -n "$BUILD_POLICY_DIGEST" \
    && test -n "$RECIPE_POLICY_DIGEST" \
    && test -n "$BASE_IMAGE_DIGEST" \
    && test "$BASE_IMAGE_DIGEST" = "${BASE_IMAGE##*@}" \
    && case "$BASE_IMAGE" in *@sha256:*) ;; *) exit 64 ;; esac

LABEL org.opencontainers.image.title="Plastic Promise server backend" \
      org.opencontainers.image.description="Canonical SQLite-owning MCP governance runtime" \
      org.opencontainers.image.source="${SOURCE_REPOSITORY}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${PACKAGE_VERSION}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}" \
      org.opencontainers.image.base.digest="${BASE_IMAGE_DIGEST}" \
      org.opencontainers.image.licenses="MIT" \
      org.plastic-promise.endpoint.role="pp-server-backend" \
      org.plastic-promise.endpoint.variant="standard" \
      org.plastic-promise.endpoint.contract="plastic-promise-endpoint-contract/v1" \
      org.plastic-promise.build.policy-digest="${BUILD_POLICY_DIGEST}" \
      org.plastic-promise.build.recipe-policy-digest="${RECIPE_POLICY_DIGEST}" \
      org.plastic-promise.authority="agent-registry-authority,work-board-authority,canonical-memory-authority,collaboration-event-writer" \
      org.plastic-promise.server.source-exclusions="plastic_promise/core/backend_inference.py,plastic_promise/core/embedder.py,plastic_promise/core/inference_provider.py,plastic_promise/core/inference_jobs.py,plastic_promise/core/provider_http.py,plastic_promise/core/reranker.py,plastic_promise/client/local_rerank_executor.py,plastic_promise/local_inference_node,plastic_promise/mcp/inference_gateway.py,plastic_promise/mcp/inference_gateway_server.py"

ENV TZ=UTC \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --from=server-package /role-package /app
RUN python -m pip install --no-cache-dir . \
    && python -c "import shutil,site; shutil.move('/app/role-package.receipt.json', site.getsitepackages()[0] + '/role-package.receipt.json')" \
    && rm -rf /app/plastic_promise /app/build \
    && useradd --create-home --shell /usr/sbin/nologin ppruntime

USER ppruntime
EXPOSE 9020
ENTRYPOINT ["plastic-promise-canonical-runtime"]
