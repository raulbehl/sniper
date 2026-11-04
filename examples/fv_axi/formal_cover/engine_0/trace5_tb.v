`ifndef VERILATOR
module testbench;
  reg [4095:0] vcdfile;
  reg clock;
`else
module testbench(input clock, output reg genclock);
  initial genclock = 1;
`endif
  reg genclock = 1;
  reg [31:0] cycle = 0;
  reg [0:0] PI_reset;
  wire [0:0] PI_clk = clock;
  axi_fv_top UUT (
    .reset(PI_reset),
    .clk(PI_clk)
  );
`ifndef VERILATOR
  initial begin
    if ($value$plusargs("vcd=%s", vcdfile)) begin
      $dumpfile(vcdfile);
      $dumpvars(0, testbench);
    end
    #5 clock = 0;
    while (genclock) begin
      #5 clock = 0;
      #5 clock = 1;
    end
  end
`endif
  initial begin
`ifndef VERILATOR
    #1;
`endif
    // UUT.$auto$async2sync.\cc:116:execute$2136  = 1'b1;
    // UUT.$auto$async2sync.\cc:116:execute$2142  = 1'b1;
    // UUT.$auto$async2sync.\cc:116:execute$2148  = 1'b1;
    // UUT.$auto$async2sync.\cc:116:execute$2154  = 1'b1;
    // UUT.$auto$async2sync.\cc:116:execute$2160  = 1'b1;
    // UUT.$auto$async2sync.\cc:116:execute$2166  = 1'b1;
    UUT.DUT._witness_.anyinit_driver_arvalid_q = 1'b0;
    UUT.DUT._witness_.anyinit_driver_awvalid_q = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_rd_buffer_id_q = 2'b00;
    UUT.DUT._witness_.anyinit_driver_axi_rd_buffer_valid_q_0_0_ = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_rd_buffer_valid_q_1_1_ = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_rd_buffer_valid_q_2_2_ = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_rd_buffer_valid_q_3_3_ = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_rd_xfer_cnt_q = 2'b00;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_0__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_0__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_0__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_0__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_10__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_10__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_10__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_10__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_11__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_11__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_11__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_11__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_12__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_12__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_12__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_12__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_13__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_13__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_13__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_13__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_14__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_14__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_14__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_14__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_15__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_15__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_15__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_15__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_1__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_1__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_1__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_1__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_2__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_2__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_2__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_2__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_3__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_3__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_3__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_3__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_4__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_4__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_4__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_4__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_5__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_5__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_5__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_5__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_6__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_6__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_6__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_6__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_7__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_7__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_7__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_7__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_8__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_8__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_8__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_8__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_9__ar_naw = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_9__done = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_9__pending_req = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_tracker_state_q_9__valid = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_wr_buffer_valid_q_0_0_ = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_wr_buffer_valid_q_1_1_ = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_wr_buffer_valid_q_2_2_ = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_wr_buffer_valid_q_3_3_ = 1'b0;
    UUT.DUT._witness_.anyinit_driver_axi_wr_xfer_cnt_q = 2'b00;
    UUT.DUT._witness_.anyinit_driver_mem_cpu_id_q = 4'b0000;
    UUT.DUT._witness_.anyinit_driver_mem_cpu_rd_data_q = 128'b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
    UUT.DUT._witness_.anyinit_driver_mem_cpu_valid_q = 1'b0;
    UUT.DUT._witness_.anyinit_driver_w_xfer_inprog_q = 1'b0;
    UUT.DUT._witness_.anyinit_driver_wvalid_q = 1'b0;
    UUT.DUT.ar_req_entry_pld_q = 63'b000000000000000000000000000000000000000000000000000000000000000;
    UUT.DUT.aw_req_entry_pld_q = 63'b000000000000000000000000000000000000000001000000000000000000000;
    UUT.DUT.axi_rd_buffer_q = 512'b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
    UUT.DUT.axi_tracker_q = 1008'b000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000;
    UUT.DUT.axi_wr_buffer_q = 512'b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
    UUT.DUT.axi_wr_data_q = 128'b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
    UUT.DUT.axi_wr_strb_q = 16'b0000000000000000;
    UUT.DUT.old_entry_q = 256'b0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
    UUT.rst_for_cycle = 1'b0;
    UUT.u_item.addr = 32'b00000000000000000000000000000000;
    UUT.u_item.first_byte = 32'b00000000000000000000000000000000;
    UUT.u_item.num_bytes = 32'b00000000000000000000000000000000;
    UUT.u_item.shape = 2'b00;
    UUT.u_item.wr = 1'b0;
    UUT.u_item.wr_data = 128'b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
    UUT.valid = 1'b0;

    // state 0
    PI_reset = 1'b1;
    UUT.ar_ready = 1'b0;
    UUT.aw_ready = 1'b0;
    UUT.b_id = 3'b000;
    UUT.b_valid = 1'b0;
    UUT.r_data = 32'b00000000000000000000000000000000;
    UUT.r_id = 3'b000;
    UUT.r_last = 1'b0;
    UUT.r_valid = 1'b0;
    UUT.rsp_ready = 1'b0;
    UUT.w_ready = 1'b0;
    UUT.u_item.nxt_addr = 32'b00000000000000000000000000000000;
    UUT.u_item.nxt_first_byte = 32'b10000000000000000000000000000000;
    UUT.u_item.nxt_num_bytes = 32'b00000000000000000000000000000000;
    UUT.u_item.nxt_shape = 2'b00;
    UUT.u_item.nxt_wr = 1'b0;
    UUT.u_item.nxt_wr_data = 128'b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_reset <= 1'b0;
      UUT.ar_ready <= 1'b0;
      UUT.aw_ready <= 1'b0;
      UUT.b_id <= 3'b000;
      UUT.b_valid <= 1'b0;
      UUT.r_data <= 32'b00000000000000000000000000000000;
      UUT.r_id <= 3'b000;
      UUT.r_last <= 1'b0;
      UUT.r_valid <= 1'b0;
      UUT.rsp_ready <= 1'b0;
      UUT.w_ready <= 1'b0;
      UUT.u_item.nxt_addr <= 32'b00000000000000000000000000000000;
      UUT.u_item.nxt_first_byte <= 32'b00000000000000000000000000001100;
      UUT.u_item.nxt_num_bytes <= 32'b00000000000000000000000000000011;
      UUT.u_item.nxt_shape <= 2'b10;
      UUT.u_item.nxt_wr <= 1'b1;
      UUT.u_item.nxt_wr_data <= 128'b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
    end

    // state 2
    if (cycle == 1) begin
      PI_reset <= 1'b0;
      UUT.ar_ready <= 1'b0;
      UUT.aw_ready <= 1'b0;
      UUT.b_id <= 3'b000;
      UUT.b_valid <= 1'b0;
      UUT.r_data <= 32'b00000000000000000000000000000000;
      UUT.r_id <= 3'b000;
      UUT.r_last <= 1'b0;
      UUT.r_valid <= 1'b0;
      UUT.rsp_ready <= 1'b0;
      UUT.w_ready <= 1'b0;
      UUT.u_item.nxt_addr <= 32'b00000000000000000000000000000000;
      UUT.u_item.nxt_first_byte <= 32'b10000000000000000000000000000000;
      UUT.u_item.nxt_num_bytes <= 32'b00000000000000000000000000000000;
      UUT.u_item.nxt_shape <= 2'b00;
      UUT.u_item.nxt_wr <= 1'b0;
      UUT.u_item.nxt_wr_data <= 128'b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
    end

    // state 3
    if (cycle == 2) begin
      PI_reset <= 1'b0;
      UUT.ar_ready <= 1'b0;
      UUT.aw_ready <= 1'b0;
      UUT.b_id <= 3'b010;
      UUT.b_valid <= 1'b0;
      UUT.r_data <= 32'b00000000000000000000000000000000;
      UUT.r_id <= 3'b010;
      UUT.r_last <= 1'b0;
      UUT.r_valid <= 1'b0;
      UUT.rsp_ready <= 1'b0;
      UUT.w_ready <= 1'b0;
      UUT.u_item.nxt_addr <= 32'b00000000000000000000000000000000;
      UUT.u_item.nxt_first_byte <= 32'b00000000000000000000000000000000;
      UUT.u_item.nxt_num_bytes <= 32'b00000000000000000000000000010000;
      UUT.u_item.nxt_shape <= 2'b00;
      UUT.u_item.nxt_wr <= 1'b0;
      UUT.u_item.nxt_wr_data <= 128'b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
    end

    // state 4
    if (cycle == 3) begin
      PI_reset <= 1'b0;
      UUT.ar_ready <= 1'b1;
      UUT.aw_ready <= 1'b1;
      UUT.b_id <= 3'b000;
      UUT.b_valid <= 1'b0;
      UUT.r_data <= 32'b00000000000000000000000000000000;
      UUT.r_id <= 3'b000;
      UUT.r_last <= 1'b0;
      UUT.r_valid <= 1'b0;
      UUT.rsp_ready <= 1'b0;
      UUT.w_ready <= 1'b1;
      UUT.u_item.nxt_addr <= 32'b00000000000000000000000000000000;
      UUT.u_item.nxt_first_byte <= 32'b00000000000000000000000000000000;
      UUT.u_item.nxt_num_bytes <= 32'b10000000000000000000000000000000;
      UUT.u_item.nxt_shape <= 2'b00;
      UUT.u_item.nxt_wr <= 1'b0;
      UUT.u_item.nxt_wr_data <= 128'b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
    end

    // state 5
    if (cycle == 4) begin
      PI_reset <= 1'b0;
      UUT.ar_ready <= 1'b0;
      UUT.aw_ready <= 1'b0;
      UUT.b_id <= 3'b000;
      UUT.b_valid <= 1'b0;
      UUT.r_data <= 32'b00000000000000000000000000000000;
      UUT.r_id <= 3'b000;
      UUT.r_last <= 1'b0;
      UUT.r_valid <= 1'b0;
      UUT.rsp_ready <= 1'b0;
      UUT.w_ready <= 1'b0;
      UUT.u_item.nxt_addr <= 32'b00000000000000000000000000000000;
      UUT.u_item.nxt_first_byte <= 32'b00000000000000000000000000000000;
      UUT.u_item.nxt_num_bytes <= 32'b00000000000000000000000000010000;
      UUT.u_item.nxt_shape <= 2'b00;
      UUT.u_item.nxt_wr <= 1'b0;
      UUT.u_item.nxt_wr_data <= 128'b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000;
    end

    genclock <= cycle < 5;
    cycle <= cycle + 1;
  end
endmodule
