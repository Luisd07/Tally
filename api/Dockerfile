FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /srv

# pg_dump, pinned to the postgres:16 server image in docker-compose.yml (Debian trixie's
# own repo only carries the postgresql-client-17 front-end), plus restic, for the nightly
# backup job in app/worker.py. ca-certificates stays installed: restic needs it for TLS to
# Cloud Storage; curl and gnupg are build-time only, for fetching and verifying the pgdg key.
# postgresql-client-16 is pinned by major version only: trixie-pgdg only serves the current
# minor release, so an exact minor pin breaks the monthly rebuild the moment it rotates.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-client-16 \
        restic=0.18.0-1+b4 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/* /etc/apt/sources.list.d/pgdg.list

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

RUN useradd --system --uid 10001 app && mkdir -p /data/receipts && chown app /data/receipts
USER app

EXPOSE 8000
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
