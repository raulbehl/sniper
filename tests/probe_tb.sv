// Self-contained probe for tb_extract: one of each construct the extractor should report.
package probe_pkg;
  localparam int W = 8;
  typedef enum logic [1:0] {RD, WR, ATOMIC} op_e;
  typedef logic [W-1:0] addr_t;
endpackage

interface probe_if (input logic clk);
  logic       valid, ready;
  logic [7:0] addr;
  logic [1:0] op;
  modport drv (output valid, addr, op, input ready, clk);
endinterface

module probe_dut (input logic clk, input logic valid, input logic [7:0] addr, output logic ready);
  always_ff @(posedge clk) ready <= valid && addr[0];
endmodule

package probe_tb_pkg;
  import probe_pkg::*;

  class base_item;
    rand addr_t addr;
    rand op_e   op;
    rand int unsigned len;
    rand bit [3:0] be[4];
    int unsigned  delay = 3;
    constraint c_len  { len inside {[1:16]}; soft len < 8; }
    constraint c_dist { op dist {RD := 6, WR := 3, ATOMIC := 1}; }
    constraint c_impl { (op == ATOMIC) -> (addr[1:0] == 0); if (len > 4) addr < 128; else addr >= 128; }
    constraint c_each { foreach (be[i]) be[i] != 0; }
    covergroup cg_item;
      cp_op   : coverpoint op { ignore_bins no_atomic = {ATOMIC}; }
      cp_len  : coverpoint len { bins sml = {[1:4]}; bins big[2] = {[5:16]}; illegal_bins zero = {0}; }
      cp_addr : coverpoint addr iff (op != ATOMIC) { bins lo = {[0:127]}; bins hi = {[128:255]};
                                                    bins t = (0 => 1 => 2); }
      x_op_len : cross cp_op, cp_len { ignore_bins i = binsof(cp_op) intersect {WR}; }
      option.per_instance = 1;
    endgroup
    function new(); cg_item = new(); endfunction
    function void post_randomize(); delay = $urandom_range(5, 0); endfunction
  endclass

  class knobs #(int N = 4);
    int unsigned pct = 50;
    int unsigned n   = N;
  endclass

  class driver;
    virtual probe_if vif;
    knobs #(2) k;
    function new(virtual probe_if v); vif = v; k = new(); endfunction
    task run(int count);
      base_item it = new();
      repeat (count) begin
        void'(it.randomize() with { len <= k.n; op != ATOMIC; });
        randcase
          3: repeat (it.delay) @(posedge vif.clk);
          1: ;
        endcase
        vif.valid <= 1'b1;
        vif.addr  <= it.addr;
        vif.op    <= it.op;
        do @(posedge vif.clk); while (!vif.ready);
        vif.valid <= 1'b0;
        it.cg_item.sample();
      end
    endtask
  endclass

  class test;
    driver d;
    function void configure(); d.k.pct = 80; endfunction
  endclass
endpackage

module probe_top;
  import probe_tb_pkg::*;
  logic clk = 0;
  always #5 clk = ~clk;
  probe_if pif (.clk(clk));
  probe_dut dut (.clk(clk), .valid(pif.valid), .addr(pif.addr), .ready(pif.ready));
  c_hs: cover property (@(posedge clk) pif.valid && pif.ready);
  a_stable: assume property (@(posedge clk) pif.valid && !pif.ready |=> pif.valid);
  initial begin
    automatic driver d = new(pif);
    int x;
    void'(std::randomize(x) with { x inside {[0:9]}; });
    d.run(10 + x);
  end
endmodule
