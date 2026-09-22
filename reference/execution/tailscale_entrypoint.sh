#!/bin/sh
set -eu

: "${TS_AUTHKEY:?TS_AUTHKEY is required to connect this Modal worker to Tailscale}"

tailscaled \
  --state=mem: \
  --tun=userspace-networking \
  --socks5-server=localhost:1080 \
  --outbound-http-proxy-listen=localhost:1080 &

tailscale up --authkey="${TS_AUTHKEY}" --hostname="modal-eval-${MODAL_TASK_ID:-worker}"
exec "$@"
