# The demo site as a container (Phase 11, ADR-0025): the public demo's only approved crawl
# target, reachable on the stack's internal network as http://demo-site:8080. Its pages are
# synthetic, written for SiteScope (README.md).
#
#   docker build -t sitescope-demo-site:local demo-site

ARG NGINX_IMAGE=nginx:1.30.5-alpine

FROM ${NGINX_IMAGE}
LABEL org.opencontainers.image.title="sitescope-demo-site" \
      org.opencontainers.image.description="SiteScope's synthetic demo website (a crawl target)" \
      org.opencontainers.image.licenses="MIT"
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/nginx.conf
COPY robots.txt *.html /usr/share/nginx/html/
COPY blog /usr/share/nginx/html/blog
COPY docs /usr/share/nginx/html/docs
COPY private /usr/share/nginx/html/private
USER 101:101
EXPOSE 8080
STOPSIGNAL SIGQUIT
CMD ["nginx", "-g", "daemon off;"]
