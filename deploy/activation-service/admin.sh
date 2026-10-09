#!/bin/sh
set -eu
exec docker exec -i wechatrebot-activation python -m services.activation_service --data-dir /data "$@"
