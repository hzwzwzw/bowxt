#!/usr/bin/env bash
set -euo pipefail

wechat_url=${WECHAT_URL:-${1:-}}
wechat_sha256=${WECHAT_SHA256:-${2:-}}
wechat_version=${WECHAT_VERSION:-${3:-}}

[[ -n "$wechat_url" && -n "$wechat_sha256" && -n "$wechat_version" ]] || {
  echo "usage: WECHAT_URL=... WECHAT_SHA256=... WECHAT_VERSION=... $0" >&2
  exit 2
}
[[ $(id -u) -eq 0 ]] || { echo "install-wechat.sh must run as root" >&2; exit 1; }

package=/tmp/bowxt-wechat.deb
curl -fL --retry 3 --output "$package" "$wechat_url"
echo "$wechat_sha256  $package" | sha256sum -c -
apt-get install -y --no-install-recommends "$package"
test "$(dpkg-query -W -f='${Version}' wechat)" = "$wechat_version"
find "$package" -delete
