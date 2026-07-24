# --- build stage: compile the NestJS app ---
# (npm install, not npm ci: this app is an npm-workspaces member with no local lockfile —
# the lock lives at the repo root, which isn't in this build context.)
FROM node:20-slim AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

# --- run stage: prod deps + compiled dist only ---
FROM node:20-slim
WORKDIR /app
ENV NODE_ENV=production
COPY package.json ./
RUN npm install --omit=dev
COPY --from=build /app/dist ./dist
# Fixed internal API port; migrations run on boot (see src/main.ts).
EXPOSE 3009
CMD ["node", "dist/main"]
