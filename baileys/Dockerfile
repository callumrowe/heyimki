FROM node:20-slim

# Baileys dependencies require native build tools
RUN apt-get update && apt-get install -y git python3 make g++ && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

ARG CACHEBUST=0
COPY src/ ./src/

# Auth state dir — will be overridden by the named volume on the VPS
RUN mkdir -p auth_info

CMD ["node", "--watch", "src/index.js"]
