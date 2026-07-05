FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

# lucidlink 0.12.0 ships a portable py3-none wheel (bundles liblucid.so) on PyPI,
# so we no longer vendor a cp312 wheel. Only an x86_64 manylinux wheel is published
# (no linux/arm64), hence the --platform=linux/amd64 pin above is still required.
RUN pip install --no-cache-dir \
    fastapi==0.115.12 \
    uvicorn==0.34.3 \
    httpx==0.28.1 \
    python-multipart==0.0.20 \
    lucidlink==0.12.0

COPY lucidlink_api.py .

EXPOSE 8000

CMD ["uvicorn", "lucidlink_api:app", "--host", "0.0.0.0", "--port", "8000"]
