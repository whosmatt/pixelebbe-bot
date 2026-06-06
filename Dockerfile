FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-numpy \
        python3-requests \
        python3-flask \
        python3-pil \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements_docker.txt ./
RUN pip3 install --break-system-packages -r requirements_docker.txt

COPY *.py ./
COPY templates/ ./templates/

EXPOSE 5001

CMD ["python3", "app.py"]
