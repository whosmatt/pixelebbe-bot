FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        baresip-core \
        python3 \
        python3-pip \
        python3-numpy \
        python3-requests \
        python3-flask \
        python3-pil \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python-only deps that aren't in apt
COPY requirements_docker.txt ./
RUN pip3 install --break-system-packages -r requirements_docker.txt

# baresip config (injected at runtime from env/files)
RUN mkdir -p /root/.baresip

COPY baresip_config/config  /root/.baresip/config

# 3-minute WAV silence file — baresip audio source (bot sends silence to remote)
RUN python3 -c "import wave; w=wave.open('/silence.wav','w'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(bytes(8000*180*2)); w.close()"

# App code
COPY *.py ./
COPY templates/ ./templates/

EXPOSE 5001

CMD ["python3", "app.py"]
