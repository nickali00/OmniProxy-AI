FROM docker:29.9.3-cli@sha256:2357639ad1ea7d0194134bf3292622fd4e562483966ed674e5d7c945b5f3b28d

RUN apk upgrade --no-cache && apk add --no-cache python3

WORKDIR /app

COPY maintenance-runner/server.py ./server.py

EXPOSE 8790

CMD ["python3", "server.py"]
