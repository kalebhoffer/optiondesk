FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

COPY config ./config
RUN mkdir -p /app/data /home/app/.optiondesk && chown -R app:app /app/data /home/app/.optiondesk
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
# Listen on all interfaces inside the container; docker-compose.yml only
# publishes the port on the host's loopback address.
CMD ["optiondesk", "run", "--config", "config/example.toml", "--host", "0.0.0.0"]
