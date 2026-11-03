// slang arguments for ~/work/sniper/sniper.py (mirrors the Makefile, DUT=rtl)
--top tb_top
+define+UVM_NO_DPI
+define+UVM_REPORT_DISABLE_FILE_LINE
+incdir+/Users/rahulbehl/work/uvm-verilator/src
+incdir+tb
+incdir+mem_agent
+incdir+axi_agent
+incdir+env
+incdir+tests
/Users/rahulbehl/work/uvm-verilator/src/uvm_pkg.sv
../axi_pkg.sv
../../../../questions/shared/verilog/qs_find_first.sv
../../../../questions/shared/verilog/qs_pop_cnt.sv
../axi_master.sv
tb/mem_bus_if.sv
tb/axi_bus_if.sv
tb/axi_uvm_pkg.sv
tb/tb_top.sv
