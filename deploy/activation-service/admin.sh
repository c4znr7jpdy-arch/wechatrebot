#!/bin/sh
set -eu
if [ -t 0 ] && [ -t 1 ]; then
    exec docker exec -it wechatrebot-activation python -m services.activation_service --data-dir /data "$@"
fi
exec docker exec -i wechatrebot-activation python -m services.activation_service --data-dir /data "$@"
