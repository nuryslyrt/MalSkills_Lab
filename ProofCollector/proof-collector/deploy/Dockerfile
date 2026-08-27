# Proof Collector — M1 container (stdlib only: no pip, no build-time network beyond the base image).
FROM python:3.12-slim
RUN useradd -u 10001 -m collector
WORKDIR /app
COPY app/ ./app/
COPY scenarios/ ./scenarios/
ENV MODE=local PORT=8888 CONFIG_DIR=/config STATE_DIR=/state PYTHONUNBUFFERED=1
USER collector
EXPOSE 8888
CMD ["python3", "-m", "app.server"]
