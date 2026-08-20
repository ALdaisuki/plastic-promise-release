# syntax=docker/dockerfile:1.7

# ``BASE_IMAGE`` and ``COMPUTE_VARIANT`` form an explicit CPU/CUDA build
# matrix. The caller must supply the immutable base reference declared in
# deploy/oci-base-images.json. Host Ollama is intentionally not embedded: it
# remains an external compatibility adapter rather than a model/runtime
# dependency of this image. The node's supported local default is llama.cpp,
# whose OpenAI-compatible embedding and rerank servers are operator-managed
# peers. Do not install the optional in-process CrossEncoder stack here: it
# pulls PyTorch/Triton/CUDA Python wheels into an image that does not execute
# those adapters.
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS compute-package
ARG PACKAGE_VERSION

# Stage the complete source tree only as compiler input.  The materializer
# emits the closed compute allowlist; no full source tree crosses into the
# final image.
WORKDIR /source
COPY pyproject.toml README.md /source/
COPY plastic_promise /source/plastic_promise
RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m plastic_promise.role_package \
        --role pp-compute-node \
        --source-root /source \
        --output-root /role-package \
        --version "$PACKAGE_VERSION"

FROM ${BASE_IMAGE}

ARG BASE_IMAGE
ARG BASE_IMAGE_DIGEST
ARG SOURCE_REPOSITORY=https://github.com/ALdaisuki/plastic-promise
ARG SOURCE_REVISION
ARG PACKAGE_VERSION
ARG COMPUTE_VARIANT
ARG BUILD_POLICY_DIGEST
ARG RECIPE_POLICY_DIGEST
ARG PIP_INDEX_URL="https://pypi.org/simple"

RUN test -n "$SOURCE_REVISION" \
    && test "$SOURCE_REVISION" != "unknown" \
    && test -n "$PACKAGE_VERSION" \
    && test "$PACKAGE_VERSION" != "unknown" \
    && test -n "$COMPUTE_VARIANT" \
    && test -n "$BUILD_POLICY_DIGEST" \
    && test -n "$RECIPE_POLICY_DIGEST" \
    && test -n "$BASE_IMAGE_DIGEST" \
    && test "$BASE_IMAGE_DIGEST" = "${BASE_IMAGE##*@}" \
    && case "$BASE_IMAGE" in *@sha256:*) ;; *) exit 64 ;; esac

LABEL org.opencontainers.image.title="Plastic Promise compute node" \
      org.opencontainers.image.description="Bounded local embedding and rerank node" \
      org.opencontainers.image.source="${SOURCE_REPOSITORY}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${PACKAGE_VERSION}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}" \
      org.opencontainers.image.base.digest="${BASE_IMAGE_DIGEST}" \
      org.opencontainers.image.licenses="MIT" \
      org.plastic-promise.endpoint.role="pp-compute-node" \
      org.plastic-promise.authority="compute-execution" \
      org.plastic-promise.endpoint.variant="${COMPUTE_VARIANT}" \
      org.plastic-promise.compute.variant="${COMPUTE_VARIANT}" \
      org.plastic-promise.endpoint.contract="plastic-promise-endpoint-contract/v1" \
      org.plastic-promise.compute.capabilities="embedding/v1,rerank/v1,structured-json/v1" \
      org.plastic-promise.compute.package-manifest="plastic-promise-compute-package-manifest/v1" \
      org.plastic-promise.build.policy-digest="${BUILD_POLICY_DIGEST}" \
      org.plastic-promise.build.recipe-policy-digest="${RECIPE_POLICY_DIGEST}" \
      org.plastic-promise.model.source="operator-mounted-read-only"

ENV TZ=UTC \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CC=gcc \
    CXX=g++

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        python3 python3-dev python3-pip python3-venv gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=compute-package /role-package /app
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python3 -m venv /opt/plastic-promise-venv \
    && /opt/plastic-promise-venv/bin/python -m pip install \
        --retries 10 --timeout 60 \
        --index-url "${PIP_INDEX_URL}" . \
    && rm -rf /app/plastic_promise /app/build

ENV PATH="/opt/plastic-promise-venv/bin:${PATH}"

RUN useradd --create-home --shell /usr/sbin/nologin ppnode \
    && install --directory --owner=ppnode --group=ppnode \
        /var/lib/plastic-promise/compute-node
USER ppnode

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import os; from urllib.request import Request, urlopen; request = Request('http://127.0.0.1:19130/health', headers={'Authorization': os.environ['PP_LOCAL_NODE_AUTHORIZATION']}); urlopen(request, timeout=3).read()"

ENTRYPOINT ["plastic-promise-local-inference-node"]
