FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The ledger lives here; mount a volume so codes survive a redeploy. Losing it
# does not lose access -- the reconciler rebuilds from Hostaway -- but it would
# orphan the passcodes already on the locks.
RUN mkdir -p /data /config && \
    adduser --system --group --no-create-home bridge && \
    chown -R bridge:bridge /data /config
USER bridge

ENV DATABASE_URL=sqlite:////data/bridge.db \
    UNITS_FILE=/config/units.yaml

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
