// Records the CPU-MEM bus of the UVM testbench at every clock edge, as the DUT and the monitor sample it
// (before that edge's nonblocking updates): one line per edge, hex fields in the order of MEM_FIELDS in
// replay_check.py. Bound into tb_top; file from +MEM_TRACE=<path>.
module mem_trace_rec;
  int fd = 0;
  string fn;
  initial if ($value$plusargs("MEM_TRACE=%s", fn)) fd = $fopen(fn, "w");
  always @(posedge tb_top.clk) if (fd != 0)
    $fdisplay(fd, "%h %h %h %h %h %h %h %h %h %h",
              tb_top.reset_n,
              tb_top.mem_if.cpu_mem_valid, tb_top.mem_if.cpu_mem_id, tb_top.mem_if.cpu_mem_addr,
              tb_top.mem_if.cpu_mem_byte_en, tb_top.mem_if.cpu_mem_wr, tb_top.mem_if.cpu_mem_ready,
              tb_top.mem_if.mem_cpu_valid, tb_top.mem_if.mem_cpu_id, tb_top.mem_if.mem_cpu_ready);
  final if (fd != 0) $fclose(fd);
endmodule

bind tb_top mem_trace_rec u_mem_trace_rec ();
