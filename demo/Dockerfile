# Stub OpenAI-compatible transcription + chat-completions server for the
# demo. Not a production image: no ffmpeg/whisper/LLM, just FastAPI +
# pattern-matching over data generate_media.py already generated (see
# stub_services.py).
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" python-multipart

COPY stub_services.py ./

EXPOSE 8000
CMD ["uvicorn", "stub_services:app", "--host", "0.0.0.0", "--port", "8000"]
