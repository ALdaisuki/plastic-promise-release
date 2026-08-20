# syntax=docker/dockerfile:1
# The local edge is deliberately a static, non-authoritative dashboard edge.
# It has no Python runtime, canonical state, model cache, credential, socket,
# reverse tunnel, or backend proxy configuration. Browser-side MCP access is
# supplied separately through the existing authenticated local client path.
ARG BASE_IMAGE
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

LABEL org.opencontainers.image.title="Plastic Promise local edge" \
      org.opencontainers.image.description="Static local dashboard edge with no canonical authority" \
      org.opencontainers.image.source="${SOURCE_REPOSITORY}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${PACKAGE_VERSION}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}" \
      org.opencontainers.image.base.digest="${BASE_IMAGE_DIGEST}" \
      org.opencontainers.image.licenses="MIT" \
      org.plastic-promise.endpoint.role="pp-local-edge" \
      org.plastic-promise.endpoint.variant="standard" \
      org.plastic-promise.endpoint.contract="plastic-promise-endpoint-contract/v1" \
      org.plastic-promise.build.policy-digest="${BUILD_POLICY_DIGEST}" \
      org.plastic-promise.build.recipe-policy-digest="${RECIPE_POLICY_DIGEST}" \
      org.plastic-promise.authority="local-edge,bounded-awareness-display,bounded-event-submission"

ENV TZ=UTC

USER root
COPY deploy/local-edge/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/local-edge/entrypoint.sh /usr/local/bin/plastic-promise-local-edge
COPY plastic_promise/mcp/dashboard_v2/static/ /usr/share/nginx/html/

RUN chmod 0555 /usr/local/bin/plastic-promise-local-edge

USER 101
EXPOSE 8080
ENTRYPOINT ["plastic-promise-local-edge"]
