# Formal run of the qs-ques AXI master with stimulus from its UVM testbench's constraints.
# Same structure as questions/shared/make.cfg (formal_cover / formal_assert, smtbmc), except that the
# DUT is read with yosys-slang: read_verilog cannot parse `module axi_master import axi_pkg::*;`.
#
#   make fv              sniper extraction of the UVM testbench (if missing) -> fvgen collateral in fv/
#   make formal          formal_cover + formal_assert
#
QS        ?= $(HOME)/work/sniper_corpus/qs-ques
AXI       ?= $(QS)/tutorials/sv_testbench/axi_slave_testbench
PRIM      ?= $(QS)/tutorials/formal_verification
UVM       ?= $(HOME)/work/uvm-verilator
SNIPER    ?= $(abspath ../..)
PYTHON    ?= python3
FV_JSON   ?= axi_uvm_tb_constructs.json      # sniper output of the AXI UVM testbench (generated)
COVER_DEPTH  ?= 30
ASSERT_DEPTH ?= 30

DUT_FILES = $(AXI)/axi_pkg.sv $(QS)/questions/shared/verilog/qs_find_first.sv \
            $(QS)/questions/shared/verilog/qs_pop_cnt.sv $(AXI)/axi_master.sv
FV_FILES  = fv/fv_constraints.sv axi_fv_top.sv

$(FV_JSON):
	cd $(AXI)/uvm_tb && $(PYTHON) $(SNIPER)/sniper.py --own . -o $(abspath $(FV_JSON)) -- \
	    +incdir+$(UVM)/src $(UVM)/src/uvm_pkg.sv -f $(SNIPER)/examples/axi_uvm_tb/sniper.f

fv: $(FV_JSON)
	$(PYTHON) $(SNIPER)/fvgen.py $(FV_JSON) -o fv --prim-assert-dir $(PRIM)

ALL_FILES = $(abspath $(DUT_FILES) $(FV_FILES) fv/prim_assert.sv fv/prim_assert_yosys_macros.svh)

# $(1) = cover | assert, $(2) = sby mode, $(3) = depth   (written like make.cfg's formal_build_*)
define sby_build
	@echo "[options]" > formal_$(1).sby
	@echo "mode $(2)" >> formal_$(1).sby
	@echo "depth $(3)" >> formal_$(1).sby
	@echo "[engines]" >> formal_$(1).sby
	@echo "smtbmc" >> formal_$(1).sby
	@echo "[script]" >> formal_$(1).sby
	@echo "plugin -i slang" >> formal_$(1).sby
	@echo "read_slang $(notdir $(DUT_FILES)) --top axi_master" >> formal_$(1).sby
	@echo "read_verilog -sv -formal -DFORMAL $(notdir $(FV_FILES))" >> formal_$(1).sby
	@echo "prep -top axi_fv_top" >> formal_$(1).sby
	@echo "[files]" >> formal_$(1).sby
	@printf "%s\n" $(ALL_FILES) >> formal_$(1).sby
endef

formal_build_cover:
	$(call sby_build,cover,cover,$(COVER_DEPTH))

formal_build_assert:
	$(call sby_build,assert,bmc,$(ASSERT_DEPTH))

formal_cover: formal_build_cover
	sby -f formal_cover.sby

formal_assert: formal_build_assert
	sby -f formal_assert.sby

formal: formal_cover formal_assert

clean:
	rm -rf fv formal_cover formal_assert formal_cover.sby formal_assert.sby $(FV_JSON)

.PHONY: fv formal formal_cover formal_assert formal_build_cover formal_build_assert clean
