FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .

# Run unprivileged; the service needs no filesystem writes at all.
USER 65534:65534

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/healthz', timeout=3)"

CMD ["python", "app.py"]
