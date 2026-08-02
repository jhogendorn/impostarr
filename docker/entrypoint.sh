#!/bin/sh
# Deliberately dumb: create_app() (src/impostarr/main.py) already runs
# ensure_external_plugins()/activate_plugin_overlay() at startup, so there
# is nothing to do here but launch the app.
set -eu

exec uvicorn --factory impostarr.main:app --host 0.0.0.0 --port 8484
