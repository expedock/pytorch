# syntax = docker/dockerfile:experimental
#
# NOTE: To build this you will need a docker version > 18.06 with
#       experimental enabled and DOCKER_BUILDKIT=1
#
#       If you do not use buildkit you are not going to have a good time
#
#       For reference:
#           https://docs.docker.com/develop/develop-images/build_enhancements/
ARG BASE_IMAGE=ubuntu:22.04
ARG PYTHON_VERSION=3.10.4

FROM ${BASE_IMAGE} as dev-base
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        ccache \
        cmake \
        curl \
        git \
        libjpeg-dev \
        libpng-dev && \
    rm -rf /var/lib/apt/lists/*
RUN /usr/sbin/update-ccache-symlinks
RUN mkdir /opt/ccache && ccache --set-config=cache_dir=/opt/ccache
ENV PATH /opt/conda/bin:$PATH

FROM dev-base as conda
ARG PYTHON_VERSION=3.10.4
# Automatically set by buildx
ARG TARGETPLATFORM
# translating Docker's TARGETPLATFORM into miniconda arches
RUN case ${TARGETPLATFORM} in \
         "linux/arm64")  MINICONDA_ARCH=aarch64  ;; \
         *)              MINICONDA_ARCH=x86_64   ;; \
    esac && \
    curl -fsSL -v -o ~/miniconda.sh -O  "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-${MINICONDA_ARCH}.sh"
COPY requirements.txt .
# Manually invoke bash on miniconda script per https://github.com/conda/conda/issues/10431
RUN chmod +x ~/miniconda.sh && \
    bash ~/miniconda.sh -b -p /opt/conda && \
    rm ~/miniconda.sh && \
    /opt/conda/bin/conda install -y python=${PYTHON_VERSION} cmake conda-build pyyaml numpy ipython && \
    /opt/conda/bin/python -mpip install -r requirements.txt && \
    /opt/conda/bin/conda clean -ya
RUN /opt/conda/bin/conda install -c nvidia cuda-toolkit

FROM dev-base as submodule-update
WORKDIR /opt/pytorch
COPY . .
RUN git submodule update --init --recursive

FROM conda as build
WORKDIR /opt/pytorch
COPY --from=conda /opt/conda /opt/conda
COPY --from=submodule-update /opt/pytorch /opt/pytorch
RUN --mount=type=cache,target=/opt/ccache \
    # 7.5 for Franz' RTX 2060 GPU and 8.6 for the A10G GPUs in the ml.g5.* instances
    # +PTX for forward compatibility
    TORCH_CUDA_ARCH_LIST="7.5;8.6+PTX" TORCH_NVCC_FLAGS="-Xfatbin -compress-all" \
    CMAKE_PREFIX_PATH="$(dirname $(which conda))/../" \
    LDFLAGS="-L/opt/conda/lib/ $LDFLAGS" \
    BUILD_TEST=0 USE_DISTRIBUTED=0 \
    python setup.py install

FROM conda as conda-installs
ARG PYTHON_VERSION=3.10.4
ARG CUDA_VERSION=11.7
ARG CUDA_CHANNEL=nvidia
ARG INSTALL_CHANNEL=pytorch-nightly
# Automatically set by buildx
RUN /opt/conda/bin/conda update -y conda
RUN /opt/conda/bin/conda install -c "${INSTALL_CHANNEL}" -y python=${PYTHON_VERSION}
ARG TARGETPLATFORM

# On arm64 we can only install wheel packages.
RUN case ${TARGETPLATFORM} in \
         "linux/arm64")  pip install --extra-index-url https://download.pytorch.org/whl/cpu/ torch torchvision torchaudio torchtext ;; \
         *)              /opt/conda/bin/conda install -c "${INSTALL_CHANNEL}" -c "${CUDA_CHANNEL}" -y "python=${PYTHON_VERSION}" pytorch torchvision torchaudio torchtext "pytorch-cuda=$(echo $CUDA_VERSION | cut -d'.' -f 1-2)"  ;; \
    esac && \
    /opt/conda/bin/conda clean -ya
RUN /opt/conda/bin/pip install torchelastic

FROM ${BASE_IMAGE} as official
ARG PYTORCH_VERSION=2.0.1
ARG TRITON_VERSION
ARG TARGETPLATFORM
ARG CUDA_VERSION
LABEL com.nvidia.volumes.needed="nvidia_driver"
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libjpeg-dev \
        libpng-dev
COPY --from=conda-installs /opt/conda /opt/conda
RUN if test -n "${TRITON_VERSION}" -a "${TARGETPLATFORM}" != "linux/arm64"; then \
        apt install -y --no-install-recommends gcc; \
    fi
RUN rm -rf /var/lib/apt/lists/*
ENV PATH /opt/conda/bin:$PATH
ENV NVIDIA_VISIBLE_DEVICES all
ENV NVIDIA_DRIVER_CAPABILITIES compute,utility
ENV LD_LIBRARY_PATH /usr/local/nvidia/lib:/usr/local/nvidia/lib64
ENV PYTORCH_VERSION ${PYTORCH_VERSION}
WORKDIR /workspace

FROM official as dev
# Should override the already installed version from the official-image stage
COPY --from=build /opt/conda /opt/conda
COPY --from=build /opt/pytorch /opt/pytorch
