# One service, for one person, on one machine. Nothing here is built for
# deployment: it exists so the port binding and the volume are written down
# instead of remembered.
FROM python:3.13-slim

# uv from PyPI rather than `COPY --from=ghcr.io/astral-sh/uv`. The registry copy
# is faster, but it adds a second registry the build must reach and a tag that
# has to stay right; PyPI is one the build needs anyway. uv is here for one
# reason: it is the only thing that can install uv.lock exactly.
#
# Pinned to the version that wrote uv.lock (lockfile revision 3). An older uv
# refuses a revision it does not know, so if `uv lock` is ever re-run on the
# host with a newer uv, this line moves with it.
RUN pip install --no-cache-dir uv==0.12.13

WORKDIR /app

# The virtualenv lives OUTSIDE /app on purpose. The repo is bind mounted over
# /app at run time, so a .venv built here would be shadowed by whatever .venv
# the host has, which on Windows is a venv full of Windows binaries.
#
# UV_PYTHON_DOWNLOADS=never keeps uv on the image's interpreter. Left to itself
# it downloads and manages its own CPython, and the image ends up carrying two.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# Only the dependency manifests are copied, so editing rules.yaml or any module
# does not invalidate this layer. Dependencies are the only thing a rebuild is
# for; everything else arrives through the mount.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

EXPOSE 8000

# The app's own entry point rather than a bare uvicorn line, so the container
# and the terminal start the server the same way.
#
# --host 0.0.0.0 looks like the mistake the CLI default exists to prevent, and
# is not: this is the container's own network namespace, and 127.0.0.1 here
# would mean "reachable only from inside the container". What keeps the ledger
# private is the 127.0.0.1 on the published port in compose.yaml.
CMD ["python", "-m", "finanzas", "serve", "--host", "0.0.0.0", "--port", "8000"]
