#!/bin/bash
# Debian's Bash TCP builtin avoids installing an unpinned HTTP utility.
# Docker's five-second health timeout bounds connect/read. Metrics stay private.
set -eu
exec 3<>/dev/tcp/127.0.0.1/9598
printf 'GET /metrics HTTP/1.0\r\nHost: localhost\r\n\r\n' >&3
IFS= read -r -n 128 status <&3
case "$status" in
  'HTTP/1.1 200'*|'HTTP/1.0 200'*) exit 0 ;;
  *) exit 1 ;;
esac
