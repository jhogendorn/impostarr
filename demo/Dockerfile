# Stub OpenAI-compatible transcription server for the demo. Not a
# production image: no ffmpeg/whisper, just FastAPI + a duration-keyed
# lookup table (see stub_transcriber.py).
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" python-multipart

COPY stub_transcriber.py ./

EXPOSE 8000
CMD ["uvicorn", "stub_transcriber:app", "--host", "0.0.0.0", "--port", "8000"]
