// --------------------------------------------------------
// Copyright (C) quicksilicon.io - All Rights Reserved
//
// Unauthorized copying of this file, via any medium is
// strictly prohibited
// Proprietary and confidential
// --------------------------------------------------------

// --------------------------------------------------------
// Find First - RTL
// --------------------------------------------------------

module qs_find_first #(
  parameter NUM_PORTS = 16
)(
  input   logic                  clk,
  input   logic                  reset_n,

  // Input ports
  input   logic [NUM_PORTS-1:0]  req_i,

  output  logic [NUM_PORTS-1:0]  first_set_o
);

  // --------------------------------------------------------
  // Internal wire and regs
  // --------------------------------------------------------
  logic [NUM_PORTS-1:0] first_set;

  // --------------------------------------------------------
  // Find first logic
  // --------------------------------------------------------
  assign first_set[0] = req_i[0];
  for (genvar i = 1; i < NUM_PORTS; i++) begin
    assign first_set[i] = req_i[i] & ~(|first_set[i-1:0]);
  end

  // -------------------------------------------------------
  // Output assignments
  // --------------------------------------------------------
  assign first_set_o = first_set;
endmodule
