#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
config_home=${XDG_CONFIG_HOME:-${HOME:?}/.config}
install -d -m 0700 "$config_home/fcitx5" "$config_home/environment.d"
if [[ ! -e "$config_home/fcitx5/profile" ]]; then
  install -m 0600 "$project_dir/assets/fcitx5/profile" "$config_home/fcitx5/profile"
fi
if [[ ! -e "$config_home/environment.d/90-bowxt-input-method.conf" ]]; then
  install -m 0600 "$project_dir/assets/fcitx5/im.conf" \
    "$config_home/environment.d/90-bowxt-input-method.conf"
fi
echo "fcitx5 pinyin defaults installed under $config_home"
