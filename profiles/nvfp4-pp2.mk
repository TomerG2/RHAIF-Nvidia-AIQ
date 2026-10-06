# Two single-L4 workers; GPU disk includes room for model cache.
GPU_INSTANCE_TYPE ?= g6.2xlarge
GPU_REPLICAS ?= 2
GPU_ROOT_VOLUME_SIZE ?= 500
