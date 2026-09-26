FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/srv/data HOME=/tmp
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY morning_brief morning_brief
COPY migrations migrations
COPY feeds.yaml .
RUN useradd --uid 1000 --no-create-home --home-dir /tmp app
USER app
EXPOSE 8000
CMD ["uvicorn", "morning_brief.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
