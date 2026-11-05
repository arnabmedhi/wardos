FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY wardos ./wardos
COPY tools ./tools
COPY skills/openmrs ./skills/openmrs
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app:/app/tools WARDOS_PRIVATE_DIR=/tmp/wardos-demo \
    WARDOS_CHROME=/usr/bin/chromium WARDOS_MUSE_MODEL=muse-spark-1.3-contributor WARDOS_MUSE_EFFORT=high \
    TZ=Asia/Kolkata
CMD ["python", "-m", "wardos.hospital.demo_robot"]
