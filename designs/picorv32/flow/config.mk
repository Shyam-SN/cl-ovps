PROJECT_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../../..)
PICORV32_FLOW_DIR := $(PROJECT_ROOT)/designs/picorv32/flow

export DESIGN_NAME = picorv32_top
export DESIGN_NICKNAME = picorv32_baseline
export PLATFORM = sky130hd
export DESIGN_HOME = $(PROJECT_ROOT)/designs
export FLOW_VARIANT = baseline
export WORK_HOME = $(PROJECT_ROOT)/build/orfs

export VERILOG_FILES = \
  $(PICORV32_FLOW_DIR)/rtl/picorv32_top.v \
  $(PROJECT_ROOT)/designs/picorv32/picorv32.v
export SDC_FILE = $(PICORV32_FLOW_DIR)/constraints/picorv32.sdc

export DIE_AREA = 0 0 520 520
export CORE_AREA = 20 20 500 500
export CLOCK_PERIOD = 20.0
export PLACE_DENSITY = 0.55
