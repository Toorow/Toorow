FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1

RUN useradd --system --uid 65532 --no-create-home --shell /usr/sbin/nologin sandbox
WORKDIR /app
COPY --chown=65532:65532 worker.py /app/worker.py
USER 65532:65532
ENTRYPOINT ["python", "-I", "/app/worker.py"]
