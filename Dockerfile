FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY data ./data
COPY sources ./sources
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-cache-dir .
ENTRYPOINT ["seawork"]

