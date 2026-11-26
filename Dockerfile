# Job image. Rebuilt monthly from scratch (--no-cache) for linux/amd64.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TRIAGE_ROOT=/app
WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-deps -r requirements.lock

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps . && rm -rf /root/.cache

# Synthetic corpus and recorded model outputs: lets every rebuild be exercised
# end to end (`triage demo`) with no network and no credentials.
COPY corpus ./corpus
COPY examples ./examples

USER 10001:10001
ENTRYPOINT ["python", "-m", "triage"]
CMD ["run"]
