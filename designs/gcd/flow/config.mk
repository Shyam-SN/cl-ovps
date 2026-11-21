PROJECT_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../../..)

export DESIGN_NAME = gcd
export DESIGN_NICKNAME = gcd_power
export PLATFORM = sky130hd
export DESIGN_HOME = $(PROJECT_ROOT)/tools/openroad-flow-scripts/flow/designs
export FLOW_VARIANT = gcd_baseline20ns
export WORK_HOME = $(PROJECT_ROOT)/build/orfs

export VERILOG_FILES = $(PROJECT_ROOT)/designs/gcd/gcd.v
export SDC_FILE = $(PROJECT_ROOT)/designs/gcd/flow/constraints/gcd.sdc
export CLOCK_PERIOD = 20.0
export PLACE_DENSITY = 0.55
export CORE_UTILIZATION = 38
export TNS_END_PERCENT = 100
export ADDER_MAP_FILE :=
export SWAP_ARITH_OPERATORS = 1
export OPENROAD_HIERARCHICAL = 1
