// DUT for test_fvgen.py T5 (covergroups on the RTL): a FIFO level counter and a config register.
module rf_dut (
  input  logic       clk_i,
  input  logic       rst_ni,
  input  logic       wr_i,
  input  logic       rd_i,
  input  logic       cfg_we_i,
  input  logic [1:0] cfg_wd_i,
  output logic [3:0] level_o
);
  logic [3:0] level_q;
  logic [1:0] cfg_q;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      level_q <= '0;
      cfg_q <= '0;
    end else begin
      if (cfg_we_i) cfg_q <= cfg_wd_i;
      if (wr_i && !rd_i && level_q != 4'd8) level_q <= level_q + 4'd1;
      else if (rd_i && !wr_i && level_q != 4'd0) level_q <= level_q - 4'd1;
    end
  end
  logic busy;                       // internal: tapped by the testbench
  assign busy = level_q > {2'b0, cfg_q};
  assign level_o = level_q;
endmodule
