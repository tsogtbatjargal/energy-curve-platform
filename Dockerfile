# The batch image (M4, ADR-0020): one image, two entry points.
#   Lambda (staging):  the default CMD, energy_curves.cloud.lambda_handler
#   Fargate (pipeline): entryPoint ["python", "-m", "energy_curves.cloud"], command ["task", <run_id>]
# Base images are pinned by their linux/amd64 digests. Runtime dependencies come from uv.lock,
# installed by pip with --require-hashes, so the image gets exactly the locked, verified wheels.
FROM ghcr.io/astral-sh/uv:0.12.17@sha256:1194b357d63b7bea6c121d8eef5d08d29c26ffbcfc64ec5ebcefd642ea759edb AS uv

FROM public.ecr.aws/lambda/python:3.14@sha256:feabf69ea6ba5e044cceb2a2cbf683c740c18970c2a3ac6191edb014dae1a047
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
