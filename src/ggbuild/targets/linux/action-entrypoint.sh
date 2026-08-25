#!/bin/sh
set -eu

: "${GGBUILD_REF:?GGBUILD_REF must be a full ggbuild Git commit SHA}"

case "${GGBUILD_REF}" in
    *[!0-9a-f]* | "")
        echo "GGBUILD_REF must be a full lowercase Git commit SHA" >&2
        exit 2
        ;;
esac
if [ "${#GGBUILD_REF}" -ne 40 ]; then
    echo "GGBUILD_REF must be a full lowercase Git commit SHA" >&2
    exit 2
fi

project_root="${GGBUILD_PROJECT_ROOT:-${GITHUB_WORKSPACE:-/github/workspace}}"
export PYTHONPATH="${project_root}/src:${project_root}${PYTHONPATH:+:${PYTHONPATH}}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${project_root}/.cache/uv}"

mkdir -p "${UV_CACHE_DIR}"
PYTHONDONTWRITEBYTECODE=1 uv pip install \
    --python /opt/venv/bin/python \
    --upgrade \
    "ggbuild @ git+https://github.com/vercel-labs/ggbuild@${GGBUILD_REF}"

cd "${project_root}"
rootless=false
if [ -r /proc/1/uid_map ]; then
    read -r mapped_uid host_uid mapped_count < /proc/1/uid_map
    if [ "${mapped_uid}" = 0 ] && \
        { [ "${host_uid}" != 0 ] || [ "${mapped_count}" != 4294967295 ]; }; then
        rootless=true
    fi
fi
if [ "$(id -u)" -eq 0 ] && [ "${rootless}" = false ]; then
    test_uid="$(stat -c %u "${project_root}")"
    test_gid="$(stat -c %g "${project_root}")"
    if [ "${test_uid}" -eq 0 ]; then
        test_uid="$(id -u nobody)"
        test_gid="$(id -g nobody)"
    fi
    test_home="${project_root}/.cache/ggbuild-home"
    mkdir -p "${test_home}" "${project_root}/dist" "${project_root}/.cache/work"
    chown -R "${test_uid}:${test_gid}" \
        "${project_root}/.cache" "${project_root}/dist"
    if ! awk -F: -v uid="${test_uid}" \
        '$3 == uid { found = 1 } END { exit !found }' /etc/passwd; then
        printf 'ggbuild:x:%s:%s:ggbuild:%s:/bin/sh\n' \
            "${test_uid}" "${test_gid}" "${test_home}" >> /etc/passwd
    fi
    if ! awk -F: -v gid="${test_gid}" \
        '$3 == gid { found = 1 } END { exit !found }' /etc/group; then
        printf 'ggbuild:x:%s:\n' "${test_gid}" >> /etc/group
    fi
    export HOME="${test_home}"
    exec /opt/venv/bin/python -c '
import os
import sys
os.setgroups([])
os.setgid(int(sys.argv[1]))
os.setuid(int(sys.argv[2]))
os.execv(sys.argv[3], sys.argv[3:])
' "${test_gid}" "${test_uid}" /opt/venv/bin/python -m ggbuild "$@"
fi
exec /opt/venv/bin/python -m ggbuild "$@"
