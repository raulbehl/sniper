// --------------------------------------------------------
// Copyright (C) quicksilicon.io - All Rights Reserved
//
// Unauthorized copying of this file, via any medium is
// strictly prohibited
// Proprietary and confidential
// --------------------------------------------------------

// --------------------------------------------------------
// AXI Master - RTL
// --------------------------------------------------------

module axi_master import axi_pkg::*; (
  input   logic                                   clk,
  input   logic                                   reset_n,

  // Memory Request interface from CPU
  input   logic                                   cpu_mem_valid_i,
  input   logic [MEM_ID_WIDTH-1:0]                cpu_mem_id_i,
  input   logic [MEM_ADDR_WIDTH-1:0]              cpu_mem_addr_i,
  input   logic [MEM_BYTE_EN_WIDTH-1:0]           cpu_mem_byte_en_i,
  input   logic                                   cpu_mem_wr_i,
  input   logic [MEM_DATA_WIDTH-1:0]              cpu_mem_wr_data_i,
  output  logic                                   cpu_mem_ready_o,

  // Memory Response interface to CPU
  output  logic                                   mem_cpu_valid_o,
  output  logic [MEM_ID_WIDTH-1:0]                mem_cpu_id_o,
  output  logic [MEM_DATA_WIDTH-1:0]              mem_cpu_rd_data_o,
  input   logic                                   mem_cpu_ready_i,

  // AW Channel
  output  logic                                   axi_awvalid_o,
  output  logic [AXI_ID_WIDTH-1:0]                axi_awid_o,
  output  logic [AXI_ADDR_WIDTH-1:0]              axi_awaddr_o,
  output  logic [AXI_LEN_WIDTH-1:0]               axi_awlen_o,
  output  logic [AXI_SIZE_WIDTH-1:0]              axi_awsize_o,
  input   logic                                   axi_awready_i,

  // W Channel
  output  logic                                   axi_wvalid_o,
  output  logic [AXI_DATA_WIDTH-1:0]              axi_wdata_o,
  output  logic [AXI_STRB_WIDTH-1:0]              axi_wstrb_o,
  output  logic                                   axi_wlast_o,
  input   logic                                   axi_wready_i,

  // B Channel
  input   logic                                   axi_bvalid_i,
  input   logic [AXI_ID_WIDTH-1:0]                axi_bid_i,
  output  logic                                   axi_bready_o,

  // AR Channel
  output  logic                                   axi_arvalid_o,
  output  logic [AXI_ID_WIDTH-1:0]                axi_arid_o,
  output  logic [AXI_ADDR_WIDTH-1:0]              axi_araddr_o,
  output  logic [AXI_LEN_WIDTH-1:0]               axi_arlen_o,
  output  logic [AXI_SIZE_WIDTH-1:0]              axi_arsize_o,
  input   logic                                   axi_arready_i,

  // R Channel
  input   logic                                   axi_rvalid_i,
  input   logic [AXI_ID_WIDTH-1:0]                axi_rid_i,
  input   logic [AXI_DATA_WIDTH-1:0]              axi_rdata_i,
  input   logic                                   axi_rlast_i,
  output  logic                                   axi_rready_o
);

  // -------------------------------------------------------------------------------------
  // Internal wire and regs
  // -------------------------------------------------------------------------------------
  logic                                                                 cpu_mem_xfer;
  logic                                                                 cpu_mem_ready;
  logic           [MEM_DATA_WIDTH-1:0]                                  cpu_mem_wr_data;
  logic           [AXI_LEN-1:0]                                         cpu_mem_reduced_byte_en;
  logic           [MEM_BYTE_EN_WIDTH-1:0]                               cpu_mem_byte_en_first_set;
  logic           [AXI_LEN-1:0]                                         cpu_mem_reduced_one_hot;

  axi_ax_t                                                              axi_ar;
  axi_ax_t                                                              axi_aw;
  axi_ax_t        [AXI_TRACKER_DEPTH-1:0]                               axi_tracker_q;
  axi_ax_t        [AXI_TRACKER_DEPTH-1:0]                               nxt_axi_tracker;
  logic           [AXI_TRACKER_DEPTH-1:0]                               axi_tracker_en;
  axi_ax_state_t  [AXI_TRACKER_DEPTH-1:0]                               axi_tracker_state_q;
  logic           [AXI_TRACKER_DEPTH-1:0]                               axi_tracker_state_en;
  axi_ax_state_t  [AXI_TRACKER_DEPTH-1:0]                               nxt_axi_tracker_state;
  logic           [AXI_TRACKER_DEPTH-1:0]                               axi_tracker_entry_avail;
  logic           [AXI_TRACKER_DEPTH-1:0]                               axi_tracker_entry_valid;
  logic           [AXI_TRACKER_DEPTH-1:0]                               axi_tracker_free_entry;
  logic           [AXI_TRACKER_DEPTH-1:0]                               axi_tracker_free_entry_en;
  // Request tracking for AR and AW channels
  logic           [AXI_TRACKER_DEPTH-1:0]                               ar_entry_valid;
  logic           [AXI_TRACKER_DEPTH-1:0]                               ar_req_entry;
  axi_ax_t                                                              ar_req_entry_pld;
  axi_ax_t                                                              ar_req_entry_pld_q;
  logic                                                                 ar_req_entry_pld_en;
  logic           [AXI_TRACKER_DEPTH-1:0]                               aw_entry_valid;
  logic           [AXI_TRACKER_DEPTH-1:0]                               aw_req_entry;
  axi_ax_t                                                              aw_req_entry_pld;
  axi_ax_t                                                              aw_req_entry_pld_q;
  logic                                                                 aw_req_entry_pld_en;

  logic                                                                 nxt_arvalid;
  logic                                                                 arvalid_q;
  logic                                                                 nxt_awvalid;
  logic                                                                 awvalid_q;
  logic                                                                 nxt_wvalid;
  logic                                                                 wvalid_q;
  logic                                                                 w_xfer_inprog;
  logic                                                                 w_xfer_last;
  logic                                                                 w_xfer_inprog_q;
  logic                                                                 aw_w_xfer_done;
  
  logic           [AXI_TRACKER_DEPTH-1:0]                               r_resp_entry;
  logic                                                                 axi_r_xfer;
  axi_rd_buffer_t [AXI_RD_BUFFER_DEPTH-1:0]                             axi_rd_buffer_q;
  axi_rd_buffer_t [AXI_RD_BUFFER_DEPTH-1:0]                             nxt_axi_rd_buffer;
  logic           [AXI_RD_BUFFER_DEPTH-1:0][MEM_DATA_WIDTH-1:0]         axi_rdata_rot;
  logic           [AXI_RD_BUFFER_DEPTH-1:0]                             axi_rd_buffer_en;
  logic           [AXI_RD_BUFFER_WIDTH-1:0]                             axi_rd_buffer_id_q;
  logic                                                                 axi_rd_buffer_id_en;
  logic           [AXI_RD_BUFFER_DEPTH-1:0]                             axi_rd_buffer_valid_q;
  logic           [AXI_RD_BUFFER_DEPTH-1:0]                             nxt_axi_rd_buffer_valid;
  logic                                                                 axi_rd_buffer_valid_en;
  logic           [AXI_RD_BUFFER_DEPTH-1:0]                             axi_rd_buffer_avail;
  logic           [AXI_RD_BUFFER_DEPTH-1:0]                             axi_rd_buffer_pick_entry;
  logic           [AXI_BUFFER_ID_WIDTH-1:0]                             axi_rd_buffer_pick_id;
  logic           [AXI_LEN_WIDTH-1:0]                                   axi_rd_xfer_cnt_q;
  logic           [AXI_LEN_WIDTH-1:0]                                   nxt_axi_rd_xfer_cnt;
  
  logic           [AXI_LEN_WIDTH:0]                                     axi_len_raw;
  axi_len_t                                                             axi_len;
  logic           [MEM_BYTE_EN_WIDTH-1:0]                               axi_strb;
  logic           [LOG2_BYTE_EN_WIDTH-1:0]                              log2_strb;
  logic           [MEM_ADDR_WIDTH-1:0]                                  axi_addr;

  axi_ax_t                                                              axi_tracker_entry_pld;
  axi_ax_state_t                                                        axi_tracker_entry_state_pld;
  logic           [AXI_TRACKER_DEPTH-1:0]                               axi_tracker_done_entry;
  logic           [AXI_TRACKER_DEPTH-1:0]                               axi_tracker_pick_entry;

  logic           [MEM_DATA_WIDTH-1:0]                                  axi_wr_data;
  logic           [MEM_DATA_WIDTH-1:0]                                  nxt_axi_wr_data;
  logic           [MEM_DATA_WIDTH-1:0]                                  axi_wr_data_q;
  logic           [MEM_BYTE_EN_WIDTH-1:0]                               nxt_axi_wr_strb;
  logic           [MEM_BYTE_EN_WIDTH-1:0]                               axi_wr_strb_q;
  logic                                                                 axi_wr_pld_en;
  axi_wr_buffer_t [AXI_WR_BUFFER_DEPTH-1:0]                             axi_wr_buffer_q;
  axi_wr_buffer_t [AXI_WR_BUFFER_DEPTH-1:0]                             nxt_axi_wr_buffer;
  logic           [AXI_WR_BUFFER_DEPTH-1:0]                             axi_wr_buffer_en;
  logic           [AXI_WR_BUFFER_DEPTH-1:0]                             axi_wr_buffer_valid_q;
  logic           [AXI_WR_BUFFER_DEPTH-1:0]                             nxt_axi_wr_buffer_valid;
  logic                                                                 axi_wr_buffer_valid_en;
  logic           [AXI_WR_BUFFER_DEPTH-1:0]                             axi_wr_buffer_avail;
  logic           [AXI_WR_BUFFER_DEPTH-1:0]                             axi_wr_buffer_pick_entry;
  logic           [AXI_BUFFER_ID_WIDTH-1:0]                             axi_wr_buffer_pick_id;

  logic                                                                 axi_wr_xfer_en;
  logic           [AXI_LEN_WIDTH-1:0]                                   axi_wr_xfer_cnt_q;
  logic           [AXI_LEN_WIDTH-1:0]                                   nxt_axi_wr_xfer_cnt;

  logic           [AXI_TRACKER_DEPTH-1:0]                               b_resp_entry;

  logic                                                                 mem_cpu_valid_q;
  logic                                                                 nxt_mem_cpu_valid;
  logic                                                                 mem_cpu_free;
  logic                                                                 mem_cpu_pld_en;
  logic           [MEM_ID_WIDTH-1:0]                                    mem_cpu_id_q;
  logic           [MEM_DATA_WIDTH-1:0]                                  mem_cpu_rd_data_q;
  logic           [MEM_DATA_WIDTH-1:0]                                  nxt_mem_cpu_rd_data;
  logic                                                                 mem_cpu_rd_xfer;
  logic           [MEM_BYTE_EN_WIDTH-1:0]                               axi_tracker_entry_strb_shifted;

  logic           [AXI_TRACKER_DEPTH-1:0][AXI_TRACKER_DEPTH-1:0]        track_older;
  logic                                                                 track_older_en;
  logic           [AXI_TRACKER_DEPTH-1:0][AXI_TRACKER_DEPTH-1:0]        old_entry_q;
  logic           [AXI_TRACKER_DEPTH-1:0][AXI_TRACKER_DEPTH-1:0]        nxt_old_entry;
  logic           [AXI_TRACKER_DEPTH-1:0]                               axi_tracker_ordered;

  initial begin
    if ((MEM_DATA_WIDTH <= 0) || ((MEM_DATA_WIDTH & (MEM_DATA_WIDTH - 1)) != 0))
      $fatal(1, "MEM_DATA_WIDTH must be a power of two.");
    if ((AXI_DATA_WIDTH <= 0) || ((AXI_DATA_WIDTH & (AXI_DATA_WIDTH - 1)) != 0))
      $fatal(1, "AXI_DATA_WIDTH must be a power of two.");
    if ((MEM_DATA_WIDTH % AXI_DATA_WIDTH) != 0)
      $fatal(1, "MEM_DATA_WIDTH must be divisible by AXI_DATA_WIDTH.");
    if ((MEM_DATA_WIDTH % 8) != 0)
      $fatal(1, "MEM_DATA_WIDTH must be byte-granular.");
    if ((AXI_DATA_WIDTH % 8) != 0)
      $fatal(1, "AXI_DATA_WIDTH must be byte-granular.");
    if (MEM_BYTE_EN_WIDTH != (MEM_DATA_WIDTH / 8))
      $fatal(1, "MEM_BYTE_EN_WIDTH must be one bit per byte of MEM_DATA_WIDTH.");
    if (AXI_STRB_WIDTH*AXI_LEN != MEM_BYTE_EN_WIDTH)
      $fatal(1, "AXI_STRB_WIDTH * AXI_LEN must equal MEM_BYTE_EN_WIDTH.");
  end

  // -------------------------------------------------------------------------------------
  // CPU to MEM Interface ready
  // -------------------------------------------------------------------------------------
  assign cpu_mem_ready = |axi_tracker_entry_avail & (|axi_wr_buffer_avail | ~cpu_mem_wr_i);
  assign cpu_mem_xfer = cpu_mem_valid_i & cpu_mem_ready;

  // -------------------------------------------------------------------------------------
  // Request Tracking
  // -------------------------------------------------------------------------------------
  // Flops for AXI Tracker
  for (genvar i=0; i<AXI_TRACKER_DEPTH; i++) begin : g_axi_tracker
    always_ff @(posedge clk) begin
      if (axi_tracker_en[i])
        axi_tracker_q[i] <= nxt_axi_tracker[i];
    end
  end

  // Flops for tracker state
  for (genvar i=0; i<AXI_TRACKER_DEPTH; i++) begin : g_axi_tracker_state
    always_ff @(posedge clk or negedge reset_n) begin
      if (~reset_n)
        axi_tracker_state_q[i] <= '0;
      else if (axi_tracker_state_en[i])
        axi_tracker_state_q[i] <= nxt_axi_tracker_state[i];
    end
  end
  
  // -------------------------------------------------------------------------------------
  // Request tracker lookup for asserting new requests
  // -------------------------------------------------------------------------------------
  logic [AXI_ID_WIDTH-1:0] axi_id;
  assign axi_id = cpu_mem_addr_i[4+:AXI_ID_WIDTH];
  always_comb begin : g_req_tracker_lookup
    axi_tracker_state_en = '0;
    axi_tracker_en = '0;
    for (int i=0; i<AXI_TRACKER_DEPTH; i++) begin
      nxt_axi_tracker_state[i] = axi_tracker_state_q[i];
      nxt_axi_tracker[i] = axi_tracker_q[i];
      // Allocate the free entry selected
      if (axi_tracker_free_entry_en[i]) begin
        // Update the state based on read/write request from CPU to MEM interface
        axi_tracker_state_en[i] = 1'b1;
        nxt_axi_tracker_state[i].valid = 1'b1;
        nxt_axi_tracker_state[i].ar_naw = ~cpu_mem_wr_i;
        nxt_axi_tracker_state[i].pending_req = 1'b1;
        nxt_axi_tracker_state[i].done = 1'b0;

        // Store the addr, len and size
        axi_tracker_en[i] = 1'b1;
        nxt_axi_tracker[i].mem_id = cpu_mem_id_i;
        nxt_axi_tracker[i].axi_id = cpu_mem_addr_i[4+:AXI_ID_WIDTH];
        nxt_axi_tracker[i].buf_id = axi_wr_buffer_pick_id;
        nxt_axi_tracker[i].addr = axi_addr;
        nxt_axi_tracker[i].len = axi_len;
        nxt_axi_tracker[i].strb = axi_strb;
        nxt_axi_tracker[i].log2_strb = log2_strb;
      end

      // Deallocate the entry when either:
      //  - Read data is returned back to sender
      //  - BRESP is available
      if (axi_tracker_pick_entry[i] & mem_cpu_free) begin
        axi_tracker_state_en[i] = 1'b1;
        nxt_axi_tracker_state[i].valid = 1'b0;
      end

      // Update the entry when either:
      //  - AR request being sent out
      //  - AW request being sent out
      //  - Read response is available
      //  - Write response is available
      // Sending AR Request
      if (ar_req_entry[i] & (~arvalid_q | axi_arready_i)) begin
        axi_tracker_state_en[i] = 1'b1;
        nxt_axi_tracker_state[i].pending_req = 1'b0;
      end
      // Sending AW Request
      if (aw_req_entry[i] & aw_w_xfer_done) begin
        axi_tracker_state_en[i] = 1'b1;
        nxt_axi_tracker_state[i].pending_req = 1'b0;
      end
      // Read response available
      if (r_resp_entry[i] & axi_r_xfer) begin
        axi_tracker_state_en[i] = 1'b1;
        nxt_axi_tracker_state[i].done = axi_rlast_i;
        axi_tracker_en[i] = 1'b1;
        nxt_axi_tracker[i].buf_id = (|axi_rd_xfer_cnt_q) ? axi_tracker_q[i].buf_id : axi_rd_buffer_pick_id;
      end
      // Write response available
      if (b_resp_entry[i] & axi_bvalid_i) begin
        axi_tracker_state_en[i] = 1'b1;
        nxt_axi_tracker_state[i].done = 1'b1;
      end
    end
  end
  
  // Any entries not valid are available
  assign axi_tracker_entry_avail = ~axi_tracker_entry_valid;

  // Pick the first available entry which can be used to hold incoming req
  qs_find_first #(.NUM_PORTS(AXI_TRACKER_DEPTH)) ar_tracker_free_entry_ff (
    .clk            (clk),
    .reset_n        (reset_n),
    .req_i          (axi_tracker_entry_avail),
    .first_set_o    (axi_tracker_free_entry)
  );
  assign axi_tracker_free_entry_en = axi_tracker_free_entry & {AXI_TRACKER_DEPTH{cpu_mem_xfer}};

  // -------------------------------------------------------------------------------------
  // AxLen generation logic
  // -------------------------------------------------------------------------------------
  // Infer AXI len and size based on the CPU to MEM transfer
  for (genvar i=0; i<AXI_LEN; i++) begin : g_cpu_mem_reduced_byte_en
    assign cpu_mem_reduced_byte_en[i] = |cpu_mem_byte_en_i[i*AXI_STRB_WIDTH+:AXI_STRB_WIDTH];
  end

  qs_pop_cnt #(.N(AXI_LEN)) cpu_mem_byte_en_pop_cnt (
    .clk            (clk),
    .reset          (~reset_n),
    .req_i          (cpu_mem_reduced_byte_en),
    .pop_cnt_o      (axi_len_raw)
  );

  assign axi_len = axi_len_t'(axi_len_raw - (AXI_LEN_WIDTH+1)'(1'b1));

  assign cpu_mem_byte_en_first_set = cpu_mem_byte_en_i & ~(cpu_mem_byte_en_i<<1);
  for (genvar i=0; i<AXI_LEN; i++) begin : g_cpu_mem_reduced_one_hot
    assign cpu_mem_reduced_one_hot[i] = |cpu_mem_byte_en_first_set[i*AXI_STRB_WIDTH+:AXI_STRB_WIDTH];
  end

  always_comb begin
    axi_strb = '0;
    for (int i=0; i<AXI_LEN; i++) begin
      axi_strb |= (cpu_mem_byte_en_i >> AXI_STRB_WIDTH*i) & {MEM_BYTE_EN_WIDTH{cpu_mem_reduced_one_hot[i]}};
    end
  end

  // Also generate the shift amount for the read data to be shifted to the correct lanes based on the byte enables
  always_comb begin
    log2_strb = '0;
    for (int i=0; i<MEM_BYTE_EN_WIDTH; i++) begin
      if (cpu_mem_byte_en_first_set[i])
        log2_strb = LOG2_BYTE_EN_WIDTH'(i);
    end
  end

  always_comb begin
    axi_addr = '0;
    for (int i=0; i<AXI_LEN; i++) begin
      axi_addr |= {cpu_mem_addr_i[MEM_ADDR_WIDTH-1:4], i[1:0], 2'b00} & {MEM_ADDR_WIDTH{cpu_mem_reduced_one_hot[i]}};
    end
  end

  always_comb begin
    cpu_mem_wr_data = cpu_mem_wr_data_i[MEM_DATA_WIDTH-1:0];
    for (int i=0; i<AXI_LEN; i++) begin
      if (cpu_mem_reduced_one_hot[i])
        cpu_mem_wr_data >>= (i * AXI_DATA_WIDTH);
    end
  end

  // Valid Entries currently being used by any requests
  for (genvar i=0; i<AXI_TRACKER_DEPTH; i++) begin : g_entry_valid
    assign axi_tracker_entry_valid[i] = (axi_tracker_state_q[i].valid);
  end

  // -------------------------------------------------------------------------------------
  // AR Request generation logic
  // -------------------------------------------------------------------------------------
  // Pick the first valid AR entry which can be sent to AR channel
  for (genvar i=0; i<AXI_TRACKER_DEPTH; i++) begin : g_ar_entry_valid
    assign ar_entry_valid[i] = (axi_tracker_state_q[i].valid & axi_tracker_state_q[i].ar_naw & axi_tracker_state_q[i].pending_req) & ~|(track_older[i] & axi_tracker_entry_valid);
  end
  qs_find_first #(.NUM_PORTS(AXI_TRACKER_DEPTH)) ar_req_entry_ff (
    .clk            (clk),
    .reset_n        (reset_n),
    .req_i          (ar_entry_valid),
    .first_set_o    (ar_req_entry)
  );
  // Send a AR request right after coming out of reset to get the number of transfers
  assign nxt_arvalid = |ar_entry_valid | (arvalid_q & ~axi_arready_i);
  always_ff @(posedge clk or negedge reset_n)
    if (~reset_n)
      arvalid_q <= 1'h0;
    else
      arvalid_q <= nxt_arvalid;

  // One hot mux to find the active AR entry
  always_comb begin
    ar_req_entry_pld = '0;
    for (int i=0; i<AXI_TRACKER_DEPTH; i++) begin
      ar_req_entry_pld |= {$bits(axi_ax_t){ar_req_entry[i]}} & axi_tracker_q[i];
    end
  end

  assign ar_req_entry_pld_en = |ar_entry_valid & (~arvalid_q | axi_arready_i);
  always_ff @(posedge clk)
    if (ar_req_entry_pld_en)
      ar_req_entry_pld_q <= ar_req_entry_pld;

  // -------------------------------------------------------------------------------------
  // Read Response handling logic
  // -------------------------------------------------------------------------------------
  // Check if we got a valid response on this cycle
  assign axi_r_xfer = axi_rvalid_i & (|axi_rd_buffer_avail | (|axi_rd_xfer_cnt_q));

  // 4 entry read buffers
  // Consume an entry when the read response is available
  // Pick the first free read buffer
  qs_find_first #(.NUM_PORTS(AXI_RD_BUFFER_DEPTH)) ar_rd_buffer_free_entry_ff (
    .clk            (clk),
    .reset_n        (reset_n),
    .req_i          (~axi_rd_buffer_valid_q),
    .first_set_o    (axi_rd_buffer_pick_entry)
  );
  // Valid status for the read buffers
  assign axi_rd_buffer_valid_en = (axi_r_xfer & ~|axi_rd_xfer_cnt_q) | mem_cpu_rd_xfer;
  for (genvar i=0; i<AXI_RD_BUFFER_DEPTH; i++) begin : g_read_buffer_valid
    always_ff @(posedge clk or negedge reset_n)
      if (~reset_n)
        axi_rd_buffer_valid_q[i] <= '0;
      else if (axi_rd_buffer_valid_en)
        axi_rd_buffer_valid_q[i] <= nxt_axi_rd_buffer_valid[i];
  end

  assign mem_cpu_rd_xfer = mem_cpu_pld_en & axi_tracker_entry_state_pld.ar_naw;
  for (genvar i=0; i<AXI_RD_BUFFER_DEPTH; i++) begin : g_read_buffer_valid_nxt
    assign nxt_axi_rd_buffer_valid[i] = (axi_rd_buffer_pick_entry[i] & (axi_r_xfer & ~|axi_rd_xfer_cnt_q)) |
                                        (axi_rd_buffer_valid_q[i] & ~(mem_cpu_rd_xfer & (axi_tracker_entry_pld.buf_id == i[AXI_RD_BUFFER_WIDTH-1:0])));
  end

  assign axi_rd_buffer_avail = ~axi_rd_buffer_valid_q;

  // Convert the picked entry into ID
  always_comb begin
    axi_rd_buffer_pick_id = '0;
    for (int i=0; i<AXI_RD_BUFFER_DEPTH; i++) begin
      if (axi_rd_buffer_pick_entry[i])
        axi_rd_buffer_pick_id = i[AXI_BUFFER_ID_WIDTH-1:0];
    end
  end

  // Store the incoming buffer data
  // Rotate the incoming data to accomodate all of the incoming data in different beats
  always_comb begin
    for (int i=0; i<AXI_RD_BUFFER_DEPTH; i++) begin
      for (int j=0; j<AXI_LEN; j++) begin
        axi_rdata_rot[i][j*AXI_DATA_WIDTH +: AXI_DATA_WIDTH] = (axi_rd_xfer_cnt_q == j[AXI_LEN_WIDTH-1:0]) ?
                                                               (axi_rdata_i) : axi_rd_buffer_q[i].data[j*AXI_DATA_WIDTH +: AXI_DATA_WIDTH];
      end
      nxt_axi_rd_buffer[i].data = axi_rdata_rot[i];
    end
  end

  for (genvar i=0; i<AXI_RD_BUFFER_DEPTH; i++) begin : g_read_buffer
    assign axi_rd_buffer_en[i] = (axi_rd_buffer_valid_en & axi_rd_buffer_pick_entry[i]) | (axi_r_xfer & |axi_rd_xfer_cnt_q & (axi_rd_buffer_id_q == i[AXI_RD_BUFFER_WIDTH-1:0]));
    always_ff @(posedge clk) begin
      if (axi_rd_buffer_en[i]) begin
        axi_rd_buffer_q[i] <= nxt_axi_rd_buffer[i];
      end
    end
  end

  assign axi_rd_buffer_id_en = (axi_r_xfer & ~|axi_rd_xfer_cnt_q);
  always_ff @(posedge clk or negedge reset_n) begin
    if (~reset_n)
      axi_rd_buffer_id_q <= '0;
    else if (axi_rd_buffer_id_en)
      axi_rd_buffer_id_q <= axi_rd_buffer_pick_id;
  end

  // Read transfer counter to count until axi_rlast_i is asserted
  // Can have a maximum of 4 transfers for an AR request
  assign nxt_axi_rd_xfer_cnt = axi_rlast_i ? '0 : axi_rd_xfer_cnt_q + AXI_LEN_WIDTH'(1'b1);
  always_ff @(posedge clk or negedge reset_n)
    if (~reset_n)
      axi_rd_xfer_cnt_q <= '0;
    else if (axi_r_xfer)
      axi_rd_xfer_cnt_q <= nxt_axi_rd_xfer_cnt;

  // Update the state in the tracker depending on the axi_id resp
  for (genvar i=0; i<AXI_TRACKER_DEPTH; i++) begin : g_rd_buffer_entry_update
    assign r_resp_entry[i] = (axi_tracker_state_q[i].valid & axi_tracker_state_q[i].ar_naw &
                             ~axi_tracker_state_q[i].pending_req & (axi_tracker_q[i].axi_id == axi_rid_i));
  end

  // -------------------------------------------------------------------------------------
  // Write data buffer handling logic
  // -------------------------------------------------------------------------------------
  // 4 entry write buffers
  // Consume an entry when the write cpu_mem request is accepted
  // Pick the first free write buffer
  qs_find_first #(.NUM_PORTS(AXI_WR_BUFFER_DEPTH)) ar_wr_buffer_free_entry_ff (
    .clk            (clk),
    .reset_n        (reset_n),
    .req_i          (~axi_wr_buffer_valid_q),
    .first_set_o    (axi_wr_buffer_pick_entry)
  );
  // Valid status for the write buffers
  assign axi_wr_buffer_valid_en = (|axi_wr_buffer_pick_entry) | aw_req_entry_pld_en;

  for (genvar i=0; i<AXI_WR_BUFFER_DEPTH; i++) begin : g_write_buffer_valid
    always_ff @(posedge clk or negedge reset_n)
      if (~reset_n)
        axi_wr_buffer_valid_q[i] <= '0;
      else if (axi_wr_buffer_valid_en)
        axi_wr_buffer_valid_q[i] <= nxt_axi_wr_buffer_valid[i];

    assign nxt_axi_wr_buffer_valid[i] = (axi_wr_buffer_pick_entry[i] & cpu_mem_xfer & cpu_mem_wr_i) |
                                        (axi_wr_buffer_valid_q[i] & ~(aw_req_entry_pld_en & (aw_req_entry_pld.buf_id == i[AXI_WR_BUFFER_WIDTH-1:0])));
  end

  assign axi_wr_buffer_avail = ~axi_wr_buffer_valid_q;

  always_comb begin
    axi_wr_buffer_pick_id = '0;
    for (int i=0; i<AXI_WR_BUFFER_DEPTH; i++) begin
      if (axi_wr_buffer_pick_entry[i])
        axi_wr_buffer_pick_id = i[AXI_BUFFER_ID_WIDTH-1:0];
    end
  end

  // Write the incoming write data into the write buffer accounting for the byte enables
  for (genvar i=0; i<AXI_WR_BUFFER_DEPTH; i++) begin : g_write_buffer_data
    assign nxt_axi_wr_buffer[i].data = cpu_mem_wr_data;
    assign axi_wr_buffer_en[i] = (axi_wr_buffer_pick_entry[i] & cpu_mem_xfer);
    always_ff @(posedge clk) begin
      if (axi_wr_buffer_en[i])
        axi_wr_buffer_q[i] <= nxt_axi_wr_buffer[i];
    end
  end

  // Find the write data from the buffer for the current AW request being sent out
  always_comb begin
    axi_wr_data = '0;
    for (int i=0; i<AXI_WR_BUFFER_DEPTH; i++) begin
      axi_wr_data |= {$bits(axi_wr_buffer_t){aw_req_entry_pld.buf_id == i[AXI_BUFFER_ID_WIDTH-1:0]}} & axi_wr_buffer_q[i].data;
    end
  end

  // Store the write data from the buffer for the current AW request being sent out
  assign nxt_axi_wr_data = aw_req_entry_pld_en ? axi_wr_data             : axi_wr_data_q >> AXI_DATA_WIDTH;
  assign nxt_axi_wr_strb = aw_req_entry_pld_en ? aw_req_entry_pld.strb   : axi_wr_strb_q >> AXI_STRB_WIDTH;
  assign axi_wr_pld_en = aw_req_entry_pld_en | (axi_wready_i & w_xfer_inprog_q & (axi_wr_xfer_cnt_q != aw_req_entry_pld_q.len));
  always_ff @(posedge clk) begin
    if (axi_wr_pld_en) begin
      axi_wr_data_q <= nxt_axi_wr_data;
      axi_wr_strb_q <= nxt_axi_wr_strb;
    end
  end

  // -------------------------------------------------------------------------------------
  // AW Request generation logic
  // -------------------------------------------------------------------------------------
  // Pick the first valid AW entry which can be sent to AW channel
  for (genvar i=0; i<AXI_TRACKER_DEPTH; i++) begin : g_aw_entry_valid
    assign aw_entry_valid[i] = (axi_tracker_state_q[i].valid & ~axi_tracker_state_q[i].ar_naw & axi_tracker_state_q[i].pending_req) & ~|(track_older[i] & axi_tracker_entry_valid);
  end
  qs_find_first #(.NUM_PORTS(AXI_TRACKER_DEPTH)) aw_req_entry_ff (
    .clk            (clk),
    .reset_n        (reset_n),
    .req_i          (aw_entry_valid),
    .first_set_o    (aw_req_entry)
  );

  // Send a AW request if any of the AW entries are valid and the previous request has been accepted
  assign aw_w_xfer_done = (~awvalid_q | axi_awready_i) & (~wvalid_q | (axi_wready_i & (axi_wr_xfer_cnt_q == aw_req_entry_pld_q.len)));
  assign nxt_awvalid = (|aw_entry_valid & aw_w_xfer_done) | (awvalid_q & ~axi_awready_i);
  always_ff @(posedge clk or negedge reset_n)
    if (~reset_n)
      awvalid_q <= 1'h0;
    else
      awvalid_q <= nxt_awvalid;

  // One hot mux to find the active AW entry
  always_comb begin
    aw_req_entry_pld = '0;
    for (int i=0; i<AXI_TRACKER_DEPTH; i++) begin
      aw_req_entry_pld |= {$bits(axi_ax_t){aw_req_entry[i]}} & axi_tracker_q[i];
    end
  end

  assign aw_req_entry_pld_en = |aw_entry_valid & aw_w_xfer_done;
  always_ff @(posedge clk)
    if (aw_req_entry_pld_en)
      aw_req_entry_pld_q <= aw_req_entry_pld;

  // -------------------------------------------------------------------------------------
  // W Channel Request generation logic
  // -------------------------------------------------------------------------------------
  // Send a W request if any of the W entries are valid and the previous request has been accepted
  assign nxt_wvalid = ((|aw_entry_valid & aw_w_xfer_done) | (w_xfer_inprog_q & (axi_wr_xfer_cnt_q != aw_req_entry_pld_q.len))) |
                      (wvalid_q & ~axi_wready_i);
  always_ff @(posedge clk or negedge reset_n)
    if (~reset_n)
      wvalid_q <= 1'h0;
    else
      wvalid_q <= nxt_wvalid;

  // The burst ends on its last accepted beat, even if the AW is still waiting for AWREADY.
  // Otherwise inprog stays set and wvalid re-asserts for a phantom second burst.
  assign w_xfer_last = wvalid_q & axi_wready_i & (axi_wr_xfer_cnt_q == aw_req_entry_pld_q.len);
  assign w_xfer_inprog = (|aw_entry_valid & aw_w_xfer_done) | (w_xfer_inprog_q & ~w_xfer_last);
  always_ff @(posedge clk or negedge reset_n)
    if (~reset_n)
      w_xfer_inprog_q <= 1'h0;
    else
      w_xfer_inprog_q <= w_xfer_inprog;

  // Maintain a counter to track the number of beats sent out for a given AW request
  assign axi_wr_xfer_en = (axi_wready_i & w_xfer_inprog_q);
  always_ff @(posedge clk or negedge reset_n)
    if (~reset_n)
      axi_wr_xfer_cnt_q <= '0;
    else if (axi_wr_xfer_en)
      axi_wr_xfer_cnt_q <= nxt_axi_wr_xfer_cnt;

  assign nxt_axi_wr_xfer_cnt = (axi_wr_xfer_cnt_q == aw_req_entry_pld_q.len) ? '0 : axi_wr_xfer_cnt_q + AXI_LEN_WIDTH'(1'b1);

  // -------------------------------------------------------------------------------------
  // Write Response handling logic
  // -------------------------------------------------------------------------------------
  // Update the state in the tracker depending on the axi_id resp
  for (genvar i=0; i<AXI_TRACKER_DEPTH; i++) begin : g_b_resp_entry_update
    assign b_resp_entry[i] = (axi_tracker_state_q[i].valid & ~axi_tracker_state_q[i].ar_naw &
                             ~axi_tracker_state_q[i].pending_req & (axi_tracker_q[i].axi_id == axi_bid_i));
  end

  // -------------------------------------------------------------------------------------
  // AXI id Ordering logic
  // -------------------------------------------------------------------------------------
  // Maintain a dependency matrix to track the ordering of the AXI ids. This is needed to
  // ensure AXI ordering requirements are met for the same AXI id.
  for (genvar i=0; i<AXI_TRACKER_DEPTH; i=i+1) begin : g_axi_id_ordering
    assign axi_tracker_ordered[i] = axi_tracker_state_q[i].valid & (axi_tracker_q[i].axi_id == cpu_mem_addr_i[4+:AXI_ID_WIDTH]);
  end

  assign track_older_en = cpu_mem_xfer;

  for (genvar i=0; i<AXI_TRACKER_DEPTH; i=i+1) begin : g_per_row
    for (genvar j=0; j<AXI_TRACKER_DEPTH; j=j+1) begin : g_per_col
      if (i==j)
        assign track_older[i][j] = 1'b0;
      else begin
        // Set the row if ordered, clear the column
        assign nxt_old_entry[i][j] = ((axi_tracker_free_entry[i] & axi_tracker_ordered[j]) | track_older[i][j]) & ~axi_tracker_free_entry[j];

        always_ff @(posedge clk)
          if (track_older_en)
            old_entry_q[i][j] <= nxt_old_entry[i][j];

        assign track_older[i][j] = old_entry_q[i][j];
      end
    end
  end

  // -------------------------------------------------------------------------------------
  // MEM to CPU Interface
  // -------------------------------------------------------------------------------------
  // Send the response when any of the tracker entry has done flag asserted
  for (genvar i=0; i<AXI_TRACKER_DEPTH; i++) begin : g_mem_cpu_resp
    assign axi_tracker_done_entry[i] = axi_tracker_state_q[i].valid & axi_tracker_state_q[i].done;
  end

  // Find the first done entry to generate the response
  qs_find_first #(.NUM_PORTS(AXI_TRACKER_DEPTH)) axi_tracker_done_entry_ff (
    .clk            (clk),
    .reset_n        (reset_n),
    .req_i          (axi_tracker_done_entry),
    .first_set_o    (axi_tracker_pick_entry)
  );

  always_comb begin
    axi_tracker_entry_pld = '0;
    axi_tracker_entry_state_pld = '0;
    for (int i=0; i<AXI_TRACKER_DEPTH; i++) begin
      axi_tracker_entry_pld |= {$bits(axi_ax_t){axi_tracker_pick_entry[i]}} & axi_tracker_q[i];
      axi_tracker_entry_state_pld |= {$bits(axi_ax_state_t){axi_tracker_pick_entry[i]}} & axi_tracker_state_q[i];
    end
  end

  assign nxt_mem_cpu_valid = (|axi_tracker_done_entry & mem_cpu_free) |
                             (mem_cpu_valid_q & ~mem_cpu_ready_i);
  always_ff @(posedge clk or negedge reset_n)
    if (~reset_n)
      mem_cpu_valid_q <= 1'b0;
    else
      mem_cpu_valid_q <= nxt_mem_cpu_valid;

  assign mem_cpu_free = (~mem_cpu_valid_q | mem_cpu_ready_i);

  // Shift the data to account for the mem byte enables such byte fall in the correct lanes
  always_comb begin
    nxt_mem_cpu_rd_data = axi_rd_buffer_q[axi_tracker_entry_pld.buf_id].data << ({axi_tracker_entry_pld.log2_strb[3:2], 2'b00} * 8);
    axi_tracker_entry_strb_shifted = axi_tracker_entry_pld.strb << ({axi_tracker_entry_pld.log2_strb[3:2], 2'b00});
    for (int i=0; i<MEM_BYTE_EN_WIDTH; i++) begin
      nxt_mem_cpu_rd_data[i*8 +: 8] &= {8{axi_tracker_entry_strb_shifted[i]}};
    end
  end

  assign mem_cpu_pld_en = |axi_tracker_done_entry & mem_cpu_free;
  always_ff @(posedge clk or negedge reset_n)
    if (~reset_n) begin
      mem_cpu_id_q <= '0;
      mem_cpu_rd_data_q <= '0;
    end else if (mem_cpu_pld_en) begin
      mem_cpu_id_q <= axi_tracker_entry_pld.mem_id;
      mem_cpu_rd_data_q <= nxt_mem_cpu_rd_data;
    end

  // -------------------------------------------------------------------------------------
  // CPU to MEM Interface
  // -------------------------------------------------------------------------------------
  // Accept cpu_mem request if there is space available
  assign cpu_mem_ready_o = cpu_mem_ready;

  // -------------------------------------------------------------------------------------
  // Output assignments
  // -------------------------------------------------------------------------------------
  assign mem_cpu_valid_o = mem_cpu_valid_q;
  assign mem_cpu_id_o = mem_cpu_id_q;
  assign mem_cpu_rd_data_o = mem_cpu_rd_data_q;

  assign axi_arvalid_o = arvalid_q;
  assign axi_arid_o = ar_req_entry_pld_q.axi_id;
  assign axi_araddr_o = ar_req_entry_pld_q.addr;
  assign axi_arlen_o = ar_req_entry_pld_q.len;
  assign axi_arsize_o = AXI_SIZE_WIDTH'(2'b10);

  // RREADY asserted whenever read buffers have space available or an allocated buffer
  // is yet to be filled up
  assign axi_rready_o = |axi_rd_buffer_avail | (|axi_rd_xfer_cnt_q);

  assign axi_awvalid_o = awvalid_q;
  assign axi_awid_o = aw_req_entry_pld_q.axi_id;
  assign axi_awaddr_o = aw_req_entry_pld_q.addr;
  assign axi_awlen_o = aw_req_entry_pld_q.len;
  assign axi_awsize_o = AXI_SIZE_WIDTH'(2'b10);

  assign axi_wvalid_o = wvalid_q;
  assign axi_wdata_o = axi_wr_data_q[AXI_DATA_WIDTH-1:0];
  assign axi_wstrb_o = axi_wr_strb_q[AXI_STRB_WIDTH-1:0];
  assign axi_wlast_o = axi_wr_xfer_cnt_q == aw_req_entry_pld_q.len;

  // Always ready to accept the write responses
  assign axi_bready_o = 1'b1;

endmodule
