// slang arguments for ~/work/sniper/sniper.py (mirrors the Makefile, DUT=rtl); paths relative to the
// testbench's uvm_tb directory. UVM is given separately: +incdir+<uvm>/src <uvm>/src/uvm_pkg.sv
--top tb_top
+define+UVM_NO_DPI
+define+UVM_REPORT_DISABLE_FILE_LINE
+incdir+tb
+incdir+mem_agent
+incdir+axi_agent
+incdir+env
+incdir+tests
../axi_pkg.sv
../../../../questions/shared/verilog/qs_find_first.sv
../../../../questions/shared/verilog/qs_pop_cnt.sv
../axi_master.sv
tb/mem_bus_if.sv
tb/axi_bus_if.sv
tb/axi_uvm_pkg.sv
tb/tb_top.sv
