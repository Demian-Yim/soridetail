# Hey Vision + SoriDetail — one container, ready for Cloud Run or any Docker host.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv

# onnxruntime (used by the voice model) needs OpenMP; libsndfile is for WAV output.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libsndfile1 \
    && apt-get clean

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the voice model into the image so a cold start never downloads it.
RUN python -c "from supertonic import TTS; TTS(model='supertonic-3', auto_download=True)"

COPY app ./app

# Secrets are NOT in the image. Pass DAYTONA_API_KEY and GEMINI_API_KEY at run time.
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
