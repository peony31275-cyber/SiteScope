# SiteScope's public entry (Phase 11, ADR-0025): nginx serves the built dashboard and forwards
# /api/ to the API on the same origin.
#
#   docker build -t sitescope-frontend:local frontend
#
# The build stage type-checks and bundles the dashboard. The runtime image holds only nginx, its
# configuration and the static files: no Node.js, no sources, no secrets (a VITE_ variable is
# public by design, and none is set here).

ARG NODE_IMAGE=node:24.14.0-trixie-slim
ARG NGINX_IMAGE=nginx:1.30.5-alpine

# --- Build ---------------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html vite.config.ts tsconfig.json tsconfig.app.json tsconfig.node.json ./
COPY public public
COPY src src
RUN npm run build

# --- Runtime -------------------------------------------------------------------------------
FROM ${NGINX_IMAGE} AS runtime
LABEL org.opencontainers.image.title="sitescope-frontend" \
      org.opencontainers.image.description="SiteScope dashboard behind nginx, with /api/ proxied to the API" \
      org.opencontainers.image.licenses="MIT"
# The image's default site is replaced, not extended. conf.d stays empty: the HTTPS variant
# mounts real-ip.conf there (deploy/compose.https.yaml).
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY --from=build /src/dist /usr/share/nginx/html
# The master process runs as the unprivileged nginx user (uid 101) on port 8080; temp files and
# the pid file live in /tmp, so the root filesystem can be read-only.
USER 101:101
EXPOSE 8080
STOPSIGNAL SIGQUIT
CMD ["nginx", "-g", "daemon off;"]
