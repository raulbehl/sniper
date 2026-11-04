module smoke_fv_c_mem_req_item__body_L26 (input logic clk);
  `include "prim_assert.sv"
  (* anyseq *) logic [31:0] addr;
  (* anyseq *) logic [0:0] wr;
  (* anyseq *) logic [1:0] shape;
  (* anyseq *) logic [31:0] first_byte;
  (* anyseq *) logic [31:0] num_bytes;
  (* anyseq *) logic [127:0] wr_data;
  (* anyseq *) logic [0:0] ctx__want_wr;
  (* anyseq *) logic [1:0] ctx__want_shape;
  (* anyseq *) logic [31:0] ctx__addr_lo;
  (* anyseq *) logic [31:0] ctx__addr_hi;
  fv_c_mem_req_item__body_L26 u_dut (.clk(clk), .reset(1'b0), .en(1'b1), .addr(addr), .wr(wr), .shape(shape), .first_byte(first_byte), .num_bytes(num_bytes), .wr_data(wr_data), .ctx__want_wr(ctx__want_wr), .ctx__want_shape(ctx__want_shape), .ctx__addr_lo(ctx__addr_lo), .ctx__addr_hi(ctx__addr_hi));
  always_ff @(posedge clk) sat: cover (1'b1);   // the constraints have a solution
endmodule
