# Multi-stage / minimal container build for reference sample API
FROM python:3.11-slim-bookworm

WORKDIR /app

# Ensure Python output is sent straight to terminal (container logs) without buffering
ENV PYTHONUNBUFFERED=1 \
    PORT=8080

# Create unprivileged application user
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -M appuser

# Copy application script
COPY --chown=10001:10001 app.py /app/app.py

# Switch to non-root user
USER 10001:10001

EXPOSE 8080

ENTRYPOINT ["python", "/app/app.py"]
