FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    requests \
    websocket-client \
    'httpx[socks]'

# Copy the engine
COPY polymarket_engine3.py /app/polymarket_engine3.py
COPY polymarket_engine3_test.py /app/polymarket_engine3_test.py

# Sanity check on build — engine module imports clean
RUN python3 -c "import polymarket_engine3; print('engine3 module imports OK')"
RUN python3 polymarket_engine3_test.py

# Logs go to stdout — fly logs streams them
ENV PYTHONUNBUFFERED=1

CMD ["python3", "-u", "polymarket_engine3.py"]
