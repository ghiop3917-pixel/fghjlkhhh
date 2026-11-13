FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core espeak-ng
COPY pyproject.toml /app/
COPY --chown=10001:10001 studio /app/studio
RUN pip install --no-cache-dir '.[voice,align]' && useradd --uid 10001 --create-home studio && mkdir /app/data && chown studio:studio /app/data
USER studio
CMD ["python", "-m", "studio", "api", "--host", "0.0.0.0"]
