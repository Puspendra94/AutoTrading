# --- build stage: compile the static Astro site ---
# (npm install, not npm ci: this app is an npm-workspaces member with no local lockfile —
# the lock lives at the repo root, which isn't in this build context.)
FROM node:20-slim AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

# --- run stage: slim image that serves ./dist + proxies /api and /socket.io ---
FROM node:20-slim
WORKDIR /app
ENV NODE_ENV=production
COPY package.json ./
RUN npm install --omit=dev
COPY --from=build /app/dist ./dist
COPY server.mjs ./server.mjs
# Fixed internal port; the published host port is chosen by compose (random).
EXPOSE 4321
CMD ["node", "server.mjs"]
