FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY lucidlink_api.py .

# Surface SDK deprecations in the logs instead of letting them pass silently.
# A no-arg unlink_filespace() call went unnoticed for months exactly because
# nothing was listening for these.
ENV PYTHONWARNINGS=default::DeprecationWarning

EXPOSE 8000

CMD ["uvicorn", "lucidlink_api:app", "--host", "0.0.0.0", "--port", "8000"]
