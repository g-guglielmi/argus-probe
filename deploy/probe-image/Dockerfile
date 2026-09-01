# Self-enrolling Zabbix active proxy for Argus.
#
# Wraps the stock Zabbix 7.0 proxy image with a first-boot enrollment step: it generates its own
# key + CSR, redeems a single-use token against Argus (/api/enroll), and writes the signed cert +
# ca.crt to the data volume - then execs the normal Zabbix proxy entrypoint. The private key is
# created inside the container and never sent anywhere.
#
# Run it with a persistent volume and the two ARGUS_ENROLL_* env vars from the Argus Probes page:
#   docker run -d --name argus-proxy-<site> --restart unless-stopped \
#     -v /docker/argus-proxy-<site>:/var/lib/zabbix \
#     -e ARGUS_ENROLL_URL=https://monitoring.example.com/api/enroll \
#     -e ARGUS_ENROLL_TOKEN=<token> \
#     ghcr.io/g-guglielmi/argus-probe:latest
FROM zabbix/zabbix-proxy-sqlite3:alpine-7.0-latest

USER root
# openssl/curl/jq: enrollment + check-in only. The proxy is a pure reporter and never touches Docker
# - all self-update work is done by the separate argus-updater sidecar (probe-watch mode), which
# holds the socket - so this image needs no docker-cli.
RUN apk add --no-cache openssl curl jq

# Bake the image version (Zabbix version + our wrapper revision, e.g. "7.0.29-r1") so the running
# probe can report it to Argus at check-in. Supplied by CI (probe-image.yml); "dev" for local builds.
ARG PROBE_VERSION=dev
RUN echo "$PROBE_VERSION" > /etc/argus-probe.version
LABEL io.argus.probe.version=$PROBE_VERSION
LABEL org.opencontainers.image.source="https://github.com/g-guglielmi/argus-probe"

COPY entrypoint.sh /usr/local/bin/argus-entrypoint.sh
RUN chmod +x /usr/local/bin/argus-entrypoint.sh

# Run as root (like the stock image expects): the entrypoint prepares the fresh, root-owned
# /var/lib/zabbix volume and chowns the enrolled certs, then hands off to the stock entrypoint,
# which fixes the spool ownership and drops to the unprivileged zabbix user to run the proxy.
ENTRYPOINT ["/usr/local/bin/argus-entrypoint.sh"]
# Overriding ENTRYPOINT clears the base image's CMD, so restore it - this is the command our
# entrypoint hands to the stock docker-entrypoint.sh, and it carries the required --foreground
# flag (mandatory with LogType=console).
CMD ["/usr/sbin/zabbix_proxy", "--foreground", "-c", "/etc/zabbix/zabbix_proxy.conf"]
