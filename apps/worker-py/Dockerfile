# Python Data + AI worker. Runs 24/7: owns the Binance kline stream, the feature engine, the
# decision loop and the exit ladder.
#
# The previous version of this file installed four packages by hand (asyncpg, httpx, apscheduler,
# python-dotenv). It predated every AI and pattern feature, so the image could not import half the
# code it was supposed to run. Dependencies now come from pyproject.toml, which is the one place
# they are declared.
#
# TA-Lib is the reason this is a multi-stage build. The Python package is a thin wrapper around a
# C library that is not in any slim Python image and is not pip-installable — it has to be
# compiled. Building it in a throwaway stage keeps the compiler toolchain out of the final image
# while the runtime still gets the shared object it links against.

# --- build stage: compile the TA-Lib C library, then the Python deps against it ---
FROM python:3.12-slim AS build

ARG TALIB_VERSION=0.6.4

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --prefix=/usr so the headers and .so land where the Python wrapper's build looks by default.
RUN wget -q "https://github.com/TA-Lib/ta-lib/releases/download/v${TALIB_VERSION}/ta-lib-${TALIB_VERSION}-src.tar.gz" \
    && tar xzf "ta-lib-${TALIB_VERSION}-src.tar.gz" \
    && cd "ta-lib-${TALIB_VERSION}" \
    && ./configure --prefix=/usr \
    && make -j"$(nproc)" \
    && make install \
    && cd / && rm -rf "ta-lib-${TALIB_VERSION}" "ta-lib-${TALIB_VERSION}-src.tar.gz"

# Everything installs into a venv so the runtime stage can take it as one directory.
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app
# numpy first: the TA-Lib wrapper compiles against its headers, so it must already be present.
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir "numpy>=1.26"

# Dependency layer, cached until pyproject changes. Only the manifest and the package are copied,
# so setuptools' flat-layout auto-discovery sees exactly one top-level package.
COPY pyproject.toml ./
COPY worker ./worker
RUN pip install --no-cache-dir .

# --- runtime stage: venv + the TA-Lib shared object, no toolchain ---
FROM python:3.12-slim

# libta-lib.so is loaded at import time; without it `import talib` fails even though the wheel
# is installed.
COPY --from=build /usr/lib/libta-lib* /usr/lib/
COPY --from=build /opt/venv /opt/venv

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY worker ./worker

# Fail fast and loudly at build time if the native library did not survive the stage copy —
# far better than discovering it when the first candle closes in production.
RUN python -c "import talib, numpy; print('TA-Lib', talib.__version__, '/ numpy', numpy.__version__)"

CMD ["python", "-m", "worker.main"]
