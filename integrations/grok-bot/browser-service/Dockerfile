FROM mcr.microsoft.com/playwright:v1.64.1-jammy
WORKDIR /app
RUN npm install --omit=dev playwright@1.64.1
COPY computer.js /app/computer.cjs
CMD ["node", "/app/computer.cjs"]
