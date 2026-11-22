#!/bin/sh
# Runs as root when the container starts: gives the remote user access to the
# host's Docker socket, whose group ID differs from host to host.
set -e

SOCKET=/var/run/docker.sock
REMOTE_USER=vscode

if [ -S "$SOCKET" ]; then
	gid=$(stat -c %g "$SOCKET")
	group=$(getent group "$gid" | cut -d: -f1)
	if [ -z "$group" ]; then
		group=docker-host
		groupadd --gid "$gid" "$group"
	fi
	usermod -aG "$group" "$REMOTE_USER"
fi

exec "$@"
