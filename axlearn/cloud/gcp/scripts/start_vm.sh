#!/usr/bin/env bash
#
# Copyright © 2023 Apple Inc.
#
# Boot script for GCP VM.
#
# Downloads install-bundle from metadata:bucket, installs, and sets label:boot_status = 'done'.

echo "=== AXLearn start_vm.sh ==="

# Setup logs.
BASE_LOG_ROOT="/output/logs"
mkdir -p ${BASE_LOG_ROOT}
chmod -R 776 ${BASE_LOG_ROOT}
SETUP_LOG_PATH="${BASE_LOG_ROOT}/setup.log"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') : $1" >> ${SETUP_LOG_PATH}
}

# Exit on error.
# Try to set label:boot_status = `failed` and  upload the logs file to GCS when exit.
set -e
trap 'echo "Bootstrap failed." >> ${SETUP_LOG_PATH}; \
      gcloud storage cp ${SETUP_LOG_PATH} "${GS_JOB_PATH}/logs/setup_log-$(hostname)"; \
      gcloud compute instances add-labels ${JOB_NAME} --labels=boot_status=failed --zone ${ZONE}; \
      exit 1' ERR

# Ensure that gcloud is installed.
while [[ ! -x $(which gcloud) ]]
do
  log "gcloud not found, installing..."
  # Install google-cloud-sdk via apt.
  echo "deb https://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list
  apt-get update && apt-get -y install google-cloud-sdk
  sleep 1
done

# Get env-vars
get_metadata() {
  curl http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1 -H "Metadata-Flavor: Google"
}

BUCKET=$(get_metadata bundle_bucket)
JOB_NAME=$(get_metadata job_name)
ZONE=$(get_metadata zone)
DOCKER_REGISTRY=$(get_metadata docker_registry)
BUNDLER_TYPE=$(get_metadata bundler_type)

if [[ -z ${BUCKET} || -z ${JOB_NAME} || -z ${ZONE} || -z ${DOCKER_REGISTRY} || -z ${BUNDLER_TYPE} ]]; then
  log "Required metadata is missing. Exiting."
  false
fi

log "Using bundler type: ${BUNDLER_TYPE}"

tar_bundlers=("tar" "gcs")
docker_bundlers=("docker" "artifactregistry" "cloudbuild")

if [[ " ${tar_bundlers[*]} " =~ " ${BUNDLER_TYPE} " ]]; then
  log "Running tar bundler setup..."

  until apt install lsof; do
    # We want lsof so we can monitor when the lock & lock-frontend are available.
    sleep 1
  done

  # Block until lock & lock-frontend are available.
  while [[ ! -z $(lsof /var/lib/dpkg/lock-frontend) || ! -z $(lsof /var/lib/dpkg/lock) ]]
  do
    log "Waiting for dpkg locks to release."
    sleep 1
  done

  # Install screen as it's seemingly the only way to persist via gcloud ssh command.
  yes | apt-get install screen htop tmux >> ${SETUP_LOG_PATH} 2>&1

  # Unpack bundle.
  GS_JOB_PATH="gs://${BUCKET}/axlearn/jobs/${JOB_NAME}"
  GS_BUNDLE_PATH="${GS_JOB_PATH}/axlearn.tar.gz"
  LOCAL_BUNDLE_PATH="/root"
  mkdir -p ${LOCAL_BUNDLE_PATH}
  pushd ${LOCAL_BUNDLE_PATH}
  (gsutil cp ${GS_BUNDLE_PATH} "${LOCAL_BUNDLE_PATH}/axlearn.tar.gz" && tar -xzf "axlearn.tar.gz") \
  >> ${SETUP_LOG_PATH} 2>&1

  # Install + activate Python 3.10.
  install_py310() {
    wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O miniconda.sh
    /bin/bash miniconda.sh -b -p /opt/conda
    source /opt/conda/etc/profile.d/conda.sh
    # Accept conda ToS to allow conda env creation
    conda tos accept
    conda create -y -n py310 python=3.10
    conda activate py310
    conda info --envs
    # Add conda to .profile file, and use login shell to source.
    echo 'source /opt/conda/etc/profile.d/conda.sh && conda activate py310' >> ~/.profile
  }
  install_py310 >> ${SETUP_LOG_PATH} 2>&1
  log "Using python3: $(which python3)"

  # Install bundle.
  install_bundle() {
    # 'Until' seemingly necessary as of 08/06/23 to avoid background setup process damaging partial
    # pip install state.
    # TODO(markblee,tom_gunter): Revisit this and understand why and how to wait until ready.
    until python3 -m pip install .[gcp]; do
      echo "Attempting to install bundle again..."
      sleep 1
    done
  }
  install_bundle >> ${SETUP_LOG_PATH} 2>&1
  popd
fi

# Run after gcloud is guaranteed to be available.
if [[ " ${docker_bundlers[*]} " =~ " ${BUNDLER_TYPE} " ]]; then
  log "Running docker bundler setup..."

  while [[ ! -x $(which docker) ]]
  do
    log "docker not found, installing..."
    apt-get update && apt-get -y install docker.io >> ${SETUP_LOG_PATH} 2>&1
    sleep 1
  done
  log "Using docker version: $(docker --version)"
  # Docker auth.
  yes | gcloud auth configure-docker ${DOCKER_REGISTRY} >> ${SETUP_LOG_PATH} 2>&1
fi

# Set label if possible (not possible on TPU-VM).
gcloud compute instances add-labels "${JOB_NAME}" \
  --labels=boot_status=done \
  --zone ${ZONE} >> ${SETUP_LOG_PATH} 2>&1

# Copy logs.
gcloud storage cp ${SETUP_LOG_PATH} "${GS_JOB_PATH}/logs/setup_log-$(hostname)"
