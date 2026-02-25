# Stage 1: Build
FROM oven/bun:1-alpine AS builder
WORKDIR /app
COPY package.json bun.lock* ./
RUN bun install --frozen-lockfile
COPY tsconfig.json ./
COPY src/ ./src/
RUN bun build src/index.ts --target=bun --outdir=dist --external pg-cloudflare
RUN bun install --production

# Stage 2: Production
FROM oven/bun:1-alpine
WORKDIR /app
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/dist ./dist

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

CMD ["bun", "dist/index.js"]
