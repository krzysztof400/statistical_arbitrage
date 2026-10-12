# Statistical Arbitrage Engine

## Prerequisites

For a native build, install:

- CMake 3.28 or newer
- A C++20 compiler
- Ninja
- Git (CMake downloads Catch2 during configuration)

The repository also includes a Docker-based development environment with all required tools.

## Build Natively

From the repository root, configure and build a Release version:

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

The first configuration downloads Catch2 through CMake's `FetchContent`.

## Start the Engine

Run the engine executable after building:

```sh
./build/src/bot
```

The current example engine prints the calculated signal and exits.

## Run Tests

Run the complete test suite through CTest:

```sh
ctest --test-dir build --output-on-failure
```

To run the Catch2 executable directly:

```sh
./build/tests/tests
```

After changing source files, rebuild before running the tests:

```sh
cmake --build build
ctest --test-dir build --output-on-failure
```

## Work in Docker

### VS Code Dev Container

The recommended workflow is to open the repository in VS Code and run **Dev Containers: Reopen in Container** from the Command Palette. The `.devcontainer/devcontainer.json` configuration builds the image from `Dockerfile`, uses the non-root `devuser` account, and installs the C/C++, CMake, formatting, and Vim extensions.

Inside the container, run the same build, engine, and test commands from the repository root:

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
./build/src/bot
ctest --test-dir build --output-on-failure
```

The `build/` directory is ignored by Git, and the container uses a persistent ccache volume to speed up repeated builds.

### Run the Container Manually

Build the development image from the repository root:

```sh
docker build -t statistical-arbitrage-dev .
```

Start a shell with the repository mounted at `/app`:

```sh
docker run --rm -it \
	-v "$PWD:/app" \
	-w /app \
	statistical-arbitrage-dev
```

Then execute the build and test commands from the previous section inside the container. If the host uses a non-default user ID, pass matching `UID` and `GID` values when building the image to avoid permission issues with generated files:

```sh
docker build --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" \
	-t statistical-arbitrage-dev .
```

