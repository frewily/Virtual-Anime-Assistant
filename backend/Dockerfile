FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/backend/requirements.txt

RUN groupadd --gid 10001 vaa \
    && useradd --uid 10001 --gid vaa --create-home vaa

COPY --chown=vaa:vaa backend /app/backend
COPY --chown=vaa:vaa config /app/config

WORKDIR /app/backend
USER vaa

CMD ["python", "main.py"]
