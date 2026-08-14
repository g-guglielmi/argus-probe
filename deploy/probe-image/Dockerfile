# Self-enrolling Zabbix active proxy for Argus.
#
# Wraps the stock Zabbix 7.0 proxy image with a first-boot enrollment step: it generates its own
# key + CSR, redeems a single-use token against Argus (/api/enroll), and writes the signed cert +
# ca.crt to the data volume — then execs the normal Zabbix proxy entrypoint. The private key is
# created inside the container and never sent anywhere.
#
# Run it with a persistent volume and the two ARGUS_ENROLL_* env vars from the Argus Probes page:
#   docker run -d --name argus-probe --restart unless-stopped \
#     -v /docker/argus-probe:/var/lib/zabbix \
#     -e ARGUS_ENROLL_URL=https://monitoring.example.com/api/enroll \
#     -e ARGUS_ENROLL_TOKEN=<token> \
#     ghcr.io/g-guglielmi/argus-probe:latest
FROM zabbix/zabbix-proxy-sqlite3:alpine-7.0-latest

USER root
RUN apk add --no-cache openssl curl jq
COPY entrypoint.sh /usr/local/bin/argus-entrypoint.sh
RUN chmod +x /usr/local/bin/argus-entrypoint.sh
USER 1997

ENTRYPOINT ["/usr/local/bin/argus-entrypoint.sh"]
