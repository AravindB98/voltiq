FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY voltiq ./voltiq
RUN pip install --no-cache-dir .

EXPOSE 8000
ENV VOLTIQ_DB=/data/voltiq.db
VOLUME /data

# Seed a demo fleet on first start, then serve the API + dashboard
CMD ["sh", "-c", "voltiq demo --vehicles 5 --days 365 && voltiq serve --host 0.0.0.0 --port 8000"]
