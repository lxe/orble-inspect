# The image Inspect's sandbox checks run in: procps for ps, adduser, and an unprivileged default user, so the
# permission checks mean something (root reads and writes anything).
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends procps adduser && rm -rf /var/lib/apt/lists/* \
 && useradd -m tester && mkdir /work && chown tester /work
USER tester
WORKDIR /work
