module smoke_fv_c_mem_req_item__body_L28 (input logic clk);
  `include "prim_assert.sv"
  (* anyseq *) logic [31:0] addr;
  (* anyseq *) logic [0:0] wr;
  (* anyseq *) logic [1:0] shape;
  (* anyseq *) logic [31:0] first_byte;
  (* anyseq *) logic [31:0] num_bytes;
  (* anyseq *) logic [127:0] wr_data;
  (* anyseq *) logic [31:0] ctx__wr_req__addr;
  (* anyseq *) logic [31:0] ctx__wr_req__first_byte;
  (* anyseq *) logic [31:0] ctx__wr_req__num_bytes;
  fv_c_mem_req_item__body_L28 u_dut (.clk(clk), .reset(1'b0), .en(1'b1), .addr(addr), .wr(wr), .shape(shape), .first_byte(first_byte), .num_bytes(num_bytes), .wr_data(wr_data), .ctx__wr_req__addr(ctx__wr_req__addr), .ctx__wr_req__first_byte(ctx__wr_req__first_byte), .ctx__wr_req__num_bytes(ctx__wr_req__num_bytes));
  always_ff @(posedge clk) sat: cover (1'b1);   // the constraints have a solution
endmodule
