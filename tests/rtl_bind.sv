// Binds rf_chk into rf_dut (test_fvgen.py T5).
module rf_bind;
  bind rf_dut rf_chk u_chk (.clk_i, .rst_ni, .wr_i, .level_q);
endmodule
