#!/bin/sh
# The image intentionally stays root until here (no USER directive in the
# Dockerfile) so PUID/PGID remapping is possible; this always drops
# privileges via gosu before exec'ing the app — the app itself never runs
# as root, even in the default (PUID/PGID unset) case.
#
# *arr-style PUID/PGID support: when set, the built-in `impostarr` user
# (uid/gid 1000) is remapped to match before dropping privileges, so it can
# write to bind-mounted volumes owned by an arbitrary host uid/gid. See
# README for the matching host-side setup.
#
# Otherwise deliberately dumb: create_app() (src/impostarr/main.py) already
# runs ensure_external_plugins()/activate_plugin_overlay() at startup, so
# there is nothing else to do here but launch the app.
set -eu

if [ -n "${PUID:-}" ] || [ -n "${PGID:-}" ]; then
    groupmod -o -g "${PGID:-1000}" impostarr
    usermod -o -u "${PUID:-1000}" impostarr
fi

exec gosu impostarr uvicorn --factory impostarr.main:app --host 0.0.0.0 --port 8484
