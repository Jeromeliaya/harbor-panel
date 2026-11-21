FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

RUN groupadd --gid 10001 harbor \
    && useradd --uid 10001 --gid harbor --no-create-home --shell /usr/sbin/nologin harbor \
    && install -d -o harbor -g harbor /data/workspace /data/state

COPY server.py files.py host.py appearance.py terminal.py local_ssh.py LICENSE ./
COPY web/ ./web/

USER 10001:10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8088), timeout=3).close()"]

CMD ["python", "server.py", "--host", "127.0.0.1", "--port", "8088", "--root", "/data/workspace", "--state-dir", "/data/state", "--bootstrap-token-file", "/data/state/bootstrap.token"]
