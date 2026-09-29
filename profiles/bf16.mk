# BF16 serving config. Needs a GPU that can hold the weights; Phase 0 requires an 80 GiB class SKU.
GPU_INSTANCE_TYPE_REQUIRED := true
GPU_VM_SIZE_REQUIRED := true
GPU_REPLICAS ?= 1
