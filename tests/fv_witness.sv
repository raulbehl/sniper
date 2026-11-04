// fvgen --witness end to end (test_fvgen.py T4): bins reached formally from pkt's randomize()
// model must be hit in simulation by fv_replay.sv's pkt__fv_replay under any seed.
interface wt_if;
  logic [2:0] src;
endinterface

package wt_pkg;
  typedef enum bit [1:0] {IDLE, RD, WR, CFG} kind_e;

  class pkt;
    rand kind_e kind;
    rand bit [7:0] len;
    rand bit [3:0] ch;
    rand bit par;
    bit [1:0] mode;              // testbench state: no constraint reads it
    rand bit [2:0] src;          // rand, but the monitor overwrites it from the interface
    constraint c_len { kind == IDLE -> len == 0; kind != IDLE -> len inside {[1:200]}; }
    constraint c_ch { kind == CFG -> ch < 4; (kind == WR) -> (ch != len[3:0]); }
`ifdef VERILATOR
    // this simulator picks a dist bucket before solving, so a pinned value outside it fails randomize()
    // (simulator bug, see T2); the legal set is the same
    constraint c_dist { kind inside {IDLE, RD, WR, CFG}; }
`else
    constraint c_dist { kind dist {IDLE := 1, RD := 5, WR := 5, CFG := 1}; }
`endif
    constraint c_par { soft par == len[0]; }

`ifndef VERILATOR
    covergroup cg;
      cp_kind : coverpoint kind { bins seq_rw = (RD => WR => RD); bins idle_cfg = (IDLE => CFG [*2]); }
      cp_len : coverpoint len iff (kind != IDLE) { bins short_len = {[1:15]}; bins big[3] = {[16:200]}; bins top = {200}; }
      cp_ch : coverpoint ch { bins lo = {[0:3]}; bins hi = {[12:15]}; }
      cp_par : coverpoint par { option.at_least = 2; bins one = {1}; }
      cp_k : coverpoint kind;
      x_kc : cross cp_k, cp_ch { ignore_bins i = binsof(cp_k) intersect {IDLE}; }
      cp_mode : coverpoint mode { bins m3 = {3}; }
      cp_src : coverpoint src { bins s5 = {5}; }
      x_ks : cross cp_k, cp_src;
    endgroup
    function new(); cg = new(); endfunction
`else
    function new(); endfunction
`endif
  endclass

  // a monitor that copies a DUT value into the object: src is observed, not stimulus
  class mon;
    virtual wt_if vif;
    function void observe(pkt p); p.src = vif.src; endfunction
  endclass

  // a coverage collector: covergroups sampled with the object, under conditions on a rand field
  // (kind) and on testbench state (mode, set before randomize())
`ifndef VERILATOR
  class coll;
    covergroup rd_cg with function sample(pkt p);
      cp_len : coverpoint p.len { bins b[4] = {[1:200]}; }
      cp_ch : coverpoint p.ch { bins lo = {[0:7]}; bins hi = {[8:15]}; }
    endgroup
    covergroup md_cg with function sample(pkt p);
      cp_kind : coverpoint p.kind { bins wr = {WR}; bins rd_cfg = (RD => CFG); }
    endgroup
    function new(); rd_cg = new(); md_cg = new(); endfunction
    function void write(pkt p);
      case (p.kind)
        RD: rd_cg.sample(p);
        default: ;
      endcase
      if (p.mode == 2) md_cg.sample(p);
    endfunction
  endclass
`endif
endpackage

module wt_top;
  initial begin
    wt_pkg::pkt p = new();
    void'(p.randomize());
  end
endmodule
