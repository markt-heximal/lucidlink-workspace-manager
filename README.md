# LucidLink Workspace Manager

A self-hosted API for managing [LucidLink](https://www.lucidlink.com/) filespaces and files. Runs as two Docker containers:

- **lucidlink-fileservice** — FastAPI app wrapping the LucidLink Python SDK for file operations + filespace listing
- **lucidlink-api** — Official LucidLink management API (filespace creation, deletion, cloud operations)

Both are exposed through a single port (8000) with the management API proxied at `/api/v1/*`.

## Prerequisites

- Docker with Compose (v2+)
- A LucidLink Service Account token (`sa_live:...`)
- (Optional) [Tailscale](https://tailscale.com/) for remote access via Funnel

## Quick Start

```bash
git clone https://github.com/markt-heximal/lucidlink-workspace-manager.git
cd lucidlink-workspace-manager
chmod +x setup.sh start.sh
./setup.sh    # interactive — creates .env
./start.sh    # builds & launches containers
```

## API Endpoints

### File Operations
Require `Authorization: Bearer <token>` and `X-LucidLink-Filespace: <name>` headers.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/files?path=/` | List directory |
| GET | `/files/read?path=/file.txt` | Read file (text or base64) |
| POST | `/files/write` | Write file (JSON body: `{path, content}`) |
| POST | `/files/upload` | Upload file (multipart form) |
| GET | `/files/download?path=/file.txt` | Download file |
| POST | `/files/mkdir?path=/dir` | Create directory |
| DELETE | `/files?path=/file.txt` | Delete file |
| DELETE | `/files/dir?path=/dir` | Delete directory |
| POST | `/files/move` | Move/rename (JSON body: `{src, dst}`) |
| GET | `/files/stat?path=/file.txt` | File metadata |
| GET | `/files/exists?path=/file.txt` | Check existence |

### Filespace Operations
Require `Authorization: Bearer <token>` header.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/filespaces` | List all filespaces |
| GET | `/filespaces/{name}` | Get filespace by name |

### Management API (proxied)
Forwarded to the LucidLink management API container.

| Method | Path | Description |
|--------|------|-------------|
| * | `/api/v1/*` | All management operations |

### Health & Status

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/uptime` | Uptime info |
| GET | `/docs` | Swagger UI |

## Configuration

Copy `.env.example` to `.env` or run `./setup.sh`:

```env
LUCIDLINK_TOKEN=sa_live:your_key:your_secret
MINIO_ENDPOINT=https://your-minio.example.com
MINIO_BUCKET=lucidlink
MINIO_ACCESS_KEY=your_access_key
MINIO_SECRET_KEY=your_secret_key
MINIO_REGION=us-east-1
```

## Architecture

```
                        ┌──────────────────────────┐
  Client ──► :8000 ──►  │  lucidlink-fileservice    │
                        │  (FastAPI + Python SDK)   │
                        │                          │
                        │  /files/* — direct SDK   │
                        │  /filespaces — direct SDK│
                        │  /api/v1/* ──proxy──►────┼──► lucidlink-api:3003
                        └──────────────────────────┘    (management API)
```

## Platform Notes

The LucidLink Python SDK is x86_64 only. On ARM hosts (Raspberry Pi, Apple Silicon), Docker runs it under QEMU emulation automatically — this works but is slower.

## License

MIT
