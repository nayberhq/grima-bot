FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 GRIMA_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt LICENSE.md ./
RUN pip install --no-cache-dir -r requirements.txt
COPY source /app/source
COPY tests /app/tests
COPY deploy /app/deploy
COPY deploy.sh /app/deploy.sh
RUN chmod +x /app/deploy/entrypoint.sh
WORKDIR /app/source
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=4 CMD python -c "import os,time; assert time.time()-os.path.getmtime('/tmp/grima-ready')<90"
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
