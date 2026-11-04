# Formal run of the qs-ques AXI master, stimulus from its UVM constraints

`axi_fv_top.sv` drives the AXI master (`qs-ques/tutorials/sv_testbench/axi_slave_testbench`) with
`fv_r_mem_req_item`, which fvgen generates from the UVM testbench's `mem_req_item` constraints
(`c_addr`, `c_run`, `c_shape`). The glue is `mem_driver`'s request loop as RTL; the AXI slave
side is unconstrained.

```sh
# 1. extract the UVM testbench with sniper (axi_uvm_tb_constructs.json, generated) and
# 2. turn its constraints into assumes and a randomize() model (fv/)
make fv QS=/path/to/qs-ques UVM=/path/to/uvm-core PYTHON=/path/to/python-with-pyslang
# 3. the qs-ques formal targets
make formal_cover      # 6/6 covers reached (full-line / full-lanes writes, 1-byte read, partial
                       #   AXI strobe, wlast, read issue)
make formal_assert     # PASS to depth 30: every request is a contiguous run inside one line
```

Control: with `#(.CM_c_run(1'b0))` on `u_item` (`constraint_mode(0)` of `c_run`) `formal_assert`
fails `req_in_line` at step 1, i.e. the proof rests on the generated assumptions.

The DUT goes through `read_slang` (yosys-slang): Yosys' `read_verilog` cannot parse
`module axi_master import axi_pkg::*;`. The generated modules and the top go through
`read_verilog -sv -formal -DFORMAL`, as in `questions/shared/make.cfg`.
