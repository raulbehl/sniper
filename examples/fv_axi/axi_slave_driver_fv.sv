// axi_slave_driver_fv: the UVM testbench's AXI slave (axi_slave_driver, fed by axi_rd_resp_seq /
// axi_wr_resp_seq through axi_slave_monitor) as a synthesizable formal environment for the DUT.
//
// Translation rules (one per UVM construct; every block below names the source it comes from):
//   fork ... join of forever threads  -> one clocked state machine per thread, all on posedge clk
//   @(posedge vif.clk)                -> a state boundary (the statements after it run at that edge)
//   vif.sig <= v                      -> a register assignment at that edge (NBA: the DUT sees it from
//                                        the next edge, exactly as in simulation)
//   repeat (n) @(posedge vif.clk)     -> a counter state, n edges
//   do @(posedge clk) while (!c)      -> a wait state, left at the first edge where c is sampled true
//   get_next_item / item_done         -> take the next request the responder sequence would hand over;
//                                        the sequence's queue becomes a register queue
//   $urandom_range(hi, lo)            -> a free value (FV_ANYSEQ) assumed in [lo:hi]
//   $urandom_range(99) < cfg.pct      -> a free bit; constant 1 for pct >= 100, constant 0 for pct == 0
//   cfg.<knob>                        -> a parameter (defaults: axi_slave_cfg's)
//   cfg.mem.read_word(a)              -> free read data (any memory content: over-approximation)
//   two processes at one edge in an order the LRM leaves open (monitor vs. responder sequence)
//                                     -> a free choice between both outcomes
//
// Sources: uvm_tb/axi_agent/axi_slave_driver.svh (run_phase, ready_loop, r_loop, b_loop),
// axi_rd_resp_seq.svh, axi_wr_resp_seq.svh, axi_slave_monitor.svh (when requests become visible),
// axi_slave_cfg.svh (knobs). The UVM queues are unbounded; QDEPTH entries are modelled and an
// assertion checks they never overflow within the explored depth.
`include "fv_tool.svh"

module axi_slave_driver_fv #(
  // axi_slave_cfg (the tests override some: axi_backpressure_test, axi_ooo_test, axi_stress_test)
  parameter int unsigned AR_READY_PCT = 100,
  parameter int unsigned AW_READY_PCT = 100,
  parameter int unsigned W_READY_PCT  = 100,
  parameter int unsigned RD_LAT_MIN   = 1,
  parameter int unsigned RD_LAT_MAX   = 4,
  parameter int unsigned R_GAP_PCT    = 0,
  parameter int unsigned R_GAP_MAX    = 3,
  parameter int unsigned WR_LAT_MIN   = 1,
  parameter int unsigned WR_LAT_MAX   = 4,
  parameter int unsigned OOO_PCT      = 0,
  parameter int unsigned QDEPTH       = 8,
  parameter int ID_W = 3, LEN_W = 2, DATA_W = 32
) (
  input  logic              clk,
  input  logic              reset_n,
  // driven by the DUT (read here as the monitor and the driver's do-while loops read them)
  input  logic              arvalid,
  input  logic [ID_W-1:0]   arid,
  input  logic [LEN_W-1:0]  arlen,
  input  logic              awvalid,
  input  logic [ID_W-1:0]   awid,
  input  logic              wvalid,
  input  logic              wlast,
  input  logic              rready,
  input  logic              bready,
  // driven by the testbench (axi_bus_if initial values: 0)
  output logic              arready = 1'b0,
  output logic              awready = 1'b0,
  output logic              wready  = 1'b0,
  output logic              rvalid  = 1'b0,
  output logic [ID_W-1:0]   rid     = '0,
  output logic [DATA_W-1:0] rdata   = '0,
  output logic              rlast   = 1'b0,
  output logic              bvalid  = 1'b0,
  output logic [ID_W-1:0]   bid     = '0,
  // test hook: a request queue overflowed (raise QDEPTH)
  output logic              q_overflow
);
  `include "prim_assert.sv"
  localparam int QW = $clog2(QDEPTH + 1);
  localparam int PW = (QDEPTH > 1) ? $clog2(QDEPTH) : 1;

  // ------------------------------------------------------------------ free choices ($urandom_range)
  `FV_ANYSEQ logic ch_arready, ch_awready, ch_wready;   // ready_loop: $urandom_range(99) < cfg.*_ready_pct
  `FV_ANYSEQ logic [7:0] ch_rlat;                        // axi_rd_resp_seq: start_delay
  `FV_ANYSEQ logic [7:0] ch_gap;                         // axi_rd_resp_seq: gap[k]
  `FV_ANYSEQ logic [7:0] ch_wlat;                        // axi_wr_resp_seq: delay
  `FV_ANYSEQ logic [DATA_W-1:0] ch_rdata;                // cfg.mem.read_word(a)
  `FV_ANYSEQ logic [PW-1:0] ch_rpick, ch_bpick;          // ooo: idx = eligible[$urandom_range(..)]
  `FV_ANYSEQ logic ch_rlate, ch_blate;                   // monitor ran after the sequence at this edge
  `ASSUME(rlat_range, ch_rlat >= 8'(RD_LAT_MIN) && ch_rlat <= 8'(RD_LAT_MAX), clk, 1'b0)
  `ASSUME(wlat_range, ch_wlat >= 8'(WR_LAT_MIN) && ch_wlat <= 8'(WR_LAT_MAX), clk, 1'b0)
  // gap[k] = ($urandom_range(99) < r_gap_pct) ? $urandom_range(r_gap_max, 1) : 0
  `ASSUME(gap_range, (R_GAP_PCT == 0) ? (ch_gap == 8'd0) :
                     (R_GAP_PCT >= 100) ? (ch_gap >= 8'd1 && ch_gap <= 8'(R_GAP_MAX)) :
                     (ch_gap <= 8'(R_GAP_MAX)), clk, 1'b0)

  function automatic logic pct_bit(int unsigned pct, logic ch);
    return (pct >= 100) ? 1'b1 : (pct == 0) ? 1'b0 : ch;
  endfunction

  // ------------------------------------------------------------------ run_phase
  // while (!vif.reset_n) @(posedge vif.clk); fork ready_loop(); r_loop(); b_loop(); join
  // started: the edge that ends the wait has passed (ready_loop's first assignment is one edge later)
  logic started = 1'b0;
  always_ff @(posedge clk) if (reset_n) started <= 1'b1;

  // ------------------------------------------------------------------ ready_loop
  // forever begin @(posedge vif.clk); vif.axi_arready <= ...; vif.axi_awready <= ...; vif.axi_wready <= ...; end
  always_ff @(posedge clk) if (started) begin
    arready <= pct_bit(AR_READY_PCT, ch_arready);
    awready <= pct_bit(AW_READY_PCT, ch_awready);
    wready  <= pct_bit(W_READY_PCT,  ch_wready);
  end

  // ------------------------------------------------------------------ monitor -> sequencer fifos
  // axi_slave_monitor samples at every edge with reset_n high: an AR handshake becomes an axi_rd_req;
  // an AW handshake and a W burst (wlast handshake) are paired in order (try_pair) into an axi_wr_req.
  wire ar_hs   = reset_n && arvalid && arready;
  wire aw_hs   = reset_n && awvalid && awready;
  wire wl_hs   = reset_n && wvalid && wready && wlast;

  // write pairing (try_pair): AWs waiting for their W burst, completed W bursts waiting for their AW; at
  // most one pair forms per edge (before it, one of the two queues is empty)
  logic [ID_W-1:0] awq [QDEPTH];
  logic [QW-1:0]   awq_n = '0;
  logic [QW-1:0]   wb_n  = '0;
  wire             wr_in    = (awq_n != 0 || aw_hs) && (wb_n != 0 || wl_hs);     // an axi_wr_req appears
  wire [ID_W-1:0]  wr_in_id = (awq_n != 0) ? awq[0] : awid;                       // the oldest AW
  always_ff @(posedge clk) begin
    automatic logic [ID_W-1:0] q [QDEPTH] = awq;
    automatic logic [QW-1:0] n = awq_n, w = wb_n;
    if (aw_hs && n < QDEPTH) begin q[n] = awid; n = n + 1'b1; end
    if (wl_hs) w = w + 1'b1;
    if (n != 0 && w != 0) begin
      for (int i = 0; i < QDEPTH - 1; i++) q[i] = q[i + 1];
      n = n - 1'b1; w = w - 1'b1;
    end
    awq <= q; awq_n <= n; wb_n <= w;
  end

  // ------------------------------------------------------------------ responder queues (pend[$])
  // axi_rd_resp_seq / axi_wr_resp_seq keep the requests not yet handed to the driver in pend[$].
  // A request is picked when the driver asks for its next item (get_next_item): the oldest one, or with
  // cfg.ooo_pct > 0 any request with no older one of the same id. A request that arrives at the edge where
  // the driver asks is visible to the pick when pend is empty (the sequence blocks in ar_fifo.get() and
  // wakes at that edge); with other requests pending it depends on whether the monitor ran first (ch_*late).
  typedef struct packed { logic [ID_W-1:0] id; logic [LEN_W-1:0] len; } req_t;

  // read side
  req_t            rq [QDEPTH];
  logic [QW-1:0]   rq_n = '0;
  // write side (len unused)
  req_t            bq [QDEPTH];
  logic [QW-1:0]   bq_n = '0;

  // eligible: pending, and no older pending request of the same id (same-id responses stay in order)
  logic [QDEPTH-1:0] r_elig, b_elig;
  logic r_in_elig, b_in_elig;           // the request arriving at this edge, against the pending ones
  always_comb begin
    r_in_elig = 1'b1; b_in_elig = 1'b1;
    for (int i = 0; i < QDEPTH; i++) begin
      r_elig[i] = (i < rq_n); b_elig[i] = (i < bq_n);
      for (int j = 0; j < i; j++) begin
        if (rq[j].id == rq[i].id) r_elig[i] = 1'b0;
        if (bq[j].id == bq[i].id) b_elig[i] = 1'b0;
      end
      if (i < rq_n && rq[i].id == arid) r_in_elig = 1'b0;
      if (i < bq_n && bq[i].id == wr_in_id) b_in_elig = 1'b0;
    end
  end

  // ------------------------------------------------------------------ r_loop
  // forever: get_next_item(it); repeat (it.start_delay) @(posedge); for k in beats: drive rvalid/rid/
  // rdata/rlast; do @(posedge) while (!rready); rvalid <= 0; rlast <= 0; repeat (it.gap[k]) @(posedge)
  typedef enum logic [1:0] {R_IDLE, R_DLY, R_BEAT, R_GAP} rst_t;
  rst_t            r_st = R_IDLE;
  logic [7:0]      r_cnt = '0;
  logic [ID_W-1:0] r_id = '0;
  logic [LEN_W-1:0] r_len = '0, r_k = '0;

  // the pick at this edge (used when the driver asks for an item)
  logic            r_take;          // the driver asks for its next item at this edge
  logic            r_got;           // ... and gets one
  logic            r_from_in;       // the request handshaked at this edge is the one picked
  logic [PW-1:0]   r_idx;
  req_t            r_req;
  always_comb begin
    r_take = (r_st == R_IDLE) || (r_st == R_BEAT && rready && r_k == r_len);       // item_done -> next
    r_got = 1'b0; r_from_in = 1'b0; r_idx = '0; r_req = '0;
    if (r_take && reset_n) begin
      if (rq_n == 0) begin
        if (ar_hs) begin r_got = 1'b1; r_from_in = 1'b1; r_req = '{arid, arlen}; end
      end else if (OOO_PCT == 0) begin
        r_got = 1'b1; r_req = rq[0];
      end else if (!ch_rlate && ar_hs && QW'(ch_rpick) == rq_n && r_in_elig) begin
        r_got = 1'b1; r_from_in = 1'b1; r_req = '{arid, arlen};
      end else begin
        r_got = 1'b1; r_idx = r_elig[ch_rpick] ? ch_rpick : '0; r_req = rq[r_idx];
      end
    end
  end

  always_ff @(posedge clk) if (reset_n) begin
    automatic logic drive = 1'b0;                  // drive beat r_k (or the first beat) at this edge
    automatic logic [LEN_W-1:0] k = r_k;
    case (r_st)
      R_DLY: if (r_cnt == 8'd1) drive = 1'b1; else r_cnt <= r_cnt - 8'd1;
      R_GAP: if (r_cnt == 8'd1) drive = 1'b1; else r_cnt <= r_cnt - 8'd1;
      R_BEAT: if (rready) begin
        rvalid <= 1'b0; rlast <= 1'b0;
        if (r_k != r_len) begin
          k = r_k + 1'b1; r_k <= k;
          if (ch_gap == 8'd0) drive = 1'b1; else begin r_st <= R_GAP; r_cnt <= ch_gap; end
        end else r_st <= R_IDLE;
      end
      default: ;
    endcase
    if (r_got) begin                               // a new item: its first beat after start_delay
      r_id <= r_req.id; r_len <= r_req.len; k = '0; r_k <= '0;
      if (ch_rlat == 8'd0) drive = 1'b1; else begin r_st <= R_DLY; r_cnt <= ch_rlat; drive = 1'b0; end
    end
    if (drive) begin
      rvalid <= 1'b1;
      rid    <= r_got ? r_req.id : r_id;
      rdata  <= ch_rdata;
      rlast  <= (k == (r_got ? r_req.len : r_len));
      r_st   <= R_BEAT;
    end
  end

  // read queue: requests arriving (not picked at once) appended, the picked one removed
  always_ff @(posedge clk) if (reset_n) begin
    automatic logic [QW-1:0] n = rq_n;
    automatic req_t q [QDEPTH] = rq;
    if (r_got && !r_from_in) begin
      for (int i = 0; i < QDEPTH - 1; i++) if (i >= r_idx) q[i] = q[i + 1];
      n = n - 1'b1;
    end
    if (ar_hs && !r_from_in && n < QDEPTH) begin q[n] = '{arid, arlen}; n = n + 1'b1; end
    rq <= q; rq_n <= n;
  end

  // ------------------------------------------------------------------ b_loop
  // forever: get_next_item(it); repeat (it.delay) @(posedge); bvalid <= 1; bid <= it.id;
  // do @(posedge) while (!bready); bvalid <= 0
  typedef enum logic [1:0] {B_IDLE, B_DLY, B_VALID} bst_t;
  bst_t            b_st = B_IDLE;
  logic [7:0]      b_cnt = '0;
  logic [ID_W-1:0] b_id = '0;
  logic            b_take, b_got, b_from_in;
  logic [PW-1:0]   b_idx;
  req_t            b_req;
  always_comb begin
    b_take = (b_st == B_IDLE) || (b_st == B_VALID && bready);
    b_got = 1'b0; b_from_in = 1'b0; b_idx = '0; b_req = '0;
    if (b_take && reset_n) begin
      if (bq_n == 0) begin
        if (wr_in) begin b_got = 1'b1; b_from_in = 1'b1; b_req = '{wr_in_id, '0}; end
      end else if (OOO_PCT == 0) begin
        b_got = 1'b1; b_req = bq[0];
      end else if (!ch_blate && wr_in && QW'(ch_bpick) == bq_n && b_in_elig) begin
        b_got = 1'b1; b_from_in = 1'b1; b_req = '{wr_in_id, '0};
      end else begin
        b_got = 1'b1; b_idx = b_elig[ch_bpick] ? ch_bpick : '0; b_req = bq[b_idx];
      end
    end
  end
  always_ff @(posedge clk) if (reset_n) begin
    automatic logic drive = 1'b0;
    case (b_st)
      B_DLY: if (b_cnt == 8'd1) drive = 1'b1; else b_cnt <= b_cnt - 8'd1;
      B_VALID: if (bready) begin bvalid <= 1'b0; b_st <= B_IDLE; end
      default: ;
    endcase
    if (b_got) begin
      b_id <= b_req.id;
      if (ch_wlat == 8'd0) drive = 1'b1; else begin b_st <= B_DLY; b_cnt <= ch_wlat; drive = 1'b0; end
    end
    if (drive) begin
      bvalid <= 1'b1;
      bid    <= b_got ? b_req.id : b_id;
      b_st   <= B_VALID;
    end
  end
  always_ff @(posedge clk) if (reset_n) begin
    automatic logic [QW-1:0] n = bq_n;
    automatic req_t q [QDEPTH] = bq;
    if (b_got && !b_from_in) begin
      for (int i = 0; i < QDEPTH - 1; i++) if (i >= b_idx) q[i] = q[i + 1];
      n = n - 1'b1;
    end
    if (wr_in && !b_from_in && n < QDEPTH) begin q[n] = '{wr_in_id, '0}; n = n + 1'b1; end
    bq <= q; bq_n <= n;
  end

  // ------------------------------------------------------------------ model limits
  assign q_overflow = (ar_hs && !r_from_in && rq_n == QW'(QDEPTH) && !(r_got && !r_from_in)) ||
                      (wr_in && !b_from_in && bq_n == QW'(QDEPTH) && !(b_got && !b_from_in)) ||
                      (aw_hs && awq_n == QW'(QDEPTH) && !wr_in);
endmodule
