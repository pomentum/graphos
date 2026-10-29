#!/bin/sh
# graphos-api's entrypoint: python-igraph when asked for, then graphos.
#
# python-igraph (GPL-2.0-or-later) gives graphos its community algorithms,
# Louvain and Leiden. The published image leaves it out, so that the image
# holds only permissively licensed code. With GRAPHOS_IGRAPH=true this script
# installs it from PyPI into the container on its first start (restarts reuse
# it; a recreated container installs it again, and needs PyPI then), and
# graphos finds it at start: GET /v1/info reports "louvain": true.
#
#   GRAPHOS_IGRAPH          true: install python-igraph (default false). A
#                           requirement ("python-igraph>=0.11,<1.0", as
#                           docker-compose.louvain.yml set it before) also
#                           turns it on, with that requirement.
#   GRAPHOS_IGRAPH_PACKAGE  the requirement to install (default: the `louvain`
#                           extra's, in pyproject.toml).
#
# Nothing is installed when igraph is there already: an image built with
# GRAPHOS_EXTRAS=louvain, or an install of the same requirement kept by the
# container. A failed install is reported and graphos starts without it
# (recompute-communities then answers 501); the next start tries again.
#
# Arguments go to graphos: `docker run <image> reset-admin-password`.
set -eu

extras=/tmp/graphos-extras
# Keep within the `louvain` extra of pyproject.toml (tests/test_louvain_optional.py).
package="${GRAPHOS_IGRAPH_PACKAGE:-python-igraph>=0.11,<1.0}"

case "${GRAPHOS_IGRAPH:-false}" in
    true | 1 | yes) want=true ;;
    python-igraph*) want=true; package="$GRAPHOS_IGRAPH" ;;
    *) want=false ;;
esac

if [ "$want" = true ] && ! python -c "import igraph" 2>/dev/null; then
    if [ "$(cat "$extras/.installed" 2>/dev/null)" != "$package" ]; then
        echo "graphos-api: installing $package for Louvain and Leiden" >&2
        rm -rf "$extras"
        if HOME=/tmp pip install --quiet --no-cache-dir --disable-pip-version-check \
            --target "$extras" "$package"; then
            echo "$package" > "$extras/.installed"
        else
            echo "graphos-api: could not install $package; starting without Louvain and Leiden" >&2
            rm -rf "$extras"
        fi
    fi
    if [ -d "$extras" ]; then
        export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$extras"
    fi
fi

exec python -m graphos "$@"
