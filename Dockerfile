FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY linkforge ./linkforge
COPY tests ./tests

RUN pip install --no-cache-dir -e . && pip install --no-cache-dir pytest

RUN useradd -m farmer && chown -R farmer:farmer /app
USER farmer

# The container is stateful: results.db and links/ must live on a volume.
ENV DB_PATH=/app/data/results.db LINKS_DIR=/app/data/links
RUN mkdir -p /app/data && chown -R farmer:farmer /app/data

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s \
  CMD python -c "import linkforge" || exit 1

ENTRYPOINT ["linkforge"]
CMD ["bot"]
