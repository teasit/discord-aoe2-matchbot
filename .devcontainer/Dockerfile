FROM python:3.12-slim-bookworm

############################################### TOOLS ###################################################
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
#########################################################################################################

############################################### DOCKER ##################################################
# Docker CLI with compose and buildx, talking to the host daemon through the mounted socket. Replaces the
# docker-outside-of-docker feature, whose download from ghcr.io fails on some networks.
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
COPY docker-socket-init.sh /usr/local/share/docker-socket-init.sh
ENTRYPOINT ["/usr/local/share/docker-socket-init.sh"]
CMD ["sleep", "infinity"]
#########################################################################################################

############################################### UV ######################################################
RUN pip install --no-cache-dir uv
#########################################################################################################

############################################### USER ####################################################
# Non-root user so files written to the bind-mounted repo (e.g. .git/objects) stay owned by the host user.
# VS Code remaps the UID/GID to the host user's on Linux (updateRemoteUserUID).
ARG USERNAME=vscode
ARG USER_UID=1000
ARG USER_GID=$USER_UID
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID --create-home --shell /bin/bash $USERNAME
#########################################################################################################
