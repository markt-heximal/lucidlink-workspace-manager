FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

COPY lucidlink-0.6.0-cp312-cp312-manylinux_2_28_x86_64.whl .

RUN pip install --no-cache-dir \
    fastapi==0.115.12 \
    uvicorn==0.34.3 \
    httpx==0.28.1 \
    python-multipart==0.0.20 \
    ./lucidlink-0.6.0-cp312-cp312-manylinux_2_28_x86_64.whl \
    && rm lucidlink-0.6.0-cp312-cp312-manylinux_2_28_x86_64.whl

COPY lucidlink_api.py .

EXPOSE 8000

CMD ["uvicorn", "lucidlink_api:app", "--host", "0.0.0.0", "--port", "8000"]
