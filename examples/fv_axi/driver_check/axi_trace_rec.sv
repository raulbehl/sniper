// Records the AXI bus of the UVM testbench at every clock edge, as the DUT and the monitor sample it
// (before that edge's nonblocking updates): one line per edge, hex fields in the order of FIELDS in
// check_driver.py. Bound into tb_top; file from +AXI_TRACE=<path>.
module axi_trace_rec;
  int fd = 0;
  string fn;
  initial if ($value$plusargs("AXI_TRACE=%s", fn)) fd = $fopen(fn, "w");
  always @(posedge tb_top.clk) if (fd != 0)
    $fdisplay(fd, "%h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h %h",
              tb_top.reset_n,
              tb_top.axi_if.axi_arvalid, tb_top.axi_if.axi_arid, tb_top.axi_if.axi_arlen,
              tb_top.axi_if.axi_awvalid, tb_top.axi_if.axi_awid, tb_top.axi_if.axi_wvalid, tb_top.axi_if.axi_wlast,
              tb_top.axi_if.axi_rready, tb_top.axi_if.axi_bready,
              tb_top.axi_if.axi_arready, tb_top.axi_if.axi_awready, tb_top.axi_if.axi_wready,
              tb_top.axi_if.axi_rvalid, tb_top.axi_if.axi_rid, tb_top.axi_if.axi_rdata, tb_top.axi_if.axi_rlast,
              tb_top.axi_if.axi_bvalid, tb_top.axi_if.axi_bid);
  final if (fd != 0) $fclose(fd);
endmodule

bind tb_top axi_trace_rec u_axi_trace_rec ();
