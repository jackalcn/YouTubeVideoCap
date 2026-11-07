#!/bin/sh
# Starts as root only to make the mounted folders usable, then runs the app as the ordinary user "app".
set -e

if [ "$(id -u)" != 0 ]; then
    # Started with `user:` or --user, so nothing can be fixed here; at least say why downloads would fail.
    if ! [ -w /downloads ]; then
        echo "警告：下載資料夾 /downloads 無法寫入，影片會存不進去。請在主機上執行 sudo chown $(id -u):$(id -g) downloads" >&2
    fi
    exec "$@"
fi

# New files get the owner of the downloads folder, so a NAS share keeps its owner. A folder that Docker created for
# the bind mount belongs to root, and then the default 1000 is used. PUID and PGID choose the owner explicitly.
owner_uid=$(stat -c %u /downloads)
owner_gid=$(stat -c %g /downloads)
if [ -n "$PUID" ]; then
    uid=$PUID
    gid=${PGID:-$PUID}
elif [ "$owner_uid" != 0 ]; then
    uid=$owner_uid
    gid=$owner_gid
else
    uid=1000
    gid=1000
fi

if [ "$(id -u app)" != "$uid" ] || [ "$(id -g app)" != "$gid" ]; then
    groupmod --non-unique --gid "$gid" app
    usermod --non-unique --uid "$uid" --gid "$gid" app
    chown -R app:app /home/app
fi
if ! setpriv --reuid=app --regid=app --init-groups test -w /downloads; then
    chown app:app /downloads
fi
if [ "$(stat -c %u /data)" != "$uid" ]; then
    chown -R app:app /data
fi

export HOME=/home/app
exec setpriv --reuid=app --regid=app --init-groups "$@"
