#!/bin/sh
# Lay out the home directory (prompts, settings, token header), then become the loop so signals reach it.
set -e
python3 -m writer_service.prepare
exec python3 -m writer_service.loop
