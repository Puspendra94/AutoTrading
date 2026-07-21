# AI-Based Algorithmic Trading Platform

An AI-assisted, multi-provider algorithmic trading platform built with Node.js/NestJS (Backend), AstroJS (Frontend), PostgreSQL with TimescaleDB (Time-series DB), and Redis.

## Project Structure

```
├── apps/
│   ├── backend/     # NestJS API, BullMQ workers, TypeORM migrations, Trading logic
│   └── frontend/    # AstroJS Dashboard, WebSocket live chart & position views
├── infra/
│   ├── docker-compose.yml  # TimescaleDB Postgres (port 5433) & Redis (port 55000)
│   └── .env               # Infrastructure docker environment configuration
├── .env.example     # Environment template
└── README.md
```

## Quick Start

1. **Start Infrastructure Services**:
   ```bash
   npm run infra:up
   ```

2. **Configure API Keys**:
   Edit `.env` and add your `ANTHROPIC_API_KEY` or AWS Bedrock credentials.

3. **Start Backend Server**:
   ```bash
   cd apps/backend
   npm install
   npm run start:dev
   ```

4. **Start Frontend Dashboard**:
   ```bash
   cd apps/frontend
   npm install
   npm run dev
   ```
