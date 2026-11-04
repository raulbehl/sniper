// Checker bound into rf_dut by rtl_bind.sv (test_fvgen.py T5): default clocking / disable iff,
// a named sequence, |=>.
module rf_chk (input logic clk_i, input logic rst_ni, input logic wr_i, input logic [3:0] level_q);
  default clocking @(posedge clk_i); endclocking
  default disable iff (!rst_ni);
  sequence full_write_S;
    wr_i && level_q == 4'd8;
  endsequence
  full_hold_A: assert property (full_write_S |=> level_q == 4'd8);
endmodule
