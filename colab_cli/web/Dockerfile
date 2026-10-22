# Web 控制台（Nuxt 4 / Nitro node-server）
FROM node:24-bookworm-slim AS build
# argon2 优先用 prebuilt 二进制；缺失时回退源码编译所需工具链
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts && npm rebuild argon2
COPY . .
RUN npx nuxt build

FROM node:24-bookworm-slim AS runtime
ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000
WORKDIR /app
COPY --from=build /app/.output ./.output
USER node
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD node -e "fetch('http://127.0.0.1:3000/api/health').then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))"
# 数据库迁移在 Nitro 启动时由 server/plugins/01.db.ts 执行（migrationsRun: true）
CMD ["node", ".output/server/index.mjs"]
