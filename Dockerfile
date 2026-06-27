FROM ubuntu:24.04 AS build-env

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    ninja-build \
    clang \
    clang-tidy \
    clang-format \
    lld \
    gdb \
    git \
    python3 \
    ccache \
    pkg-config \
    # tools for downloading files
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV CCACHE_DIR=/ccache
RUN mkdir -p /ccache && chmod -R 777 /ccache

ARG UID=1000
ARG GID=1000

RUN userdel -r ubuntu || true

RUN groupadd -g ${GID} devuser && \
    useradd -u ${UID} -g ${GID} -m -s /bin/bash devuser

USER devuser

WORKDIR /app
CMD ["/bin/bash"]