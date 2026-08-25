#!/usr/bin/env bash

set -eEuo pipefail

if [[ $# -eq 0 ]]; then
    echo "sccache wrapper: expected compiler arguments" >&2
    exit 99
fi

: "${SCCACHE:?sccache wrapper: SCCACHE is not set}"
if [[ ! -f "$SCCACHE" || ! -x "$SCCACHE" ]]; then
    echo "sccache wrapper: SCCACHE is not an executable file" >&2
    exit 99
fi

tool=${0##*/}
wrapper_dir=$(cd "${0%/*}" && pwd -P)
clean_path=
IFS=: read -ra path_entries <<< "${PATH:-/usr/bin:/bin}"
for entry in "${path_entries[@]}"; do
    canonical=$entry
    if [[ -d "$entry" ]]; then
        canonical=$(cd "$entry" && pwd -P)
    fi
    entry_name=${entry%/}
    entry_name=${entry_name##*/}
    if [[ "$canonical" != "$wrapper_dir" \
          && "$entry_name" != ccache \
          && "$entry_name" != sccache ]]; then
        clean_path="${clean_path:+$clean_path:}$entry"
    fi
done
export PATH=$clean_path

case "$tool" in
    sccache)
        export SCCACHE_WRAPPER=1
        exec "$SCCACHE" "$@"
        ;;
    cc|c++|c99|gcc|g++|clang|clang++|rustc|*-gcc|*-g++|*-clang|*-clang++)
        exec "$SCCACHE" "$tool" "$@"
        ;;
    *)
        echo "sccache wrapper: invalid tool $tool" >&2
        exit 99
        ;;
esac
