# The batch image (M4, ADR-0020): one image, two entry points.
#   Lambda (staging):  the default CMD, energy_curves.cloud.lambda_handler
#   Fargate (pipeline): entryPoint ["python", "-m", "energy_curves.cloud"], command ["task", <run_id>]
# Base images are pinned by their linux/amd64 digests. Runtime dependencies come from uv.lock,
# installed by pip with --require-hashes, so the image gets exactly the locked, verified wheels.
FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

FROM public.ecr.aws/lambda/python:3.12@sha256:517bcc7dc1a3ba62e324961c1563cc54a2bd6dc1060188f9d376211ecb7b2926
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
WORKDIR ${LAMBDA_TASK_ROOT}
ENV ECP_SOURCE=synthetic PYTHONDONTWRITEBYTECODE=1
# Lambda always runs functions as its own unprivileged user; Fargate runs the image's USER, so the
# task is not root either.
USER 1000:1000
CMD ["energy_curves.cloud.lambda_handler"]
