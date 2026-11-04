// --------------------------------------------------------
// Copyright (C) quicksilicon.io - All Rights Reserved
//
// Unauthorized copying of this file, via any medium is
// strictly prohibited
// Proprietary and confidential
// --------------------------------------------------------

// --------------------------------------------------------
// Population Count - RTL
// --------------------------------------------------------

module qs_pop_cnt #(
  parameter N = 16,
  parameter W = $clog2(N+1)
)(
  /* verilator lint_off UNUSEDSIGNAL */
  input   logic          clk,
  input   logic          reset,
  /* verilator lint_on UNUSEDSIGNAL */

  input   logic [N-1:0]  req_i,
  output  logic [W-1:0]  pop_cnt_o
);

  // --------------------------------------------------------
  // Internal wire and regs
  // --------------------------------------------------------
  logic [W-1:0] pop_cnt;

  // --------------------------------------------------------
  // Population count logic
  // --------------------------------------------------------
  always_comb begin
    pop_cnt = '0;
    for (int i = 0; i < N; i++) begin
      pop_cnt = pop_cnt + req_i[i];
    end
  end

  // -------------------------------------------------------
  // Output assignments
  // --------------------------------------------------------
  assign pop_cnt_o = pop_cnt;
endmodule

