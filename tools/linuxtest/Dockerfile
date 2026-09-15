# tools/linuxtest/Dockerfile - an Ubuntu box to run the app's own test plan on.
#
# This is a TEST harness image, deliberately not a deployment image: it carries
# a toolchain (Go, Node, Playwright, Source2Viewer-CLI) that a production image
# has no business shipping. The repo is not COPYed in either - the driver mounts
# it read-only at /src and tools/linuxtest/run_tests.py stages a writable copy,
# so a run can never write into the Windows working tree it was launched from.
#
# Build args let a run trade coverage for time; the driver passes them through:
#   WITH_GO=0        skip the Go toolchain (no parser build/test)
#   WITH_BROWSER=0   skip Playwright + Chromium (no real-browser page smoke)
FROM ubuntu:24.04

ARG WITH_GO=1
ARG WITH_BROWSER=1
ARG GO_VERSION=1.24.0

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH=/opt/venv/bin:/usr/local/go/bin:$PATH

# nodejs from the distro is Node 18, which is new enough for every module here
# (the test files are plain ESM/CJS with no runtime deps).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev \
        nodejs \
        curl unzip ca-certificates file procps \
    && rm -rf /var/lib/apt/lists/*

# A venv rather than --break-system-packages: Ubuntu 24.04 marks the system
# Python as externally managed (PEP 668), and overriding that is exactly the
# kind of "works on my box" shortcut this image exists to catch.
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --upgrade pip

COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# Source2Viewer-CLI, Linux build - the same tool tools/extract_ui_assets.sh
# drives on Windows. Downloaded here so the extraction test needs nothing from
# the host but the game files themselves.
RUN mkdir -p /opt/s2v \
    && curl -fsSL -o /tmp/s2v.zip \
        https://github.com/ValveResourceFormat/ValveResourceFormat/releases/latest/download/cli-linux-x64.zip \
    && unzip -q /tmp/s2v.zip -d /opt/s2v \
    && chmod +x /opt/s2v/Source2Viewer-CLI \
    && rm /tmp/s2v.zip

# Go from the official tarball: CLAUDE.md notes apt's Go is too old for go.mod
# (which requires 1.24).
RUN if [ "$WITH_GO" = "1" ]; then \
        curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
            | tar -C /usr/local -xz && /usr/local/go/bin/go version; \
    else echo "skipping Go toolchain (WITH_GO=$WITH_GO)"; fi

# Chromium + its system libraries, for tools/smoke_pages.py. This is the only
# check that catches a misordered <script> tag, so it is on by default despite
# being the slowest part of the build.
RUN if [ "$WITH_BROWSER" = "1" ]; then \
        /opt/venv/bin/pip install --no-cache-dir playwright \
        && /opt/venv/bin/playwright install --with-deps chromium; \
    else echo "skipping Playwright (WITH_BROWSER=$WITH_BROWSER)"; fi

ENV SOURCE2VIEWER_CLI=/opt/s2v/Source2Viewer-CLI
WORKDIR /app
ENTRYPOINT ["/opt/venv/bin/python3", "/src/tools/linuxtest/run_tests.py"]
