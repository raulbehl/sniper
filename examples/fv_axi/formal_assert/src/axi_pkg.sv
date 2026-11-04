// --------------------------------------------------------
// Copyright (C) quicksilicon.io - All Rights Reserved
//
// Unauthorized copying of this file, via any medium is
// strictly prohibited
// Proprietary and confidential
// --------------------------------------------------------

// --------------------------------------------------------
// AXI Master - Package
// --------------------------------------------------------
package axi_pkg;

// --------------------------------------------------------
  // SRAM Interface Parameters
  // --------------------------------------------------------
  parameter int MEM_ADDR_WIDTH = 32;
  parameter int MEM_DATA_WIDTH = 128;
  parameter int MEM_ID_WIDTH = 4;
  parameter int MEM_BYTE_EN_WIDTH = MEM_DATA_WIDTH/8;
  parameter int LOG2_BYTE_EN_WIDTH = $clog2(MEM_BYTE_EN_WIDTH);

  // --------------------------------------------------------
  // AXI Master Microarchitecture Parameters
  // --------------------------------------------------------
  parameter int AXI_TRACKER_DEPTH = 16;
  parameter int AXI_TRACKER_SIZE = $clog2(AXI_TRACKER_DEPTH);
  parameter int AXI_ID_WIDTH = 3;
  parameter int AXI_ADDR_WIDTH = 32;
  parameter int AXI_DATA_WIDTH = 32;
  parameter int AXI_STRB_WIDTH = AXI_DATA_WIDTH / 8;
  parameter int AXI_LEN = MEM_DATA_WIDTH / AXI_DATA_WIDTH;
  parameter int AXI_LEN_WIDTH = $clog2(AXI_LEN);
  parameter int AXI_SIZE_WIDTH = 3;
  parameter int AXI_RD_BUFFER_DEPTH = 4;
  parameter int AXI_RD_BUFFER_WIDTH = $clog2(AXI_RD_BUFFER_DEPTH);
  parameter int AXI_WR_BUFFER_DEPTH = AXI_RD_BUFFER_DEPTH;
  parameter int AXI_WR_BUFFER_WIDTH = $clog2(AXI_WR_BUFFER_DEPTH);
  parameter int AXI_BUFFER_ID_WIDTH = AXI_WR_BUFFER_WIDTH > AXI_RD_BUFFER_WIDTH ? AXI_WR_BUFFER_WIDTH : AXI_RD_BUFFER_WIDTH;

  // --------------------------------------------------------
  // Enums and Structs
  // --------------------------------------------------------
  typedef enum logic [AXI_LEN_WIDTH-1:0] {
    LEN_1,
    LEN_2,
    LEN_3,
    LEN_4
  } axi_len_t;

  // Request tracker struct
  typedef struct packed {
    logic [MEM_ID_WIDTH-1:0]        mem_id;
    logic [AXI_ID_WIDTH-1:0]        axi_id;
    logic [AXI_BUFFER_ID_WIDTH-1:0] buf_id;
    logic [AXI_ADDR_WIDTH-1:0]      addr;
    axi_len_t                       len;
    logic [MEM_BYTE_EN_WIDTH-1:0]   strb;
    logic [LOG2_BYTE_EN_WIDTH-1:0]  log2_strb;
  } axi_ax_t;

  // Request tracker state struct
  typedef struct packed {
    logic                            valid;
    logic                            ar_naw;
    logic                            pending_req;
    logic                            done;
  } axi_ax_state_t;

  // Read buffer struct
  typedef struct packed {
    logic [MEM_DATA_WIDTH-1:0]  data;
  } axi_rd_buffer_t;

  // Write buffer struct
  typedef struct packed {
    logic [MEM_DATA_WIDTH-1:0]  data;
  } axi_wr_buffer_t;

endpackage
