FROM python:3.12-slim
WORKDIR /srv
COPY requirements-api.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --no-create-home app
COPY app ./app
USER app
ENV APP_ENV=production
# The login throttle, RAG index and CAG cache live in process memory, so default to ONE worker; raise WEB_CONCURRENCY only after moving them to Redis.
# Behind a proxy/load balancer (Render, Fly, nginx) set FORWARDED_ALLOW_IPS to its address (or '*' if only the proxy can reach the container)
# so the per-client login throttle sees real client IPs.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
