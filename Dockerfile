FROM python:3.14-slim
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Tehran
COPY vms_autostart.py /app/vms_autostart.py
RUN mkdir -p /app/state /var/log/vms_autostart
ENTRYPOINT ["python3", "/app/vms_autostart.py"]
