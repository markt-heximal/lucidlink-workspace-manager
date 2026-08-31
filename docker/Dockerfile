FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

# Dependencies are pinned in requirements.txt so the image is reproducible and
# bumps show up as a reviewable diff.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ll_core.py connect_api.py insights_api.py lucidlink_api.py ./

# Surface SDK deprecations in the logs instead of letting them pass silently.
ENV PYTHONWARNINGS=default::DeprecationWarning

ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA

EXPOSE 8000

CMD ["uvicorn", "lucidlink_api:app", "--host", "0.0.0.0", "--port", "8000"]
