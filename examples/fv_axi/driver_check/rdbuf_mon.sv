// Counts the clock cycles the DUT's 4-entry read buffer is full (every axi_rd_buffer_valid_q bit set) and
// reports it at the end of the simulation. Bound into axi_master: the testbench is unchanged.
module rdbuf_mon;
  int unsigned full_cycles = 0, max_used = 0;
  always @(posedge axi_master.clk) begin
    if (&axi_master.axi_rd_buffer_valid_q) full_cycles++;
    if ($countones(axi_master.axi_rd_buffer_valid_q) > max_used) max_used = $countones(axi_master.axi_rd_buffer_valid_q);
  end
  final $display("RDBUF: full for %0d cycles, at most %0d of 4 entries used", full_cycles, max_used);
endmodule

bind axi_master rdbuf_mon u_rdbuf_mon ();
