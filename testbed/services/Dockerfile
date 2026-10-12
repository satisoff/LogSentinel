# One image for gateway, orders, inventory and the load generator (keeps disk and build time low).
FROM python:3.12.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /app
COPY app/ /app/
# SERVICE selects the module: gateway | orders | inventory | loadgen
CMD ["sh", "-c", "exec uvicorn ${SERVICE}:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log --log-level warning"]
