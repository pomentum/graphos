# syntax=docker/dockerfile:1.7
# graphos UI: a static app served by nginx, which also forwards /api to graphos.

FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json vite.config.ts postcss.config.cjs index.html ./
COPY public ./public
COPY src ./src
RUN npm run build && test -s dist/index.html

FROM nginxinc/nginx-unprivileged:1.29-alpine
LABEL org.opencontainers.image.source="https://github.com/pomentum/graphos" \
      org.opencontainers.image.description="Web UI for graphos" \
      org.opencontainers.image.licenses="Apache-2.0"
# Where graphos answers, as seen from this container. It is looked up per request
# through the container's DNS, which the nginx entrypoint exposes to the config
# as NGINX_LOCAL_RESOLVERS when NGINX_ENTRYPOINT_LOCAL_RESOLVERS is set.
ENV GRAPHOS_API_UPSTREAM=http://graphos-api:8080 \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
COPY deploy/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/licenses/graphos-ui/
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
