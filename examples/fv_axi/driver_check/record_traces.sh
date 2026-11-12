#!/bin/sh
# Build the qs-ques AXI UVM testbench with axi_trace_rec.sv bound in (Verilator) and record the AXI bus of
# every test: <out>/<test>.<seed>.trc, one row per clock edge.
#
#   record_traces.sh <out dir> [seed]        (QS: qs-ques checkout; UVM_SRC: a UVM 2020 src dir)
set -e
OUT=${1:?out dir}; SEED=${2:-1}
QS=${QS:-$HOME/work/sniper_corpus/qs-ques}
UVM=${UVM_SRC:-$HOME/work/sniper_corpus/uvm-core/src}
T=$QS/tutorials/sv_testbench/axi_slave_testbench; H=$T/uvm_tb; S=$QS/questions/shared/verilog
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
if [ ! -x "$OUT/build/Vtb_top" ]; then
  verilator --binary --timing -j 8 -Wno-fatal -Wno-lint -Wno-style --timescale-override 1ns/1ps \
    --top-module tb_top -Mdir "$OUT/build" +define+UVM_NO_DPI +define+UVM_REPORT_DISABLE_FILE_LINE \
    +incdir+$UVM +incdir+$H/tb +incdir+$H/mem_agent +incdir+$H/axi_agent +incdir+$H/env +incdir+$H/tests \
    $UVM/uvm_pkg.sv $T/axi_pkg.sv $S/qs_find_first.sv $S/qs_pop_cnt.sv $T/axi_master.sv \
    $H/tb/mem_bus_if.sv $H/tb/axi_bus_if.sv $H/tb/axi_uvm_pkg.sv $H/tb/tb_top.sv "$HERE/axi_trace_rec.sv" \
    -o Vtb_top > "$OUT/build.log" 2>&1
fi
for t in axi_smoke_rd_test axi_smoke_wr_test axi_rd_rand_test axi_wr_rand_test axi_rw_rand_test \
         axi_wr_rd_data_test axi_ooo_test axi_backpressure_test axi_stress_test; do
  "$OUT/build/Vtb_top" +UVM_TESTNAME=$t +UVM_NO_RELNOTES +MAX_ERRORS=1 +verilator+seed+$SEED \
    +AXI_TRACE="$OUT/$t.$SEED.trc" > "$OUT/$t.$SEED.log" 2>&1
  printf '%-24s %s %6s cycles\n' $t "$(grep -q 'TEST PASSED' "$OUT/$t.$SEED.log" && echo PASS || echo FAIL)" \
    "$(wc -l < "$OUT/$t.$SEED.trc" | tr -d ' ')"
done
