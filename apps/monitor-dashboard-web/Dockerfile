# Stage 1: Build frontend with Bun
FROM harbor.local:30002/library/bun:1.3.9-alpine AS builder

WORKDIR /app

COPY package.json ./
RUN bun install --frozen-lockfile

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src

RUN bunx vite build

# Stage 2: Serve with Nginx
FROM harbor.local:30002/library/nginx:alpine

RUN apk add --no-cache tzdata
ENV TZ=Asia/Shanghai

COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=builder /app/dist /usr/share/nginx/html/dashboard

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
