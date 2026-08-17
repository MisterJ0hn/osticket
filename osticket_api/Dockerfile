FROM python:3.12-slim

WORKDIR /srv

# Las dependencias van en una capa aparte para que un cambio de código no
# obligue a reinstalarlas.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Sin buffering, los logs salen al instante en `docker logs`.
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
