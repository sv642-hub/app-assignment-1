# Build the React app with Node, then ship only the static files inside the
# Caddy image that already fronts the stack. Build from the repository root:
# docker build -f frontend/Dockerfile .
FROM node:22.22.3-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
