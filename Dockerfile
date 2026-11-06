# Backend image. The Microsandbox SDK wheel bundles the `msb` VMM binary and
# libkrunfw, so no system libkrun package is needed. The container must be
# given /dev/kvm and write access to the host cgroup v2 tree (see compose).
FROM node:20-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend ./backend
COPY --from=ui /ui/dist ./frontend/dist
RUN python -m backend.scripts.seed_demo_data
ENV PYTHONUNBUFFERED=1 \
    AGENTLIMIT_WORKSPACE=/app/workspace \
    FRONTEND_DIST=/app/frontend/dist
EXPOSE 8000
CMD ["python", "-m", "backend.main"]
