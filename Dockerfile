FROM python:3.12-slim
WORKDIR /srv
COPY requirements-api.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV APP_ENV=production
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]
