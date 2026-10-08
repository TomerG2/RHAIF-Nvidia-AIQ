# Uses four existing B200 nodes with eight GPUs each; install needs no cloud SKU.
# If provisioning instead, explicitly select a matching eight-B200 cloud SKU.
GPU_INSTANCE_TYPE_REQUIRED := true
GPU_VM_SIZE_REQUIRED := true
GPU_SKU_REQUIREMENT := a SKU with eight B200 GPUs per node
GPU_REPLICAS ?= 4
GPU_REPLICAS_AZURE ?= 4
GPU_COUNT ?= 8
GPU_ROOT_VOLUME_SIZE ?= 500
