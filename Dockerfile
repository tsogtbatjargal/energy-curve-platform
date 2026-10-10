# The batch image (M4, ADR-0020): one image, two entry points.
#   Lambda (staging):  the default CMD, energy_curves.cloud.lambda_handler
#   Fargate (pipeline): entryPoint ["python", "-m", "energy_curves.cloud"], command ["task", <run_id>]
# Base images are pinned by their multi-arch index (manifest-list) digests, the form Dependabot
# proposes; an index digest fixes every platform's manifest. The build selects linux/amd64 with
# `docker build --platform linux/amd64` (ADR-0020). Runtime dependencies come from uv.lock,
# installed by pip with --require-hashes, so the image gets exactly the locked, verified wheels.
FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

FROM public.ecr.aws/lambda/python:3.12@sha256:d0a4fa8f489a7d9f05a95e642ffd599bea3b07339adb510185a8a14653944b1c
# OS security updates at build time. Amazon Linux 2023 locks its repositories to the image's
# release, so a plain upgrade misses fixes published since; --releasever=latest reaches them. The
# deployed image is identified by its own digest, and CI scans it (trivy, fixed HIGH/CRITICAL).
RUN dnf -y upgrade --releasever=latest && dnf clean all
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt -o requirements.txt \
    && pip install --no-cache-dir --require-hashes --target "${LAMBDA_TASK_ROOT}" -r requirements.txt \
    && pip install --no-cache-dir --no-deps --target "${LAMBDA_TASK_ROOT}" . \
    && rm -rf /build /usr/local/bin/uv
# The base ships the local-test emulator, which only runs where AWS_LAMBDA_RUNTIME_API is unset
# (never in Lambda; the Fargate task overrides the entry point). Its Go standard library has
# had CVEs, so the image does not carry it; CI mounts a checked copy for the smoke test.
RUN rm -f /usr/local/bin/aws-lambda-rie
WORKDIR ${LAMBDA_TASK_ROOT}
ENV ECP_SOURCE=synthetic PYTHONDONTWRITEBYTECODE=1
# Lambda always runs functions as its own unprivileged user; Fargate runs the image's USER, so the
# task is not root either.
USER 1000:1000
CMD ["energy_curves.cloud.lambda_handler"]
