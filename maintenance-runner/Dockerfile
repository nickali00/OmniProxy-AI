FROM docker:27-cli

RUN apk add --no-cache python3

WORKDIR /app

COPY maintenance-runner/server.py ./server.py

EXPOSE 8790

CMD ["python3", "server.py"]
